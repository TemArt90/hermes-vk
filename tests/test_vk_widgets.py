"""Tests for interactive chat widgets: the on-disk store, the renderer, the press path, the tool.

Canonical run (from the Hermes runtime, so `gateway.*` imports resolve):
    cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q

Three limits encoded here were measured on a live community (04.10.2026), not read off the docs:
an inline keyboard carries at most 10 buttons (11 → error 911), ``messages.edit`` accepts
``keyboard`` (verified by reading the message back), and edited messages lose ``format_data`` — which
is why the widget body is plain text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import sys
import tempfile

import _paths  # noqa: E402  (registers the plugin as `vk`)

for _inherited in [name for name in list(os.environ) if name.startswith("VK_")]:
    os.environ.pop(_inherited, None)
for _inherited in [name for name in list(os.environ) if name.startswith("HERMES_SESSION_")]:
    os.environ.pop(_inherited, None)

from vk import widgets as W  # noqa: E402
from vk.tools import _handle, _resolve_peer  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────

def store_in_tmp(tmp_path: pathlib.Path) -> W.WidgetStore:
    return W.WidgetStore(tmp_path / "vk_widgets.json")


def buttons(keyboard_json: str):
    data = json.loads(keyboard_json)
    return [b for row in data["buttons"] for b in row], data["buttons"]


@contextlib.contextmanager
def open_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def make_adapter(tmp_path: pathlib.Path):
    """Adapter with the suite's stubbed transport and widgets in a scratch file."""
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "adapter-widgets.json")
    from test_vk_adapter import FakeClient
    from vk.adapter import VKAdapter

    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry

    if not platform_registry.is_registered("vk"):
        Platform._add_pseudo_member("vk")
    adapter = VKAdapter(PlatformConfig(extra={}))
    adapter.client = FakeClient()
    return adapter


# ── store + renderer ────────────────────────────────────────────────────────

def test_create_renders_plain_text_keyboard_and_counts(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(13580122, "Список покупок", ["Молоко", "Хлеб"], message_id=777)
    done, total = W.counts(widget)
    assert (done, total) == (0, 2)
    text = W.render_text(widget)
    assert "Список покупок" in text and "☐ Молоко" in text and "Отмечено 0 из 2" in text
    # Edit keeps no format_data, so the body must survive as plain characters: no markdown markers.
    assert "**" not in text and "*" not in text
    kb = W.render(widget)[1]
    labels = [b["action"]["label"] for b in json.loads(kb)["buttons"][0]]
    assert labels == ["☐ Молоко", "☐ Хлеб"]


def test_toggle_persists_across_instances_and_flips_back(tmp_path):
    path = tmp_path / "w.json"
    store = W.WidgetStore(path)
    widget = store.create(1, "Список", ["Молоко", "Хлеб"])
    key = widget["items"][0]["k"]
    store.toggle(widget["id"], key)
    fresh = W.WidgetStore(path)
    reloaded = fresh.get(widget["id"])
    assert reloaded["items"][0]["d"] is True and reloaded["items"][1]["d"] is False
    again, item = fresh.toggle(widget["id"], key)
    assert again["items"][0]["d"] is False and item["d"] is False


def test_unknown_key_or_widget_is_a_no_op(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Список", ["Молоко"])
    assert store.toggle(widget["id"], "nope") == (None, None)
    assert store.toggle("deadbe", widget["items"][0]["k"]) == (None, None)
    # the failed toggle must not have wiped the widget
    assert store.get(widget["id"])["items"][0]["d"] is False


def test_keyboard_stays_within_vk_limits_on_every_page(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Покупки", [f"позиция {i}" for i in range(1, 14)])
    assert W.page_count(widget) == 2  # 8 per page + pager
    for page in range(W.page_count(widget)):
        widget["page"] = page
        buttons_, rows_ = buttons(W.render(widget)[1])
        assert len(buttons_) <= 10, "VK refuses an 11th inline button with error 911"
        assert len(rows_) <= 6
    # single-page lists use the full ten buttons and need no pager
    small = store.create(1, "Мелкий", [f"п{i}" for i in range(10)])
    buttons_, _ = buttons(W.render(small)[1])
    assert len(buttons_) == 10
    assert all("◀" not in b["action"]["label"] for b in buttons_)


def test_pager_wraps_and_page_survives_reload(tmp_path):
    path = tmp_path / "w.json"
    store = W.WidgetStore(path)
    widget = store.create(1, "Покупки", [f"п{i}" for i in range(1, 12)])
    store.bump_page(widget["id"], 1)
    assert W.WidgetStore(path).get(widget["id"])["page"] == 1
    store.bump_page(widget["id"], 1)  # 1 -> 0, wrapping instead of sticking at the edge
    assert store.get(widget["id"])["page"] == 0
    store.bump_page(widget["id"], -1)
    assert store.get(widget["id"])["page"] == W.page_count(store.get(widget["id"])) - 1


def test_page_slice_matches_the_buttons(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Покупки", [f"п{i}" for i in range(1, 14)])
    first = [i["t"] for i in W.page_items(widget, 0)]
    second = [i["t"] for i in W.page_items(widget, 1)]
    assert first == [f"п{i}" for i in range(1, 9)] and second == [f"п{i}" for i in range(9, 14)]


def test_update_keeps_ticks_of_unchanged_labels_and_drops_removed(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Покупки", ["Молоко", "Хлеб"])
    store.toggle(widget["id"], widget["items"][0]["k"])
    updated = store.set_items(widget["id"], ["молоко ", "Сыр"])
    # case/space-insensitive match keeps the tick, the disappeared item is gone, the new one is open
    assert [(i["t"], i["d"]) for i in updated["items"]] == [("молоко", True), ("Сыр", False)]
    assert updated["items"][0]["k"] == widget["items"][0]["k"]


def test_ownership_is_per_chat(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(2000000001, "Список", ["Молоко"])
    assert store.owned(widget["id"], 2000000001) is not None
    assert store.owned(widget["id"], 2000000002) is None  # a forwarded keyboard must not move state
    assert store.owned("nope", 2000000001) is None


def test_active_for_is_the_newest_open_widget_of_that_chat(tmp_path):
    store = store_in_tmp(tmp_path)
    first = store.create(5, "Старый", ["а"])
    second = store.create(5, "Новый", ["б"])
    store.create(6, "Чужой", ["в"])
    assert store.active_for(5)["id"] == second["id"]
    store.close(second["id"])
    assert store.active_for(5)["id"] == first["id"]


def test_reset_and_close(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Покупки", ["Молоко", "Хлеб"])
    store.toggle(widget["id"], widget["items"][1]["k"])
    reset = store.reset(widget["id"])
    assert [i["d"] for i in reset["items"]] == [False, False] and reset["page"] == 0
    closed = store.close(widget["id"])
    assert closed["closed_at"] and store.active_for(1) is None


def test_long_list_is_truncated_in_the_body_but_not_in_the_state(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Длинный", [f"позиция номер {i} " + "х" * 60 for i in range(1, 61)])
    text = W.render_text(widget)
    assert len(text) <= W.TEXT_LIMIT + 200
    assert "и ещё" in text
    assert len(store.get(widget["id"])["items"]) == 60


def test_corrupt_file_does_not_break_the_store(tmp_path):
    path = tmp_path / "w.json"
    path.write_text("{not json", encoding="utf-8")
    store = W.WidgetStore(path)
    assert store.all() == {}
    widget = store.create(1, "Список", ["Молоко"])
    assert W.WidgetStore(path).get(widget["id"])["items"][0]["t"] == "Молоко"


def test_prune_drops_old_widgets(tmp_path):
    store = store_in_tmp(tmp_path)
    widget = store.create(1, "Список", ["Молоко"])
    data = store.all()
    data[widget["id"]]["updated_at"] = "2000-01-01T00:00:00+00:00"
    store.path.write_text(json.dumps({"version": 1, "widgets": data}), encoding="utf-8")
    assert store.prune(30) == 1 and store.get(widget["id"]) is None


# ── the press path ──────────────────────────────────────────────────────────

def test_widget_press_toggles_and_rewrites_the_same_message(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(123456, "Покупки", ["Молоко", "Хлеб"], message_id=555)
    key = widget["items"][0]["k"]
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e1",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "t", "k": key}),
        }))
    assert len(adapter.client.edits) == 1
    edit = adapter.client.edits[0]
    assert edit["peer_id"] == 123456 and edit["message_id"] == 555
    assert "✅ Молоко" in edit["message"]
    labels = [b["action"]["label"] for b in json.loads(edit["keyboard"])["buttons"][0]]
    assert labels == ["✅ Молоко", "☐ Хлеб"]
    assert adapter.client.answers and adapter.client.answers[0][0].startswith("Молоко — отмечено")
    assert adapter.widgets.get(widget["id"])["items"][0]["d"] is True


def test_widget_press_from_another_chat_is_refused(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(2000000001, "Покупки", ["Молоко"])
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 999, "peer_id": 2000000002, "event_id": "e2",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "t", "k": "k1"}),
        }))
    assert adapter.client.edits == []
    assert adapter.client.answers[0][0] == "Список устарел"
    assert adapter.widgets.get(widget["id"])["items"][0]["d"] is False


def test_unknown_widget_press_answers_without_sending(tmp_path):
    adapter = make_adapter(tmp_path)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e3",
            "payload": json.dumps({"v": "w", "id": "deadbe", "a": "t", "k": "k1"}),
        }))
    assert adapter.client.edits == [] and adapter.client.sent == []
    assert adapter.client.answers[0][0] == "Список устарел"


def test_unknown_item_key_reports_the_press_without_touching_the_message(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(123456, "Покупки", ["Молоко"], message_id=556)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e4",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "t", "k": "k99"}),
        }))
    assert adapter.client.edits == [] and adapter.client.answers[0][0] == "Пункт не найден"


def test_pager_press_rewrites_with_the_next_page(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(123456, "Покупки", [f"п{i}" for i in range(1, 14)], message_id=557)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e5",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "p", "d": 1}),
        }))
    assert adapter.client.edits[0]["message"].endswith("стр. 2/2")
    assert adapter.client.answers[0][0] == "Стр. 2/2"
    assert adapter.widgets.get(widget["id"])["page"] == 1


def test_last_tick_gets_a_distinct_snackbar(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(123456, "Покупки", ["Молоко"], message_id=558)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e6",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "t", "k": "k1"}),
        }))
    assert adapter.client.answers[0][0] == "Всё отмечено ✅ 1/1"


def test_failed_edit_degrades_to_a_snackbar(tmp_path):
    adapter = make_adapter(tmp_path)
    widget = adapter.widgets.create(123456, "Покупки", ["Молоко"], message_id=559)

    async def boom(*args, **kwargs):
        from vk.vk_api import VkApiError

        raise VkApiError("messages.edit", 100, "invalid message_id")

    adapter.client.edit_message = boom
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e7",
            "payload": json.dumps({"v": "w", "id": widget["id"], "a": "t", "k": "k1"}),
        }))
    assert "Не удалось обновить" in adapter.client.answers[0][0]
    # the state is still applied — a failed rewrite must not silently drop the tick in the data
    assert adapter.widgets.get(widget["id"])["items"][0]["d"] is True


# ── the tool ────────────────────────────────────────────────────────────────

def test_status_without_a_widget_reports_failure(tmp_path):
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "tool.json")
    assert json.loads(asyncio.run(_handle({"action": "status"})))["ok"] is False


def test_create_without_a_token_is_refused_not_sent(tmp_path):
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "tool.json")
    result = json.loads(asyncio.run(_handle({"action": "create", "title": "Список",
                                            "items": ["Молоко"], "peer": "13580122"})))
    assert result["ok"] is False and "VK_TOKEN" in result["error"]


def test_create_needs_items(tmp_path):
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "tool.json")
    os.environ["VK_TOKEN"] = "token"
    try:
        result = json.loads(asyncio.run(_handle({"action": "create", "peer": "13580122", "items": []})))
    finally:
        os.environ.pop("VK_TOKEN", None)
    assert result["ok"] is False and "items" in result["error"]


def test_peer_comes_from_the_session_then_the_home_channel(tmp_path, monkeypatch):
    monkeypatch.delenv("VK_HOME_CHANNEL", raising=False)
    os.environ["HERMES_SESSION_PLATFORM"] = "vk"
    os.environ["HERMES_SESSION_CHAT_ID"] = "2000000007"
    try:
        assert _resolve_peer({}) == (2000000007, "session")
        os.environ["HERMES_SESSION_PLATFORM"] = "email"
        assert _resolve_peer({"peer": "12345"}) == (12345, "argument")
        assert _resolve_peer({}) == (None, "none")
    finally:
        os.environ.pop("HERMES_SESSION_PLATFORM", None)
        os.environ.pop("HERMES_SESSION_CHAT_ID", None)


def test_status_reports_the_live_state(tmp_path):
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "tool.json")
    store = W.WidgetStore()
    widget = store.create(13580122, "Список покупок", ["Молоко", "Хлеб"], message_id=700)
    store.toggle(widget["id"], widget["items"][0]["k"])
    result = json.loads(asyncio.run(_handle({"action": "status", "peer": "13580122"})))
    assert result["ok"] and (result["done"], result["total"]) == (1, 2)
    assert result["message_id"] == 700
    assert [i["done"] for i in result["items"]] == [True, False]
    assert "✅ Молоко" in result["text"]


def test_unknown_action_is_rejected(tmp_path):
    os.environ["VK_WIDGETS_FILE"] = str(tmp_path / "tool.json")
    assert json.loads(asyncio.run(_handle({"action": "nope"})))["ok"] is False


def test_register_tools_hands_the_tool_to_the_plugin_context():
    from vk.tools import DESCRIPTION, SCHEMA, TOOL_NAME, register_tools

    captured = {}

    class Ctx:
        def register_tool(self, **kwargs):
            captured.update(kwargs)

    register_tools(Ctx())
    assert captured["name"] == TOOL_NAME and captured["schema"] is SCHEMA
    assert captured["description"] == DESCRIPTION and captured["is_async"] is True
    assert captured["handler"].__name__ == "_handle"
    # The registry reads schema["parameters"] and the model-facing description from schema itself:
    # a bare parameters block registered the tool with empty arguments (measured live).
    assert SCHEMA["name"] == TOOL_NAME and SCHEMA["description"] == DESCRIPTION
    assert SCHEMA["parameters"]["required"] == ["action"]
    assert {"action", "items", "title", "widget_id", "peer"} <= set(SCHEMA["parameters"]["properties"])


if __name__ == "__main__":  # standalone fallback without pytest
    failures = 0
    for name, func in sorted(list(globals().items())):
        if not name.startswith("test_") or not callable(func):
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                params = func.__code__.co_varnames[: func.__code__.co_argcount]
                if "tmp_path" in params:
                    func(pathlib.Path(tmp))
                else:
                    func()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failures else 0)
