"""Trust rules for the OAuth endpoints an MCP server advertises.

A server's discovery documents name the URLs Hermes then sends client secrets, authorization codes
and refresh tokens to, and opens in the user's browser. Every OAuth request Hermes makes for an MCP
server (SDK discovery/registration/token/refresh, pre-flight discovery, device login, revocation)
and the authorization URL it opens pass :func:`check_oauth_endpoint`:

- only ``http``/``https``; never the cloud-metadata floor;
- a public MCP server may not point Hermes at a private, loopback or link-local address
  (``security.allow_private_urls`` / ``fake_ip_ranges`` opt-outs apply, as for every other fetch);
- credentials need ``https``; plain ``http`` is accepted only for a loopback endpoint, or for a
  local-network endpoint of a server that is itself on the local network (that server already
  receives its bearer token over the same link).

Responses are capped at :data:`OAUTH_MAX_RESPONSE_BYTES` and :data:`OAUTH_FETCH_TIMEOUT` seconds.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

OAUTH_MAX_RESPONSE_BYTES = 1024 * 1024
OAUTH_FETCH_TIMEOUT = 20.0


class OAuthEndpointRefused(RuntimeError):
    """An advertised OAuth endpoint, or its response, failed the trust rules above."""


def _refuse(url: str, reason: str) -> OAuthEndpointRefused:
    parts = urlsplit(url)
    shown = f"{parts.scheme}://{parts.netloc}{parts.path}"  # never echo query strings (codes, state)
    return OAuthEndpointRefused(f"MCP OAuth refused {shown}: {reason}")


def check_oauth_endpoint(url: str, server_url: str | None) -> None:
    """Raise :class:`OAuthEndpointRefused` unless *url* may receive OAuth traffic for the MCP server
    at *server_url* (unknown server = public). Resolves DNS: call off the event loop."""
    from tools.url_safety import host_address_class, is_always_blocked_url, is_safe_url
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname or ""
    except ValueError:
        raise OAuthEndpointRefused("MCP OAuth refused a malformed endpoint URL") from None
    if scheme not in ("http", "https") or not host:
        raise _refuse(url, "OAuth endpoints must be http(s) URLs")
    if is_always_blocked_url(url):
        raise _refuse(url, "it points at a cloud metadata address")
    server_host = urlsplit(server_url).hostname if server_url else None
    server_class = host_address_class(server_host) if server_host else "public"
    if server_class == "public" and not is_safe_url(url):
        raise _refuse(url, "the MCP server is public but this OAuth endpoint is on a private, loopback or "
                           "link-local address (or does not resolve)")
    if scheme == "http":
        endpoint_class = host_address_class(host)
        if not (endpoint_class == "loopback" or (endpoint_class == "internal" and server_class != "public")):
            raise _refuse(url, "OAuth credentials are only sent over https (plain http is accepted for "
                               "loopback, or a local-network server's own local endpoints)")


async def check_oauth_endpoint_async(url: str, server_url: str | None) -> None:
    import anyio
    await anyio.to_thread.run_sync(check_oauth_endpoint, url, server_url)


async def guard_oauth_request(request: Any, server_url: str | None) -> None:
    """Validate an outgoing OAuth request and bound each of its network phases."""
    await check_oauth_endpoint_async(str(request.url), server_url)
    request.extensions = {**request.extensions, "timeout": dict.fromkeys(
        ("connect", "read", "write", "pool"), OAUTH_FETCH_TIMEOUT)}


def cap_oauth_response(response: Any) -> None:
    """Bound the not-yet-read body of an OAuth *response* by size and wall-clock time; reading past
    either raises :class:`OAuthEndpointRefused`. A body already read is left alone."""
    stream = getattr(response, "stream", None)
    if stream is None or getattr(response, "is_stream_consumed", True) or hasattr(response, "_content"):
        return
    declared = response.headers.get("content-length", "")
    # An oversized declaration is refused on the first read, inside the reader's own flow.
    oversized = declared.isdigit() and int(declared) > OAUTH_MAX_RESPONSE_BYTES
    response.stream = _capped_stream_cls(type(stream).__mro__)(
        stream, str(response.request.url), time.monotonic() + OAUTH_FETCH_TIMEOUT, oversized)


def _capped_stream_cls(stream_mro: tuple) -> type:
    """A byte-stream wrapper deriving from the same httpx flavour's ``AsyncByteStream`` as the
    stream it wraps (httpx and the SDK's httpx2 each check ``isinstance`` against their own)."""
    base = next((cls for cls in stream_mro if cls.__name__ == "AsyncByteStream"), object)

    class _CappedOAuthStream(base):  # type: ignore[misc, valid-type]
        def __init__(self, inner, url: str, deadline: float, oversized: bool):
            self._inner, self._url, self._deadline, self._oversized = inner, url, deadline, oversized

        async def __aiter__(self):
            import anyio
            if self._oversized:
                raise _refuse(self._url, f"its response exceeds {OAUTH_MAX_RESPONSE_BYTES} bytes")
            total, chunks = 0, self._inner.__aiter__()
            while True:
                try:
                    with anyio.fail_after(max(self._deadline - time.monotonic(), 0)):
                        chunk = await chunks.__anext__()
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    raise _refuse(self._url, f"its response took longer than {OAUTH_FETCH_TIMEOUT:g}s") from None
                total += len(chunk)
                if total > OAUTH_MAX_RESPONSE_BYTES:
                    raise _refuse(self._url, f"its response exceeds {OAUTH_MAX_RESPONSE_BYTES} bytes")
                yield chunk

        async def aclose(self):
            await self._inner.aclose()

    return _CappedOAuthStream


def oauth_client_event_hooks(server_url: str | None) -> dict[str, list]:
    """``event_hooks=`` for an httpx client Hermes owns for OAuth traffic: every request (redirect
    hops included) is checked and every response capped, through the same rules as the SDK flow."""
    async def _on_request(request):
        await guard_oauth_request(request, server_url)

    async def _on_response(response):
        cap_oauth_response(response)

    return {"request": [_on_request], "response": [_on_response]}
