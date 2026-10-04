"""Tests for the interactive pickers: /model (provider → model) and flat choice pickers.

The core picks up an adapter's picker support purely by introspection
(``getattr(type(adapter), "send_model_picker", None) is not None`` in
``gateway/slash_commands_model.py``), so the first thing pinned here is that both methods exist on the
type and return a ``SendResult`` the core can trust. Everything after — paging inside VK's measured
10-button ceiling, callback bookkeeping, re-checking the pressed model against what was offered — is
this module's own contract.

Canonical run:
    cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import types

import _paths  # noqa: E402  (registers the plugin as `vk`, whatever this directory is called)

for _inherited in [name for name in list(os.environ) if name.startswith("VK_")]:
    os.environ.pop(_inherited, None)

from vk import pickers as P  # noqa: E402
from vk.adapter import VKAdapter  # noqa: E402


PROVIDERS = [
    {"slug": "opencode-go", "name": "OpenCode Go", "models": ["deepseek-v4-pro", "qwen3.7-plus"],
     "total_models": 2, "is_current": True},
    {"slug": "openai", "name": "OpenAI", "models": ["gpt-5.2", "gpt-5.2-mini"], "total_models": 2},
]

CHOICES = [
    {"value": "off", "label": "Выключено", "is_current": False},
    {"value": "low", "label": "Низкий", "is_current": False},
    {"value": "medium", "label": "Средний", "is_current": True},
]


@contextlib.contextmanager
def open_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def make_adapter(tmp_path: pathlib.Path, extra=None) -> VKAdapter:
    from test_vk_adapter import FakeClient
    from gateway.config import Platform, PlatformConfig
    from gateway.platform_registry import platform_registry

    if not platform_registry.is_registered("vk"):
        Platform._add_pseudo_member("vk")
    adapter = VKAdapter(PlatformConfig(extra=extra or {}))
    adapter.client = FakeClient()
    return adapter


def buttons(keyboard_json):
    data = json.loads(keyboard_json)
    flat = [b for row in data["buttons"] for b in row]
    return flat, data["buttons"]


def model_state(**over):
    record = P.new_model_record("123456", "session", PROVIDERS, "deepseek-v4-pro", "opencode-go",
                                on_model_selected=None)
    record["id"] = "tst123"  # PickerStore.put() assigns this in production
    record.update(over)
    return record


def choice_state(**over):
    record = P.new_choice_record("123456", "session", "Уровень рассуждений", CHOICES,
                                 on_choice_selected=None)
    record["id"] = "tst456"
    record.update(over)
    return record


def labels_of(keyboard_json):
    flat, _ = buttons(keyboard_json)
    return [b["action"]["label"] for b in flat]


# ── the contract the core introspects ───────────────────────────────────────

def test_adapter_exposes_both_picker_methods_on_the_type():
    """The core gates the interactive path on the *type*, not the instance."""
    assert callable(getattr(VKAdapter, "send_model_picker", None))
    assert callable(getattr(VKAdapter, "send_choice_picker", None))


def test_send_model_picker_returns_a_successful_sendresult_with_a_keyboard(tmp_path):
    adapter = make_adapter(tmp_path)
    result = asyncio.run(adapter.send_model_picker(
        chat_id="123456", providers=PROVIDERS, current_model="deepseek-v4-pro",
        current_provider="opencode-go", session_key="session", on_model_selected=_noop))
    assert result.success is True and result.message_id
    sent = adapter.client.sent[0]
    assert sent["peer_id"] == 123456 and sent["keyboard"]
    flat, rows = buttons(sent["keyboard"])
    assert len(flat) <= 10 and len(rows) <= 6
    assert "Модель" in sent["message"] and "OpenCode Go" in " ".join(labels_of(sent["keyboard"]))
    assert adapter.pickers.count() == 1


def test_send_choice_picker_refuses_to_send_an_empty_choice_list(tmp_path):
    adapter = make_adapter(tmp_path)
    result = asyncio.run(adapter.send_choice_picker(
        chat_id="123456", title="Уровень", choices=[], session_key="s", on_choice_selected=_noop))
    assert result.success is False and adapter.client.sent == []


def test_send_model_picker_reports_a_bad_peer_instead_of_raising(tmp_path):
    adapter = make_adapter(tmp_path)
    result = asyncio.run(adapter.send_model_picker(
        chat_id="не-число", providers=PROVIDERS, current_model="m", current_provider="p",
        session_key="s", on_model_selected=_noop))
    assert result.success is False and "peer" in (result.error or "")


# ── rendering inside VK's ceiling ───────────────────────────────────────────

def test_provider_page_marks_the_current_provider_and_fits_the_ceiling():
    text, keyboard = P.render(model_state())
    assert "Сейчас: deepseek-v4-pro · opencode-go" in text
    flat, rows = buttons(keyboard)
    assert len(flat) <= 10 and len(rows) <= 6
    assert any(l.startswith("• OpenCode Go") for l in labels_of(keyboard))
    assert any(l.startswith("✖") for l in labels_of(keyboard))


def test_more_than_seven_providers_page_instead_of_overflowing():
    many = [dict(p, slug=f"p{i}", name=f"Провайдер {i}") for i, p in enumerate(PROVIDERS * 6)]
    text, keyboard = P.render(model_state(providers=many))
    assert "стр. 1/" in text
    flat, rows = buttons(keyboard)
    assert len(flat) <= 10 and len(rows) <= 6
    assert set(labels_of(keyboard)) & {"◀", "▶"}


def test_model_page_shows_the_models_of_the_chosen_provider_with_a_back_button():
    text, keyboard = P.render(model_state(view="models", provider="openai", page=0))
    assert "OpenAI — модели" in text
    labels = labels_of(keyboard)
    assert "gpt-5.2" in labels and "↩ провайдеры" in labels
    flat, rows = buttons(keyboard)
    assert len(flat) <= 10 and len(rows) <= 6


def test_a_provider_without_models_offers_only_the_way_back():
    empty = [{"slug": "x", "name": "Пустой", "models": [], "total_models": 0}]
    text, keyboard = P.render(model_state(providers=empty, view="models", provider="x"))
    assert "список моделей пуст" in text
    assert labels_of(keyboard) == ["↩ провайдеры"]


def test_long_model_lists_page_and_keep_every_model_reachable():
    models = [f"vendor/model-{i}" for i in range(1, 20)]
    providers = [{"slug": "big", "name": "Big", "models": models, "total_models": len(models)}]
    seen = []
    for page in range(P.pages_for(len(models))):
        _, keyboard = P.render(model_state(providers=providers, view="models", provider="big", page=page))
        flat, rows = buttons(keyboard)
        assert len(flat) <= 10 and len(rows) <= 6
        payloads = [json.loads(b["action"]["payload"]) for b in flat if b["action"]["payload"]]
        seen.extend(p["m"] for p in payloads if p.get("a") == "md")
    assert sorted(seen) == sorted(models)


def test_choice_picker_marks_the_current_value_and_fits():
    record = choice_state()
    text, keyboard = P.render(record)
    assert "Уровень рассуждений" in text
    labels = labels_of(keyboard)
    assert "• Средний" in labels and "✖" in labels
    assert len(buttons(keyboard)[0]) <= 10


def test_result_text_clears_the_keyboard_and_marks_the_outcome():
    text, keyboard = P.result_text({"succeeded": True}, "Модель переключена")
    assert text.startswith("✓") and "Модель переключена" in text
    assert json.loads(keyboard) == {"inline": True, "buttons": []}
    failed, _ = P.result_text({"succeeded": False}, "нет")
    assert failed.startswith("✗")


# ── store lifecycle ─────────────────────────────────────────────────────────

def test_expired_picker_is_gone(tmp_path):
    store = P.PickerStore(ttl_seconds=0)
    record = store.put(**P.new_model_record("1", "s", PROVIDERS, "m", "p", None))
    assert store.get(record["id"]) is None


def test_store_caps_how_much_live_state_a_chat_can_leave_behind():
    store = P.PickerStore(ttl_seconds=600, max_records=3)
    ids = [store.put(**P.new_choice_record("1", "s", "t", CHOICES, None))["id"] for _ in range(5)]
    assert store.count() == 3
    assert store.get(ids[0]) is None and store.get(ids[-1]) is not None


def test_update_of_an_unknown_picker_is_none():
    store = P.PickerStore()
    assert store.update("deadbe", page=2) is None


# ── the press path ──────────────────────────────────────────────────────────

def _press_payload(state, **over):
    payload = {"v": "mk" if state["kind"] == "model" else "cp", "id": state["id"]}
    payload.update(over)
    return json.dumps(payload)


def test_provider_press_opens_the_model_list_in_the_same_message(tmp_path):
    adapter = make_adapter(tmp_path)
    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "deepseek-v4-pro", "opencode-go", on_model_selected=_noop))
    adapter.pickers.update(state["id"], message_id=900)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e1",
            "payload": _press_payload(state, a="pr", s="openai"),
        }))
    assert adapter.client.edits[0]["message_id"] == 900
    assert "OpenAI — модели" in adapter.client.edits[0]["message"]
    assert adapter.client.answers[-1][0] == "OpenAI"
    assert adapter.pickers.get(state["id"])["view"] == "models"


def test_model_press_calls_the_core_callback_and_shows_its_reply(tmp_path):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        calls.append((chat_id, model_id, provider_slug))
        return "✓ Модель переключена: gpt-5.2 (сессия)"

    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "deepseek-v4-pro", "opencode-go", on_model_selected=on_model_selected))
    adapter.pickers.update(state["id"], message_id=901, view="models", provider="openai")
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e2",
            "payload": _press_payload(state, a="md", m="gpt-5.2"),
        }))
    assert calls == [("123456", "gpt-5.2", "openai")]
    assert "переключена" in adapter.client.edits[-1]["message"]
    assert json.loads(adapter.client.edits[-1]["keyboard"]) == {"inline": True, "buttons": []}
    assert adapter.pickers.get(state["id"])["resolved"] is True


def test_a_forged_model_id_never_reaches_the_core_callback(tmp_path):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        calls.append(model_id)
        return "ок"

    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "m", "opencode-go", on_model_selected=on_model_selected))
    adapter.pickers.update(state["id"], message_id=902, view="models", provider="openai")
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e3",
            "payload": _press_payload(state, a="md", m="claude-opus-4-5"),  # never offered by openai
        }))
    assert calls == [] and adapter.client.answers[-1][0] == "Модель недоступна"


def test_a_second_press_after_a_committed_choice_is_refused(tmp_path):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        calls.append(model_id)
        return "ок"

    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "m", "opencode-go", on_model_selected=on_model_selected))
    adapter.pickers.update(state["id"], message_id=903, view="models", provider="openai")
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e4",
            "payload": _press_payload(state, a="md", m="gpt-5.2")}))
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e5",
            "payload": _press_payload(state, a="md", m="gpt-5.2-mini")}))
    assert calls == ["gpt-5.2"]
    assert adapter.client.answers[-1][0] == "Выбор уже сделан"


def test_press_from_another_chat_is_refused(tmp_path):
    adapter = make_adapter(tmp_path)
    state = adapter.pickers.put(**P.new_model_record(
        "2000000001", "s", PROVIDERS, "m", "opencode-go", on_model_selected=_noop))
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 999, "peer_id": 2000000002, "event_id": "e6",
            "payload": _press_payload(state, a="pr", s="openai")}))
    assert adapter.client.edits == [] and adapter.client.answers[0][0] == "Выбор устарел"


def test_unknown_picker_id_answers_without_touching_anything(tmp_path):
    adapter = make_adapter(tmp_path)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e7",
            "payload": json.dumps({"v": "mk", "id": "deadbe", "a": "pr", "s": "openai"})}))
    assert adapter.client.edits == [] and adapter.client.answers[0][0] == "Выбор устарел"


def test_paging_and_back_press_rewrite_the_message(tmp_path):
    adapter = make_adapter(tmp_path)
    many = [dict(p, slug=f"p{i}", name=f"П{i}") for i, p in enumerate(PROVIDERS * 6)]
    state = adapter.pickers.put(**P.new_model_record("123456", "s", many, "m", "opencode-go", _noop))
    adapter.pickers.update(state["id"], message_id=904)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e8",
            "payload": _press_payload(state, a="pg", p=1)}))
        assert adapter.pickers.get(state["id"])["page"] == 1
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e9",
            "payload": _press_payload(state, a="bk")}))
    assert adapter.pickers.get(state["id"])["view"] == "providers"
    assert len(adapter.client.edits) == 2


def test_close_press_drops_the_picker_and_clears_the_buttons(tmp_path):
    adapter = make_adapter(tmp_path)
    state = adapter.pickers.put(**P.new_model_record("123456", "s", PROVIDERS, "m", "p", _noop))
    adapter.pickers.update(state["id"], message_id=905)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e10",
            "payload": _press_payload(state, a="x")}))
    assert adapter.pickers.get(state["id"]) is None
    assert "закрыт" in adapter.client.edits[0]["message"]
    assert json.loads(adapter.client.edits[0]["keyboard"])["buttons"] == []


def test_choice_press_hands_the_value_to_the_core_callback(tmp_path):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_choice_selected(chat_id, value):
        calls.append((chat_id, value))
        return f"Уровень: {value}"

    state = adapter.pickers.put(**P.new_choice_record(
        "123456", "s", "Уровень рассуждений", CHOICES, on_choice_selected))
    adapter.pickers.update(state["id"], message_id=906)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e11",
            "payload": _press_payload(state, a="ch", c="low")}))
    assert calls == [("123456", "low")]
    assert "Уровень: low" in adapter.client.edits[-1]["message"]
    assert adapter.pickers.get(state["id"])["resolved"] is True


def test_a_choice_value_that_was_never_offered_is_refused(tmp_path):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_choice_selected(chat_id, value):
        calls.append(value)
        return "ок"

    state = adapter.pickers.put(**P.new_choice_record("123456", "s", "T", CHOICES, on_choice_selected))
    adapter.pickers.update(state["id"], message_id=907)
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e12",
            "payload": _press_payload(state, a="ch", c="ultra")}))
    assert calls == [] and adapter.client.answers[0][0] == "Вариант недоступен"


# ── selection guard ─────────────────────────────────────────────────────────

def test_an_expensive_model_asks_for_confirmation_before_switching(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path)
    calls = []

    async def on_model_selected(chat_id, model_id, provider_slug):
        calls.append(model_id)
        return "✓ переключено"

    async def warning(model_id, provider_slug):
        return types.SimpleNamespace(title="Дорогая модель", message="Стоит как крыло самолёта")

    monkeypatch.setattr("vk.adapter.selection_warning", warning)
    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "m", "opencode-go", on_model_selected=on_model_selected))
    adapter.pickers.update(state["id"], message_id=908, view="models", provider="openai")
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e13",
            "payload": _press_payload(state, a="md", m="gpt-5.2")}))
        assert calls == [], "the guard must hold the switch until the user confirms"
        assert adapter.pickers.get(state["id"])["view"] == "confirm"
        assert "Дорогая модель" in adapter.client.edits[-1]["message"]
        # cancelling returns to the model list without touching the model
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e14",
            "payload": _press_payload(state, a="no")}))
        assert calls == [] and adapter.pickers.get(state["id"])["view"] == "models"
        # the cancel cleared the pending model, so the tap has to happen again — then confirm
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e15",
            "payload": _press_payload(state, a="md", m="gpt-5.2")}))
        assert calls == []
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e16",
            "payload": _press_payload(state, a="ok")}))
    assert calls == ["gpt-5.2"]


def test_a_callback_failure_is_shown_as_a_failed_outcome(tmp_path):
    adapter = make_adapter(tmp_path)

    async def boom(chat_id, model_id, provider_slug):
        raise RuntimeError("провайдер не ответил")

    state = adapter.pickers.put(**P.new_model_record(
        "123456", "s", PROVIDERS, "m", "opencode-go", on_model_selected=boom))
    adapter.pickers.update(state["id"], message_id=909, view="models", provider="openai")
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e16",
            "payload": _press_payload(state, a="md", m="gpt-5.2")}))
    assert adapter.client.edits[-1]["message"].startswith("✗")
    assert "провайдер не ответил" in adapter.client.edits[-1]["message"]


async def _noop(*_args, **_kwargs):
    return "ок"
