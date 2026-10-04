"""Tests for the status-bubble path (``send_or_update_status``) and message deletion.

Both exist to cut chat noise, and both are optional capabilities the gateway probes by name:

* ``gateway/run.py`` routes every status event through ``adapter.send_or_update_status`` when it
  exists and otherwise falls back to a plain send — measured before this change, one long turn left a
  trail of one-line bubbles ("⏳ Работаю — 9 мин…", "💻 Выполняю…").
* ``delete_message`` is what the end-of-turn cleanup (``display.cleanup_progress``) and ephemeral TTLs
  call; the base implementation answers ``False``, so VK used to keep every bubble forever.

Canonical run:
    cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import time

import _paths  # noqa: E402  (registers the plugin as `vk`, whatever this directory is called)

for _inherited in [name for name in list(os.environ) if name.startswith("VK_")]:
    os.environ.pop(_inherited, None)

from vk.adapter import MAX_STATUS_BUBBLES, STATUS_BUBBLE_TTL_SECONDS, VKAdapter  # noqa: E402
from vk.vk_api import VkClient  # noqa: E402


@contextlib.contextmanager
def open_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def make_adapter() -> VKAdapter:
    from test_vk_adapter import FakeClient
    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry

    if not platform_registry.is_registered("vk"):
        Platform._add_pseudo_member("vk")
    adapter = VKAdapter(PlatformConfig(extra={}))
    adapter.client = FakeClient()
    return adapter


def status(adapter, text, *, key="long_running", chat="123456"):
    return asyncio.run(adapter.send_or_update_status(chat, key, text))


# ── one bubble per status stream ────────────────────────────────────────────

def test_the_capability_is_visible_on_the_type():
    """The gateway gates the whole status path on the method existing on the class."""
    assert callable(getattr(VKAdapter, "send_or_update_status", None))
    assert callable(getattr(VKAdapter, "delete_message", None))


def test_first_status_sends_one_message_without_a_keyboard():
    adapter = make_adapter()
    result = status(adapter, "⏳ Работаю — 1 мин")
    assert result.success is True and result.message_id
    assert len(adapter.client.sent) == 1
    sent = adapter.client.sent[0]
    assert sent["peer_id"] == 123456 and sent["message"].startswith("⏳ Работаю")
    assert "keyboard" not in sent or not sent.get("keyboard"), "status chatter carries no buttons"


def test_the_next_status_of_the_same_stream_edits_the_same_bubble():
    adapter = make_adapter()
    first = status(adapter, "⏳ Работаю — 1 мин")
    second = status(adapter, "⏳ Работаю — 2 мин")
    assert len(adapter.client.sent) == 1, "a status stream must not append a bubble per update"
    assert len(adapter.client.edits) == 1
    assert adapter.client.edits[0]["message_id"] == int(first.message_id)
    assert adapter.client.edits[0]["message"].endswith("2 мин")
    assert second.message_id == first.message_id


def test_a_different_status_key_gets_its_own_bubble():
    adapter = make_adapter()
    status(adapter, "работаю", key="long_running")
    status(adapter, "предупреждение", key="warning")
    assert len(adapter.client.sent) == 2 and adapter.client.edits == []


def test_the_same_key_in_another_chat_does_not_reuse_the_bubble():
    adapter = make_adapter()
    status(adapter, "работаю", chat="123456")
    status(adapter, "работаю", chat="2000000001")
    assert len(adapter.client.sent) == 2
    assert {call["peer_id"] for call in adapter.client.sent} == {123456, 2000000001}


def test_an_edit_failure_falls_back_to_a_fresh_bubble():
    adapter = make_adapter()
    first = status(adapter, "первый статус")

    async def boom(*args, **kwargs):
        raise RuntimeError("message can not be found")

    adapter.client.edit_message = boom
    second = status(adapter, "второй статус")
    assert len(adapter.client.sent) == 2, "a refused edit must not swallow the status"
    assert second.message_id != first.message_id
    assert adapter._status_bubbles["123456:long_running"][0] == int(second.message_id)


def test_an_idle_stream_is_not_resurrected():
    adapter = make_adapter()
    status(adapter, "прошлый запуск")
    key = "123456:long_running"
    message_id, _ = adapter._status_bubbles[key]
    adapter._status_bubbles[key] = (message_id, time.time() - STATUS_BUBBLE_TTL_SECONDS - 1)
    status(adapter, "новый запуск")
    assert len(adapter.client.sent) == 2 and adapter.client.edits == []


def test_the_bubble_map_is_capped():
    adapter = make_adapter()
    for i in range(MAX_STATUS_BUBBLES + 5):
        adapter._status_bubbles[f"c{i % 3}:k{i}"] = (i, time.time() + i)
    adapter._prune_status_bubbles()
    assert len(adapter._status_bubbles) == MAX_STATUS_BUBBLES


def test_status_refuses_bad_input_instead_of_raising():
    adapter = make_adapter()
    assert status(adapter, "текст", chat="не-число").success is False
    assert status(adapter, "   ").success is False
    assert adapter.client.sent == []
    adapter.client = None
    assert status(adapter, "текст").success is False


def test_a_send_failure_is_reported_as_retryable():
    adapter = make_adapter()

    async def boom(*args, **kwargs):
        raise RuntimeError("flood control")

    adapter.client.send_message = boom
    result = status(adapter, "статус")
    assert result.success is False and result.retryable is True


# ── deletion ────────────────────────────────────────────────────────────────

def test_delete_message_removes_the_message_by_global_id():
    adapter = make_adapter()
    assert asyncio.run(adapter.delete_message("123456", "777")) is True
    assert adapter.client.deletes == [{"peer_id": 123456, "message_id": 777}]


def test_deleting_a_status_bubble_forgets_its_mapping():
    adapter = make_adapter()
    first = status(adapter, "статус")
    assert asyncio.run(adapter.delete_message("123456", str(first.message_id))) is True
    # the next update of that stream must send a new bubble, not edit a deleted one
    status(adapter, "статус снова")
    assert len(adapter.client.sent) == 2 and adapter.client.edits == []


def test_delete_message_reports_failure_without_raising():
    adapter = make_adapter()

    async def boom(*args, **kwargs):
        raise RuntimeError("access denied: message can not be found")

    adapter.client.delete_message = boom
    assert asyncio.run(adapter.delete_message("123456", "777")) is False

    async def refused(*args, **kwargs):
        return False

    adapter.client.delete_message = refused
    assert asyncio.run(adapter.delete_message("123456", "777")) is False


def test_delete_message_refuses_junk_ids():
    adapter = make_adapter()
    assert asyncio.run(adapter.delete_message("123456", "не-число")) is False
    assert asyncio.run(adapter.delete_message("нет-чата", "777")) is False
    assert adapter.client.deletes == []


def test_delete_message_without_a_client_is_false():
    adapter = make_adapter()
    adapter.client = None
    assert asyncio.run(adapter.delete_message("123456", "777")) is False


# ── the API call itself ─────────────────────────────────────────────────────

def test_client_delete_message_parses_vk_response_and_sends_delete_for_all():
    client = VkClient("token", api_version="5.199")
    calls = []

    async def fake_call(method, **params):
        calls.append((method, params))
        return [{"peer_id": 13580122, "message_id": 363, "conversation_message_id": 360, "response": 1}]

    client.call = fake_call
    assert asyncio.run(client.delete_message(13580122, 363)) is True
    method, params = calls[0]
    assert method == "messages.delete"
    assert params["peer_id"] == 13580122 and params["message_ids"] == 363
    assert params["delete_for_all"] == 1


def test_client_delete_message_is_false_when_vk_reports_an_error_entry():
    client = VkClient("token", api_version="5.199")

    async def fake_call(method, **params):
        return [{"peer_id": 13580122, "message_id": 363,
                 "error": {"code": 15, "description": "Access denied"}}]

    client.call = fake_call
    assert asyncio.run(client.delete_message(13580122, 363)) is False


def test_client_delete_message_can_hide_only_for_the_community():
    client = VkClient("token", api_version="5.199")
    calls = []

    async def fake_call(method, **params):
        calls.append(params)
        return [{"response": 1}]

    client.call = fake_call
    assert asyncio.run(client.delete_message(13580122, 363, for_all=False)) is True
    assert calls[0]["delete_for_all"] == 0


if __name__ == "__main__":  # standalone fallback without pytest
    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print("FAIL", name, "->", type(exc).__name__, exc)
    print("итог: провалов", failures)
