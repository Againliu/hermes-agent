"""Exact-value redaction of the active profile's own configured secrets.

The pattern rules in ``agent.redact`` only see credential *shapes* (vendor prefixes,
``KEY=value`` assignments, auth headers). An opaque key with no known prefix, printed
without its variable name (an MCP server echoing its env, a provider error quoting the
key, a JSON dump of a request), passes them all. This module knows the actual values the
user configured and masks them wherever they appear, in raw, JSON-escaped and
URL-encoded form:

- ``.env`` values whose variable name is credential-shaped;
- ``auth.json`` tokens and keys (provider logins, the credential pool);
- ``config.yaml`` credential fields (``api_key``, ``token``, …) at any depth, every MCP
  server's ``headers`` values, credential-named ``env`` values and ``url`` query values;
- MCP OAuth tokens under ``mcp-tokens/``;
- values registered at runtime (:func:`register_secret_value`), e.g. the rendered
  ``${VAR}`` env/headers an MCP server was spawned with. These stay registered after
  the config is edited, so a still-running child cannot print the old value unmasked.

One process serves many profiles, so everything is keyed by profile home: profile A's
values never mask (or confirm the existence of) bytes in profile B's output. Values
shorter than ``MIN_VALUE_CHARS`` are never registered, since an unanchored replace of a
short common string would shred unrelated text.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

MARKER = "«redacted-secret»"
MIN_VALUE_CHARS = 12
_RUNTIME_MAX_PER_PROFILE = 256
# Redaction runs on every log record, so the profile's files are re-stat'ed at most this often.
_RECHECK_SECONDS = 2.0

# Env-var NAMES that hold a secret: underscore-delimited so USER / ADDRESS / URL / HOST never match.
_ENV_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_|ACCESS_|REFRESH_)?"
    r"(?:TOKEN|TOKENS|PASSWORD|PASSWD|PASS|PW|SECRET|CREDENTIAL|CREDENTIALS|KEY|KEYS)"
    r"(?:_|$)",
    re.IGNORECASE,
)
# JSON/YAML FIELD names: the credential word must END the name, so ``agent_key_expires_at``,
# ``token_type`` and ``secret_fingerprint`` (timestamps, labels, hashes) are not registered.
_FIELD_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|CREDENTIALS|KEY|AUTHORIZATION|APIKEY)$"
)
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_AUTH_SCHEME_RE = re.compile(r"^[A-Za-z][\w.-]*\s+(\S+)$")


def _is_secret_field(name: object) -> bool:
    if not isinstance(name, str):
        return False
    return bool(_FIELD_SECRET_NAME_RE.search(_CAMEL_BOUNDARY_RE.sub("_", name).upper().replace("-", "_")))


def _usable(value: object) -> bool:
    return isinstance(value, str) and len(value.strip()) >= MIN_VALUE_CHARS and "${" not in value


def _header_values(value: object):
    """A header value, plus its credential when it carries an auth scheme (``Bearer <token>``)."""
    if isinstance(value, str):
        yield value
        if m := _AUTH_SCHEME_RE.match(value.strip()):
            yield m.group(1)


def _url_query_values(url: object):
    if isinstance(url, str) and "?" in url:
        yield from (v for _, v in parse_qsl(urlsplit(url).query, keep_blank_values=False))


def mcp_server_secret_values(*, env=None, headers=None, url=None):
    """Secret values an MCP server connection carries: credential-named env values, every header
    value and every URL query value (pass the rendered ``${VAR}``-resolved forms when known)."""
    for name, value in env.items() if isinstance(env, dict) else ():
        if isinstance(name, str) and _ENV_SECRET_NAME_RE.search(name):
            yield value
    for value in headers.values() if isinstance(headers, dict) else ():
        yield from _header_values(value)
    yield from _url_query_values(url)


def _walk_fields(node, out: list, depth: int = 0) -> None:
    """Credential-named string fields at any depth of a JSON/YAML document."""
    if depth > 32:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _is_secret_field(key):
                out.append(value)
            elif isinstance(value, (dict, list)):
                _walk_fields(value, out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_fields(item, out, depth + 1)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _env_file_values(home: Path) -> list:
    from agent.secret_scope import load_env_file

    return [v for k, v in load_env_file(home / ".env").items() if _ENV_SECRET_NAME_RE.search(k)]


def _config_values(home: Path) -> list:
    from hermes_cli.config_backend import get_config_backend
    from hermes_yaml import YAMLError

    try:
        cfg = get_config_backend().read_user_doc_readonly(home)
    except (OSError, YAMLError, UnicodeDecodeError):  # absent or mid-edit: the other sources still apply
        return []
    out: list = []
    _walk_fields(cfg, out)
    servers = cfg.get("mcp_servers") if isinstance(cfg, dict) else None
    for server in (servers or {}).values() if isinstance(servers, dict) else ():
        if isinstance(server, dict):
            out.extend(mcp_server_secret_values(env=server.get("env"), headers=server.get("headers"),
                                                url=server.get("url")))
    return out


def _file_values(home: Path) -> list:
    values = _env_file_values(home) + _config_values(home)
    for doc in [_read_json(home / "auth.json"), *(_read_json(p) for p in _token_files(home))]:
        _walk_fields(doc, values)
    return values


def _token_files(home: Path) -> list:
    try:
        return sorted((home / "mcp-tokens").glob("*.json"))
    except OSError:
        return []


def _signature(home: Path) -> tuple:
    """Cheap change detector: (mtime, size) of every file a value comes from."""
    from hermes_cli.config_backend import CONFIG_FILENAME, config_version_or_none

    sig = [config_version_or_none(home / CONFIG_FILENAME)]
    for path in [home / ".env", home / "auth.json", *_token_files(home)]:
        try:
            st = path.stat()
            sig.append((path.name, st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((path.name, None, None))
    return tuple(sig)


def _forms(value: str) -> set:
    """The raw value plus the encodings it takes inside JSON bodies and URLs."""
    value = value.strip()
    return {value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}


class _Profile:
    __slots__ = ("checked_at", "file_values", "forms", "runtime", "signature")

    def __init__(self):
        self.checked_at = 0.0
        self.signature = None
        self.file_values: frozenset = frozenset()
        self.runtime: OrderedDict = OrderedDict()
        self.forms: tuple = ()

    def rebuild_forms(self) -> None:
        forms = set()
        for value in self.file_values.union(self.runtime):
            forms |= _forms(value)
        # Longest first: a value that is a substring of another never leaves the longer one's tail.
        self.forms = tuple(sorted((f for f in forms if len(f) >= MIN_VALUE_CHARS), key=len, reverse=True))


_PROFILES: dict = {}
_LOCK = threading.Lock()


def _scope(home) -> tuple[str, Path]:
    from hermes_constants import get_hermes_home, hermes_home_key

    path = Path(home) if home is not None else get_hermes_home()
    return hermes_home_key(path), path


def register_secret_value(value, *, home=None) -> None:
    """Mask *value* in this profile's output for the rest of the process (bounded, LRU)."""
    if not _usable(value):
        return
    key, _ = _scope(home)
    value = value.strip()
    with _LOCK:
        profile = _PROFILES.setdefault(key, _Profile())
        if value in profile.runtime:
            profile.runtime.move_to_end(value)
            return
        profile.runtime[value] = None
        while len(profile.runtime) > _RUNTIME_MAX_PER_PROFILE:
            profile.runtime.popitem(last=False)
        profile.rebuild_forms()


def _current_forms(home) -> tuple:
    key, path = _scope(home)
    now = time.monotonic()
    with _LOCK:
        profile = _PROFILES.setdefault(key, _Profile())
        if now - profile.checked_at < _RECHECK_SECONDS:
            return profile.forms
        profile.checked_at = now
    signature = _signature(path)
    if signature == profile.signature:
        return profile.forms
    values = frozenset(v.strip() for v in _file_values(path) if _usable(v))
    with _LOCK:
        profile.signature, profile.file_values = signature, values
        profile.rebuild_forms()
        return profile.forms


def redact_known_secret_values(text, *, home=None):
    """Replace every registered secret value of the profile (default: the ambient one)."""
    if not isinstance(text, str) or len(text) < MIN_VALUE_CHARS:
        return text
    for form in _current_forms(home):
        if form in text:
            text = text.replace(form, MARKER)
    return text


def clear_known_secret_values() -> None:
    """Forget every profile's values (tests; a long-lived process re-reads files on demand)."""
    with _LOCK:
        _PROFILES.clear()
