"""Keep a filled payment card out of screenshots until its page is gone.

A vault card fill writes the number and CVC into the page. Text that echoes them is scrubbed at the
egress boundary (``agent.redact_cards``), but pixels are not: a screenshot taken to "check the form"
carries the full card number into the model's context, the provider's logs and the session history.

From the moment a card is written until the filled document is gone (submit or any navigation loads a
new document, the tab closes, the browser session ends), every capture path for that task refuses:

- ``browser_exec``: images captured during the call are deleted and not attached;
- ``browser_cdp``: ``Page.captureScreenshot`` and the other pixel-producing methods refuse;
- ``vision_analyze``: local images written after the fill refuse.

The filled document is identified by ``performance.timeOrigin``, which every new document (including a
reload or a form POST) changes. A single-page checkout that submits without loading a new document stays
protected until it does: the guard errs toward refusing a capture. State is per profile and task, in memory.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

_DOCUMENT_ID_JS = "String(performance.timeOrigin)"
# CDP text when the supervisor's page session died with its tab: that page (and the card in it) is gone.
_GONE_MARKERS = ("session with given id not found", "no attached page session", "target closed")

CDP_CAPTURE_METHODS = frozenset({
    "Page.captureScreenshot", "Page.startScreencast", "Page.printToPDF", "HeadlessExperimental.beginFrame",
})


@dataclass
class _CardFill:
    origin: str
    document_id: str
    filled_at: float


_LIVE: dict = {}  # (profile home, task id) → _CardFill
_LOCK = threading.Lock()


def _key(task_id: Optional[str]) -> tuple[str, str]:
    from hermes_constants import get_hermes_home
    return str(get_hermes_home()), task_id or "default"


def _document_id(task_id: str) -> dict:
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is None:
        return {"gone": True}
    res = supervisor.evaluate_runtime(_DOCUMENT_ID_JS)
    if res.get("ok"):
        return {"id": str(res.get("result") or "")}
    error = str(res.get("error") or "").lower()
    return {"gone": any(marker in error for marker in _GONE_MARKERS)}


def mark_card_filled(task_id: Optional[str], origin: str, cvc: str = "") -> None:
    """Called before a card's values are written into ``origin``'s page (the fill itself does not
    navigate, so the document read here is the one that will hold the card)."""
    from agent.redact_cards import set_page_cvc

    key = _key(task_id)
    doc = _document_id(key[1])
    with _LOCK:
        _LIVE[key] = _CardFill(origin=origin, document_id=str(doc.get("id") or ""), filled_at=time.time())
    if cvc:
        set_page_cvc(key[1], cvc)


def clear(task_id: Optional[str]) -> None:
    from agent.redact_cards import drop_page_cvc

    key = _key(task_id)
    with _LOCK:
        _LIVE.pop(key, None)
    drop_page_cvc(key[1])


def live_fill(task_id: Optional[str]) -> Optional[_CardFill]:
    """The task's card fill while its document still lives, re-checked against the page now; None
    (and the state cleared) once the page navigated, closed or the browser session ended. A page that
    cannot be read for another reason keeps the protection."""
    key = _key(task_id)
    with _LOCK:
        fill = _LIVE.get(key)
    if fill is None:
        return None
    doc = _document_id(key[1])
    if doc.get("gone") or (doc.get("id") and fill.document_id and doc["id"] != fill.document_id):
        clear(task_id)
        return None
    return fill


def capture_refusal(task_id: Optional[str]) -> Optional[str]:
    """The refusal text while a filled card is on the task's page, else None."""
    fill = live_fill(task_id)
    if fill is None:
        return None
    return (f"Screenshots are blocked while a payment card is filled into {fill.origin}: the image would "
            "carry the card number. Submit the form or navigate away first; check the fields with text "
            "reads instead (card values come back masked).")


def discard_captures(printed_paths: list[str], workspace: Optional[str], since: float) -> list[str]:
    """Delete the images a ``browser_exec`` call wrote while a card was filled (the paths it printed and
    any image in its workspace newer than the call), so neither the result nor a later ``vision_analyze``
    can carry them. Returns the deleted paths."""
    candidates = set(printed_paths)
    if workspace and os.path.isdir(workspace):
        candidates.update(entry.path for entry in os.scandir(workspace)
                          if entry.is_file() and entry.name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")))
    deleted = []
    for path in sorted(candidates):
        try:
            if os.path.getmtime(path) >= since - 1:
                os.remove(path)
                deleted.append(path)
        except OSError:
            continue
    return deleted


def image_refusal(task_id: Optional[str], image_path: str) -> Optional[str]:
    """``vision_analyze`` on a local image written after the task's card fill, while it is live."""
    with _LOCK:
        fill = _LIVE.get(_key(task_id))
    if fill is None:
        return None
    try:
        written = os.path.getmtime(os.path.expanduser(image_path.removeprefix("file://")))
    except (OSError, ValueError):
        return None  # a URL or a missing file: not a capture of the page
    return capture_refusal(task_id) if written >= fill.filled_at - 1 else None
