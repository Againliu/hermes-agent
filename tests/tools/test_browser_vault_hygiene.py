"""Vault privacy around the current page: list scoped to the page's site, card numbers masked in any
grouping, the CVC masked only while its page lives, and captures refused while a card is filled."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.vault_store import VaultStore

_CARD = {"card_number": "4242424242424242", "cardholder_name": "A User", "exp_month": "7", "exp_year": "2029",
         "cvc": "123", "billing_postal_code": "94110"}
_CONTROLS = [{"autocomplete": "cc-number", "index": 0, "type": "text"},
             {"label": "CVC", "index": 1, "type": "text"}]


@pytest.fixture()
def store(tmp_path):
    return VaultStore(base_dir=tmp_path / "vault")


def _login(store, origin):
    return store.add_item(kind="login", label=origin, origin=origin,
                          secret={"identifier_type": "email", "identifier": f"me@{origin[8:]}", "password": "pw-1"})


class _Page:
    """The supervisor's view of the checkout tab: ``document`` changes when the page navigates."""

    def __init__(self):
        self.document = "1000.5"

    def evaluate_runtime(self, expression, **_):
        return {"ok": True, "result": self.document}


@pytest.fixture()
def filled_card(store):
    """A card filled into https://shop.test for task ``t``; yields the fake page."""
    from agent import redact, redact_cards
    from tools import browser_vault_card_guard, browser_vault_tool

    page = _Page()
    meta = store.add_item(kind="payment", label="Card", origin="https://shop.test", secret=_CARD)

    def fake_eval(task_id, expression):
        if "location.href" in expression:
            return {"success": True, "result": "https://shop.test/checkout"}
        return {"success": True, "result": json.dumps(_CONTROLS)}

    with patch("agent.vault_store.get_vault_store", return_value=store), \
         patch("tools.browser_supervisor.SUPERVISOR_REGISTRY.get", return_value=page), \
         patch.object(browser_vault_tool, "_eval_js", side_effect=fake_eval), \
         patch.object(browser_vault_tool, "_focus_bound_origin", return_value=None), \
         patch.object(browser_vault_tool, "_eval_js_secret",
                      return_value={"success": True, "result": json.dumps({"filled": 2})}), \
         patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
        assert json.loads(browser_vault_tool.browser_vault_fill(meta.id, task_id="t"))["success"] is True
        try:
            yield page
        finally:
            browser_vault_card_guard.clear("t")
            redact.clear_vault_redaction_values()
            redact_cards.clear_card_redactions()


def test_list_names_only_the_current_sites_items(store):
    from tools import browser_vault_tool

    _login(store, "https://github.com")
    _login(store, "https://shop.example.org")
    with patch("agent.vault_store.get_vault_store", return_value=store), \
         patch.object(browser_vault_tool, "_current_page_origin", return_value="https://github.com"):
        scoped = json.loads(browser_vault_tool._handle_vault({"action": "list"}, task_id="t"))
        everything = json.loads(browser_vault_tool._handle_vault({"action": "list", "all_sites": True}, task_id="t"))
    assert [i["origin"] for i in scoped["items"]] == ["https://github.com"]
    assert scoped["site"] == "github.com" and scoped["other_sites_items"] == 1
    assert "shop.example.org" not in json.dumps(scoped)
    assert {i["origin"] for i in everything["items"]} == {"https://github.com", "https://shop.example.org"}


@pytest.mark.parametrize("shown", ["4242 4242 4242 4242", "4242-4242-4242-4242", "4242 42424242 4242",
                                   "4242.4242.4242.4242"])
def test_card_number_masked_in_any_grouping_the_page_prints(filled_card, shown):
    from agent.redact import redact_sensitive_text

    out = redact_sensitive_text(f"value={shown};")
    assert "4242 4242" not in out and "42424242" not in out and out.count("4242") == 1, out
    assert "••••4242" in out


def test_cvc_masked_whole_number_only_until_the_page_navigates(filled_card):
    from agent.redact import redact_sensitive_text
    from tools.browser_vault_card_guard import capture_refusal

    assert redact_sensitive_text("cvc 123.") == "cvc «redacted-cvc»."
    assert redact_sensitive_text("order 41234, 1230 items, 2029-07") == "order 41234, 1230 items, 2029-07"
    assert capture_refusal("t")  # same document: still filled
    filled_card.document = "2000.25"  # submit loaded the confirmation page
    assert capture_refusal("t") is None
    assert redact_sensitive_text("cvc 123.") == "cvc 123."


def test_screenshot_and_vision_refused_while_card_is_filled(filled_card, tmp_path):
    from tools.browser_cdp_tool import browser_cdp
    from tools.vision_tools import _handle_vision_analyze
    import asyncio

    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")
    refused = json.loads(browser_cdp("Page.captureScreenshot", task_id="t"))
    assert refused.get("error_type") == "card_filled", refused
    seen = json.loads(asyncio.run(_handle_vision_analyze({"image_url": str(shot), "question": "?"}, task_id="t")))
    assert seen["error_type"] == "card_filled"
    assert json.loads(browser_cdp("Page.captureScreenshot", task_id="other")).get("error_type") != "card_filled"
    filled_card.document = "3000.75"
    assert json.loads(browser_cdp("Page.captureScreenshot", task_id="t")).get("error_type") != "card_filled"


def test_browser_exec_captures_during_a_fill_are_deleted(filled_card, tmp_path):
    from tools.browser_vault_card_guard import discard_captures
    import time

    started = time.time()
    shot, other = tmp_path / "s.png", tmp_path / "notes.txt"
    shot.write_bytes(b"x")
    other.write_text("keep", encoding="utf-8")
    assert discard_captures([], str(tmp_path), started) == [str(shot)]
    assert not shot.exists() and other.exists()
