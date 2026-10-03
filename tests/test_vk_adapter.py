"""Unit tests for the VK adapter's pure logic + a stubbed inbound/outbound round trip.

Canonical run (pytest, from the Hermes runtime so `gateway.*` imports resolve):
    cd ~/.hermes/hermes-agent && venv/bin/python -m pytest ~/.hermes/plugins/platforms/vk -q

Standalone fallback, if pytest is missing from the venv (`venv/bin/pip install pytest` restores it):
    cd <hermes-install> && PYTHONPATH=$PWD venv/bin/python <plugin-dir>/tests/test_vk_adapter.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import traceback
from types import SimpleNamespace

import _paths  # noqa: E402  (registers the plugin as `vk`, whatever this directory is called)

from vk.adapter import (  # noqa: E402
    VKAdapter, _is_group, _keyboard, _peer_bool_map, _reaction_id, _vk_mention_patterns, command_keyboard,
)
from gateway.platforms.event import ProcessingOutcome  # noqa: E402
from vk.vk_markdown import VK_SAFE_ITEM_TYPES, render_chunks, to_plain, u16_len  # noqa: E402


# ── markdown → VK format_data ────────────────────────────────────────────────

def test_plain_text_has_no_format_data():
    chunks = render_chunks("просто текст")
    assert len(chunks) == 1
    text, fmt = chunks[0]
    assert text == "просто текст"
    assert fmt is None


def test_bold_and_italic_offsets():
    text, fmt = render_chunks("привет **мир** и *всё*")[0]
    assert text == "привет мир и всё"
    assert fmt == {"version": 1, "items": [
        {"type": "bold", "offset": 7, "length": 3},
        {"type": "italic", "offset": 13, "length": 3},
    ]}


def test_link_becomes_url_item():
    text, fmt = render_chunks("см. [док](https://example.com/a)")[0]
    assert text == "см. док"
    assert fmt["items"] == [{"type": "url", "offset": 4, "length": 3, "url": "https://example.com/a"}]


def test_image_markdown_keeps_url_tappable():
    text, fmt = render_chunks("![схема](https://example.com/x.png)")[0]
    assert text == "схема"
    assert fmt["items"][0]["url"] == "https://example.com/x.png"


def test_inline_code_backticks_are_stripped_without_styles():
    text, fmt = render_chunks("команда `hermes gateway restart`")[0]
    assert text.strip() == "команда hermes gateway restart"
    assert fmt is None or all(i["type"] != "bold" for i in fmt["items"])


def test_fences_and_headings_are_normalised():
    chunks = render_chunks("# Заголовок\n\n```bash\nls -la\n```\n")
    text = "\n".join(t for t, _ in chunks)
    fmt = next(f for t, f in chunks if "Заголовок" in t)
    assert "#" not in text and "```" not in text
    assert "Заголовок" in text and "ls -la" in text
    assert {"type": "bold", "offset": 0, "length": len("Заголовок")} in fmt["items"]


def test_underscore_italics_do_not_eat_snake_case():
    text, fmt = render_chunks("файл some_long_name.txt и _курсив_")[0]
    assert "some_long_name.txt" in text          # intra-word underscores survive
    assert "_курсив_" not in text                # markup markers are consumed
    assert "курсив" in text and fmt["items"] == [{"type": "italic", "offset": text.index("курсив"), "length": 6}]


def test_emoji_offsets_are_utf16_units():
    text, fmt = render_chunks("🚀 **ok**")[0]
    assert text == "🚀 ok"
    # rocket = 1 char but 2 UTF-16 units; the space adds 1 → bold starts at 3, not 2
    assert fmt["items"][0]["offset"] == 3
    assert u16_len("🚀 ") == 3


def test_blank_header_kv_table_becomes_heading_and_bullets():
    """The live regression: a model's ``|key|value|`` table with an empty header row.

    Delivered to VK verbatim it read ``|||`` / ``|---|---|`` / ``|Статус|раскатка завершена|`` — pipes and
    dashes on a phone screen. It must become a heading plus bullets, and no pipe may survive.
    """
    source = (
        "|||\n|---|---|\n"
        "|Статус|раскатка завершена (17 файлов)|\n"
        "|Лог|/path/to/deploy.log — переписан|\n"
    )
    text, fmt = render_chunks(source)[0]
    assert "|" not in text and "---" not in text
    assert text == ("Статус\n• раскатка завершена (17 файлов)\n\n"
                    "Лог\n• /path/to/deploy.log — переписан")
    bold = [item for item in fmt["items"] if item["type"] == "bold"]
    assert len(bold) == 2 and bold[0]["offset"] == 0 and bold[0]["length"] == len("Статус")


def test_named_header_table_drops_the_heading_bullet():
    source = ("| Сервис | Адрес | Статус |\n|---|---|---|\n"
              "| api | 127.0.0.1:8000 | ok |\n| worker | queue | ok |\n")
    text = render_chunks(source)[0][0]
    assert text == ("api\n• Адрес: 127.0.0.1:8000\n• Статус: ok\n\n"
                    "worker\n• Адрес: queue\n• Статус: ok")


def test_row_label_column_shapes():
    """Two header/cell shapes the models actually emit, all mapping names onto VALUES."""
    # extra leading label column with an empty header → first column becomes the heading
    assert render_chunks("| | Размер | Сумма |\n|---|---|---|\n| архив | 12 МБ | a1b2c3 |")[0][0] == \
        "архив\n• Размер: 12 МБ\n• Сумма: a1b2c3"
    # header names the label column itself → it is still the heading, not a bullet
    assert render_chunks("| Артефакт | Размер |\n|---|---|\n| архив | 12 МБ |")[0][0] == "архив\n• Размер: 12 МБ"
    # NOTE: a single-column table (``| Размер |`` / ``|---|``) is deliberately NOT detected — the
    # framework's separator rule requires at least two dash cells so a lone ``---`` rule never
    # matches, and VK keeps that behaviour identical to Discord/Telegram.


def test_stray_pipes_and_fenced_tables_are_left_alone():
    assert render_chunks("a | b без разделителя")[0][0] == "a | b без разделителя"
    fenced = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    text = render_chunks(fenced)[0][0]
    assert "| a | b |" in text and "---" in text  # code stays code, even if it looks like a table


def test_nested_emphasis_closes_from_the_end_of_the_run():
    """``**жирный с *курсивом***`` must not leak markers (live sample shipped ``*курсивом*``)."""
    text, fmt = render_chunks("**жирный с *курсивом***")[0]
    assert text == "жирный с курсивом" and "*" not in text
    bold = [(i["offset"], i["length"]) for i in fmt["items"] if i["type"] == "bold"]
    italic = [(i["offset"], i["length"]) for i in fmt["items"] if i["type"] == "italic"]
    assert bold == [(0, 9), (9, 8)], bold
    assert italic == [(9, 8)], italic


def test_quote_lines_become_italic_without_the_marker():
    text, fmt = render_chunks("> цитирую тебя\n\nобычный текст")[0]
    assert ">" not in text
    assert text.startswith("цитирую тебя")
    assert fmt["items"][0] == {"type": "italic", "offset": 0, "length": len("цитирую тебя")}


def test_never_emits_an_item_type_vk_voids():
    """Regression: a single unsupported item type makes VK drop the WHOLE format_data object.

    Verified live: bold+italic was stored, ``strike`` alone was stored as null. Markers must
    therefore still be consumed (no literal ``~~``/``__`` left in the text) without emitting
    anything outside the allowlist.
    """
    text, fmt = render_chunks("~~зачёркнутый~~ и **жирный** и _курсив_")[0]
    assert "~" not in text and "зачёркнутый" in text
    types = {item["type"] for item in (fmt or {}).get("items", [])}
    assert types == {"bold", "italic"}, types
    assert types <= VK_SAFE_ITEM_TYPES
    # strike-only must now yield NO format_data at all rather than a poisoned one
    _, only = render_chunks("~~только зачёркнутый~~")[0]
    assert only is None


def test_chunking_respects_limit_and_keeps_spans_inside():
    source = "\n\n".join(f"**Пункт {i}** " + "текст " * 30 for i in range(12))
    chunks = render_chunks(source, limit=600)
    assert len(chunks) > 1
    for text, fmt in chunks:
        assert u16_len(text) <= 600
        for item in (fmt or {}).get("items", []):
            assert item["offset"] + item["length"] <= u16_len(text)
            assert item["type"] in VK_SAFE_ITEM_TYPES


def test_to_plain_drops_all_markup():
    plain = to_plain("## Итог\n\n- **важно**: [ссылка](https://x.y)")
    assert plain.splitlines()[0] == "Итог"
    assert "**" not in plain and "](" not in plain
    assert "• важно: ссылка" in plain


# ── keyboards and peer id math ───────────────────────────────────────────────

def test_group_vs_dm_peer_ids():
    assert _is_group(2_000_000_001) is True
    assert _is_group(123456) is False


def test_keyboard_payload_is_compact_and_bounded():
    rows = [[("1", {"v": "cl", "id": "abc123", "c": "0"}, "secondary"),
             ("x" * 100, {"v": "ea", "id": "d" * 12, "c": "deny"}, "negative")]]
    kb = json.loads(_keyboard(rows))
    assert kb["inline"] is True
    assert len(kb["buttons"]) == 1
    first = kb["buttons"][0][0]["action"]
    assert first["type"] == "callback"
    assert first["payload"] == '{"v":"cl","id":"abc123","c":"0"}'
    assert len(kb["buttons"][0][1]["action"]["label"]) <= 40
    assert kb["buttons"][0][1]["color"] == "negative"


def test_keyboard_returns_none_without_buttons():
    assert _keyboard([]) is None


# ── adapter behaviour with a stubbed transport ───────────────────────────────

class FakeClient:
    def __init__(self):
        self.sent = []
        self.answers = []
        self.edits = []
        self.reactions = []
        self.reaction_deletes = []
        self.group_id = -777
        self.group_name = "Тестовое сообщество"

    async def send_message(self, peer_id, message, **kwargs):
        self.sent.append({"peer_id": peer_id, "message": message, **kwargs})
        return 1000 + len(self.sent)

    async def edit_message(self, peer_id, message_id, message, **kwargs):
        self.edits.append({"peer_id": peer_id, "message_id": message_id, "message": message, **kwargs})

    async def send_reaction(self, peer_id, cmid, reaction_id):
        self.reactions.append((peer_id, cmid, reaction_id))
        return True

    async def delete_reaction(self, peer_id, cmid):
        self.reaction_deletes.append((peer_id, cmid))
        return True

    async def answer_event(self, event_id, user_id, peer_id, text):
        self.answers.append((text, event_id))

    async def user_names(self, ids):
        return {i: "Иван" for i in ids}

    async def chat_title(self, peer_id):
        return "Рабочая беседа"

    async def set_activity(self, peer_id, activity="typing"):
        self.activity = (peer_id, activity)

    async def download(self, url, **kwargs):
        # inbound media: any bytes are fine, the cache write is stubbed per test
        return b"\x89PNG\r\n\x1a\n" + b"0" * 16


def make_adapter(extra=None) -> VKAdapter:
    from gateway.config import Platform
    from gateway.platform_registry import platform_registry
    if not platform_registry.is_registered("vk"):
        # In production the plugin's register() ran first, so Platform("vk") resolves through the
        # registry; standalone tests have to create the same dynamic member.
        Platform._add_pseudo_member("vk")
    from gateway.config import PlatformConfig
    try:
        config = PlatformConfig(extra=extra or {})
    except TypeError:  # older/newer dataclass shape
        config = SimpleNamespace(extra=extra or {}, enabled=True)
    adapter = VKAdapter(config)
    adapter.client = FakeClient()
    return adapter


@contextlib.contextmanager
def open_loop():
    """A fresh event loop that is always closed.

    Every await in these tests goes through here: a leaked loop trips ``filterwarnings = error``
    (``ResourceWarning: unclosed event loop``) — measured, not theoretical.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def test_inbound_dm_builds_event_and_dedupes():
    adapter = make_adapter()
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    message = {
        "id": 555, "date": 1_700_000_000, "peer_id": 123456, "from_id": 123456,
        "text": "привет", "out": 0, "attachments": [],
    }
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_inbound(message, update_id="evt-1"))
        assert len(captured) == 1
        event = captured[0]
        assert event.text == "привет"
        assert event.source.chat_type == "dm"
        assert event.source.chat_id == "123456"
        assert event.source.user_name == "Иван"

        # Redelivery of the SAME update must be dropped... (awaiting is the whole point: a bare call
        # creates a coroutine nobody runs, which made this assertion vacuous until pytest flagged it.)
        loop.run_until_complete(adapter._handle_inbound(message, update_id="evt-1"))
        assert len(captured) == 1
        # ...while a NEW update id must still get through — that proves the drop above was the
        # deduplication guard doing its job, not the handler silently skipping the message.
        loop.run_until_complete(adapter._handle_inbound({**message, "id": 556}, update_id="evt-2"))
    assert [e.text for e in captured] == ["привет", "привет"]


def test_outgoing_and_empty_messages_are_ignored():
    adapter = make_adapter()
    captured = []
    adapter.handle_message = lambda event: captured.append(event)  # noqa: E731
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_inbound(
            {"id": 1, "peer_id": 123456, "from_id": 123456, "text": "эхо", "out": 1}, update_id="a"))
        loop.run_until_complete(adapter._handle_inbound({"id": 2, "out": 0}, update_id="b"))
    assert captured == []


def test_group_message_gets_quoted_on_reply():
    adapter = make_adapter()
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_inbound(
            {"id": 777, "date": 1_700_000_000, "peer_id": 2_000_000_042, "from_id": 123456,
             "text": "вопрос", "out": 0, "attachments": []}, update_id="g1"))
        assert captured[0].source.chat_type == "group"
        assert captured[0].source.chat_name == "Рабочая беседа"

        result = loop.run_until_complete(adapter.send("2000000042", "ответ"))
        assert result.success
        assert adapter.client.sent[0]["reply_to"] == 777

        result = loop.run_until_complete(adapter.send("123456", "личный ответ"))
        assert result.success
        assert adapter.client.sent[1]["reply_to"] is None  # DMs are never quoted


def test_send_chunks_long_answers():
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send("123456", "абв " * 1200))
    assert result.success
    assert len(adapter.client.sent) > 1
    assert all(len(call["message"]) <= VKAdapter.MAX_MESSAGE_LENGTH for call in adapter.client.sent)


def test_button_press_resolves_approval_and_answers_event():
    adapter = make_adapter()
    adapter._approval_state["abc"] = "session-key"
    resolved = {}
    import tools.approval as approval_mod
    original = approval_mod.resolve_gateway_approval
    approval_mod.resolve_gateway_approval = lambda key, choice: resolved.update(key=key, choice=choice) or 1
    try:
        with open_loop() as loop:
            loop.run_until_complete(adapter._handle_button_event({
                "user_id": 123456, "peer_id": 123456, "event_id": "e1",
                "payload": json.dumps({"v": "ea", "id": "abc", "c": "once"}),
            }))
    finally:
        approval_mod.resolve_gateway_approval = original
    assert resolved == {"key": "session-key", "choice": "once"}
    assert adapter.client.answers and "Разрешено" in adapter.client.answers[0][0]
    assert "abc" not in adapter._approval_state  # a button is single-use


def test_button_press_from_unauthorized_sender_is_refused():
    adapter = make_adapter()
    adapter._is_sender_authorized = lambda *a, **k: False
    adapter._approval_state["abc"] = "session-key"
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 999, "peer_id": 999, "event_id": "e2",
            "payload": json.dumps({"v": "ea", "id": "abc", "c": "once"}),
        }))
    assert adapter._approval_state["abc"] == "session-key"
    assert adapter.client.answers[0][0] == "Недостаточно прав"


def test_unknown_button_payload_answers_with_snackbar_only():
    """The path a stale/unknown press takes: snackbar + log, and NO chat message.

    Live confusion this encodes: a probe tap produced "нет сообщения" because a VK callback answer is a
    transient popup, not a message — pinning it keeps the behaviour intentional rather than accidental.
    """
    adapter = make_adapter()
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e9",
            "payload": json.dumps({"v": "probe", "id": "ch1"}),
        }))
    assert adapter.client.answers == [("Кнопка устарела", "e9")]
    assert adapter.client.sent == []


def test_answer_event_failure_is_logged_not_fatal():
    adapter = make_adapter()

    async def boom(*args, **kwargs):
        raise RuntimeError("912 chat bot feature")

    adapter.client.answer_event = boom
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_button_event({
            "user_id": 123456, "peer_id": 123456, "event_id": "e10",
            "payload": json.dumps({"v": "probe", "id": "ch2"}),
        }))  # must not raise: a failed answer is reported, the poll loop keeps running


def test_standalone_send_accepts_host_media_tuples():
    """Regression: the host passes ``(path, is_voice)`` tuples, not bare paths.

    Live failure it encodes: ``stat: path should be string, bytes, os.PathLike or integer, not tuple``
    from ``hermes send --to vk:<id> "caption MEDIA:/tmp/x.png"``.
    """
    import tempfile as _tempfile
    import vk.adapter as adapter_mod

    png = os.path.join(_tempfile.mkdtemp(prefix="vk-test-"), "chart.png")
    with open(png, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    uploaded, sent = [], []

    class FakeClient:
        def __init__(self, token, api_version=None):
            self.token = token

        async def resolve_group(self):
            return (777, "Mock")

        async def send_message(self, peer_id, message, **kwargs):
            sent.append({"peer_id": peer_id, "message": message, **kwargs})
            return 1

        async def upload_photo(self, data, filename="image.jpg"):
            uploaded.append(("photo", os.path.basename(filename), len(data)))
            return "photo-777_1"

        async def upload_document(self, data, filename, kind="doc"):
            uploaded.append((kind, os.path.basename(filename), len(data)))
            return "doc-777_2"

        async def close(self):
            pass

    original = adapter_mod.VkClient
    adapter_mod.VkClient = FakeClient
    try:
        with open_loop() as loop:
            result = loop.run_until_complete(adapter_mod._standalone_send(
                SimpleNamespace(extra={"token": "vk1.a.MOCK"}), "1234567", "отчёт готов",
                media_files=[(png, False), ("/nonexistent/missing.pdf", False)]))
    finally:
        adapter_mod.VkClient = original
    assert result.get("success") is True, result
    assert uploaded == [("photo", "chart.png", os.path.getsize(png))], uploaded
    # One message, not two: VK carries text and an attachment together, so the single-chunk
    # caption rides along with the photo; the missing path is skipped rather than fatal.
    assert len(sent) == 1, sent
    assert sent[0]["message"] == "отчёт готов" and sent[0]["attachment"] == "photo-777_1"


def test_out_of_range_clarify_index_is_refused_rather_than_answered():
    """An index the prompt never offered must not reach the agent as an answer.

    The removed fallback turned a stale press into the literal ``"choice N"``, and
    ``resolve_gateway_clarify`` stores that string verbatim as the user's reply — so the agent would act
    on text the user never sent. Refusing is the only honest outcome.
    """
    import tools.clarify_gateway as clarify_mod

    adapter = make_adapter()
    clarify_mod._entries["cid-9"] = SimpleNamespace(choices=["один", "два"])
    resolved = []
    original = clarify_mod.resolve_gateway_clarify
    clarify_mod.resolve_gateway_clarify = lambda cid, text: resolved.append((cid, text)) or True
    try:
        with open_loop() as loop:
            loop.run_until_complete(adapter._handle_button_event({
                "user_id": 123456, "peer_id": 123456, "event_id": "e11",
                "payload": json.dumps({"v": "cl", "id": "cid-9", "c": "9"}),
            }))
    finally:
        clarify_mod.resolve_gateway_clarify = original
        clarify_mod._entries.pop("cid-9", None)
    assert resolved == [], f"a phantom choice reached the resolver: {resolved}"
    assert adapter.client.answers == [("Некорректный вариант", "e11")], adapter.client.answers


def test_in_range_clarify_index_resolves_with_the_label_not_the_number():
    """The complement: a valid press must send the choice TEXT (index order, 0-based, as the keyboard
    builder emits it), because the resolver passes the string through untouched."""
    import tools.clarify_gateway as clarify_mod

    adapter = make_adapter()
    clarify_mod._entries["cid-10"] = SimpleNamespace(choices=["один", "два"])
    resolved = []
    original = clarify_mod.resolve_gateway_clarify
    clarify_mod.resolve_gateway_clarify = lambda cid, text: resolved.append((cid, text)) or True
    try:
        with open_loop() as loop:
            loop.run_until_complete(adapter._handle_button_event({
                "user_id": 123456, "peer_id": 123456, "event_id": "e12",
                "payload": json.dumps({"v": "cl", "id": "cid-10", "c": "1"}),
            }))
    finally:
        clarify_mod.resolve_gateway_clarify = original
        clarify_mod._entries.pop("cid-10", None)
    assert resolved == [("cid-10", "два")], resolved
    assert adapter.client.answers == [("✓ два", "e12")], adapter.client.answers


def test_slash_command_text_stays_bare_while_chat_keeps_the_note():
    """A command sent with an attachment must arrive as the bare command.

    Notes about attachments/geo/forwards share the message text so the agent sees what arrived; for a
    slash command that would turn ``/new`` into ``/new\\n[фото]`` and hand the note to the command
    handler as its argument (session name for /new, title for /title, target for /save …). All three
    note sources are covered, plus the chat case that must keep its note.
    """
    import vk.adapter as adapter_mod

    adapter = make_adapter()
    captured = []

    async def capture(event):
        captured.append(event)

    adapter.handle_message = capture
    base = {"date": 1_700_000_000, "peer_id": 123456, "from_id": 123456, "out": 0}

    def post(message_id, text, **extra):
        with open_loop() as loop:
            loop.run_until_complete(adapter._handle_inbound(
                {**base, "id": message_id, "text": text, **extra}, update_id=f"c{message_id}"))
        return captured[-1]

    geo = {"geo": {"coordinates": {"latitude": 0.0, "longitude": 0.0}}}
    photo = {"attachments": [{"type": "photo",
                              "photo": {"sizes": [{"width": 10, "height": 10, "url": "http://x/1.jpg"}]}}]}

    assert post(901, "/new", **geo).text == "/new"
    assert post(902, "/help", fwd_messages=[{"id": 1}]).text == "/help"
    assert post(903, "привет", **geo).text == "привет\n[геопозиция]"  # chat keeps its note

    original = adapter_mod.cache_image_from_bytes
    adapter_mod.cache_image_from_bytes = lambda data, ext=".jpg": "/cache/stub.jpg"
    try:
        event = post(904, "/new", **photo)
    finally:
        adapter_mod.cache_image_from_bytes = original
    assert event.text == "/new", event.text
    assert event.media_urls == ["/cache/stub.jpg"], event.media_urls  # the file itself still travels


def test_command_keyboard_is_a_persistent_bot_keyboard():
    """VK's substitute for `/` autocomplete: text buttons that send their own label.

    It must be a *bot* keyboard (no ``inline``) and persistent (``one_time`` false), otherwise VK either
    ties it to a single message or hides it after the first tap.
    """
    payload = json.loads(command_keyboard())
    assert payload["inline"] is False and payload["one_time"] is False
    rows = [[button["action"]["label"] for button in row] for row in payload["buttons"]]
    assert rows == [["/help", "/status"], ["/new", "/stop"]]
    assert all(button["action"]["type"] == "text" for row in payload["buttons"] for button in row)
    assert all(len(label) <= 40 for row in rows for label in row)
    # the payload is what lets a tap be identified in the inbound event
    assert [json.loads(b["action"]["payload"])["cmd"] for row in payload["buttons"] for b in row] == \
        ["help", "status", "new", "stop"]


def test_command_keyboard_is_attached_only_when_enabled():
    """Off by default (it occupies the space above the input); the client drops ``keyboard=None``.

    The environment variable deliberately wins over config, so an install whose profile sets
    ``VK_COMMAND_KEYBOARD=true`` (a real one does) would flip the "off" half of this test. Clear it for
    the assertion, restore it after — a test that only passes on a pristine environment is useless.
    """
    saved = os.environ.pop("VK_COMMAND_KEYBOARD", None)
    try:
        off, on = make_adapter(), make_adapter(extra={"command_keyboard": True})
        with open_loop() as loop:
            loop.run_until_complete(off.send("123456", "просто текст"))
            loop.run_until_complete(on.send("123456", "просто текст"))
        assert off.client.sent[0].get("keyboard") is None
        assert on.client.sent[0]["keyboard"] == command_keyboard()
        # the convention itself: env beats config, even when config asks for the keyboard
        os.environ["VK_COMMAND_KEYBOARD"] = "false"
        env_off = make_adapter(extra={"command_keyboard": True})
        with open_loop() as loop:
            loop.run_until_complete(env_off.send("123456", "просто текст"))
        assert env_off.client.sent[0].get("keyboard") is None
    finally:
        os.environ.pop("VK_COMMAND_KEYBOARD", None)
        if saved is not None:
            os.environ["VK_COMMAND_KEYBOARD"] = saved


def test_standalone_send_attaches_command_keyboard_when_enabled():
    """Cron reports and `hermes send` land in the same chat, so they must carry the keyboard too —
    a message without it can leave the client without the buttons the user turned on."""
    import vk.adapter as adapter_mod

    sent = []

    class FakeClient:
        def __init__(self, token, api_version=None):
            self.token = token

        async def resolve_group(self):
            return (777, "Mock")

        async def send_message(self, peer_id, message, **kwargs):
            sent.append({"message": message, **kwargs})
            return 1

        async def close(self):
            pass

    original = adapter_mod.VkClient
    adapter_mod.VkClient = FakeClient
    saved = os.environ.pop("VK_COMMAND_KEYBOARD", None)   # the env wins over config — see the sibling test
    try:
        with open_loop() as loop:
            loop.run_until_complete(adapter_mod._standalone_send(
                SimpleNamespace(extra={"token": "vk1.a.MOCK", "command_keyboard": True}),
                "1234567", "отчёт готов"))
            on = sent[-1]
            loop.run_until_complete(adapter_mod._standalone_send(
                SimpleNamespace(extra={"token": "vk1.a.MOCK"}), "1234567", "отчёт готов"))
            off = sent[-1]
    finally:
        adapter_mod.VkClient = original
        if saved is not None:
            os.environ["VK_COMMAND_KEYBOARD"] = saved
    assert on["keyboard"] == command_keyboard()
    assert off.get("keyboard") is None


# ── group gating: who the community answers in a chat ────────────────────────

GROUP_PEER = 2_000_000_001  # VK spells a group chat as 2000000000 + chat_id
GROUP_MSG = {
    "id": 900, "date": 1_700_000_000, "peer_id": GROUP_PEER, "from_id": 123456,
    "text": "привет", "out": 0, "attachments": [],
}


class RecordingClient(FakeClient):
    """FakeClient that remembers every download and enforces the cap the way the real client does."""

    def __init__(self, payload: bytes = b"\x89PNG\r\n\x1a\n"):
        super().__init__()
        self.payload = payload
        self.downloads = []

    async def download(self, url, *, max_bytes: int = 0, **kwargs):
        self.downloads.append((url, max_bytes))
        if max_bytes and len(self.payload) > max_bytes:
            from vk.vk_api import VkApiError
            raise VkApiError("download", 0, f"file exceeds {max_bytes} bytes")
        return self.payload


def with_mentions(adapter, group_id: int = 241965111, name: str = "Еремей"):
    """Build the mention patterns exactly as connect() does once resolve_group() has answered."""
    from gateway.platforms.helpers import compile_mention_patterns
    adapter._mention_patterns = compile_mention_patterns(
        adapter.mention_patterns_raw, log_prefix="vk", defaults=_vk_mention_patterns(group_id, name))
    return adapter


def capture_events(adapter):
    seen = []

    async def capture(event):
        seen.append(event)

    adapter.handle_message = capture
    return seen


def run_inbound(adapter, message, update_id: str = "evt"):
    with open_loop() as loop:
        loop.run_until_complete(adapter._handle_inbound(message, update_id=update_id))


def _matches(patterns, text: str) -> bool:
    return any(re.compile(pattern, re.IGNORECASE).search(text) for pattern in patterns)


def test_peer_bool_map_parses_env_and_config_and_drops_typos():
    assert _peer_bool_map("123:true,456:false,789:maybe,abc:true") == {123: True, 456: False}
    assert _peer_bool_map({"1": "on", "2": "off"}) == {1: True, 2: False}
    assert _peer_bool_map(None) == {}
    assert _peer_bool_map("") == {}


def test_mention_patterns_match_both_id_forms_and_the_community_name():
    patterns = _vk_mention_patterns(241965111, "Еремей")
    assert _matches(patterns, "[club241965111|Еремей] посчитай итоги")
    assert _matches(patterns, "эй @club241965111, ты тут?")
    assert _matches(patterns, "Еремей, посчитай")
    assert not _matches(patterns, "просто сообщение про ремонт")
    # A two-letter community name must not become a wake word: it would match unrelated words.
    assert not _matches(_vk_mention_patterns(241965111, "Вк"), "вклад в банке")


def test_group_message_is_silent_when_a_mention_is_required():
    adapter = with_mentions(make_adapter({"require_mention": True}))
    seen = capture_events(adapter)
    run_inbound(adapter, GROUP_MSG, "g1")
    assert seen == []


def test_group_message_with_a_mention_is_answered_and_the_mention_is_stripped():
    adapter = with_mentions(make_adapter({"require_mention": True}))
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "text": "[club241965111|Еремей] посчитай итоги"}, "g2")
    assert len(seen) == 1
    assert seen[0].text == "посчитай итоги"


def test_dm_is_never_mention_gated():
    adapter = with_mentions(make_adapter({"require_mention": True}))
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "peer_id": 123456, "from_id": 123456}, "g3")
    assert len(seen) == 1  # a direct message is addressed to the bot by definition


def test_group_still_answers_everything_when_the_gate_is_off():
    """The default must not change: requiring a mention is strictly opt-in."""
    adapter = with_mentions(make_adapter())
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "text": "просто болтовня"}, "g4")
    assert len(seen) == 1


def test_per_chat_override_beats_the_global_flag_in_both_directions():
    opted_out = with_mentions(make_adapter({
        "require_mention": True, "require_mention_by_peer": {str(GROUP_PEER): False}}))
    seen = capture_events(opted_out)
    run_inbound(opted_out, GROUP_MSG, "g5")
    assert len(seen) == 1  # global on, this chat opted out

    opted_in = with_mentions(make_adapter({
        "require_mention": False, "require_mention_by_peer": {str(GROUP_PEER): True}}))
    silent = capture_events(opted_in)
    run_inbound(opted_in, GROUP_MSG, "g6")
    assert silent == []  # global off, this chat opted in


# ── inbound media: the switch and the cap ────────────────────────────────────

def test_inbound_attachment_is_not_downloaded_when_the_switch_is_off():
    adapter = make_adapter({"download_attachments": False})
    adapter.client = RecordingClient()
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "photo", "photo": {"sizes": [{"width": 100, "url": "https://sun9-1.userapi.com/x.jpg"}]}}]}, "d1")
    assert adapter.client.downloads == []
    assert seen[0].media_urls == []
    assert "[фото]" in seen[0].text  # the agent still learns what arrived


def test_attachment_above_the_cap_is_reported_and_not_attached():
    adapter = make_adapter({"max_attachment_bytes": 8})
    adapter.client = RecordingClient(payload=b"0" * 64)
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "doc", "doc": {"url": "https://example.com/big.pdf", "title": "big.pdf"}}]}, "d2")
    assert [cap for _, cap in adapter.client.downloads] == [8]  # the configured cap travelled, not 20 MiB
    assert seen[0].media_urls == []
    assert "не удалось загрузить" in seen[0].text


# ── the exec-approval card the core renders through us ───────────────────────

def _payloads(keyboard) -> list:
    """Every callback payload in a keyboard, whatever nesting the builder uses.

    ``_keyboard`` hands VK a JSON *string*, so a raw walk would silently find nothing — which is
    exactly how a card that never built looked identical to a card nobody inspected.
    """
    if isinstance(keyboard, str):
        keyboard = json.loads(keyboard)
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "payload" in node:
                found.append(json.loads(node["payload"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(keyboard)
    return found


def test_exec_approval_card_carries_every_choice_bound_to_one_prompt():
    """The core calls this through ``BasePlatformAdapter.send_exec_approval``; a broken card means a
    dangerous command waits for text nobody types, so pin the contract: four choices, one id, the
    session key stored against it."""
    from gateway.platforms.base import ExecApprovalPrompt
    adapter = make_adapter()
    prompt = ExecApprovalPrompt(
        chat_id="123456", session_key="sess-42", text="Запустить опасную команду?",
        actions=[("Разрешить один раз", "once", "primary"), ("Разрешить на сессию", "session", ""),
                 ("Разрешить всегда", "always", ""), ("Запретить", "deny", "danger")],
        command="rm -rf /tmp/x", description="удаление файлов", smart_denied=False)
    with open_loop() as loop:
        result = loop.run_until_complete(adapter._send_exec_approval_prompt(prompt))
    assert result.success
    payloads = _payloads(adapter.client.sent[-1]["keyboard"])
    assert [p["c"] for p in payloads] == ["once", "session", "always", "deny"]
    assert all(p["v"] == "ea" for p in payloads)
    ids = {p["id"] for p in payloads}
    assert len(ids) == 1                       # one prompt, one id — presses cannot cross prompts
    assert set(adapter._approval_state) == ids
    assert adapter._approval_state[ids.pop()] == "sess-42"


def test_clarify_card_renders_one_button_per_choice_plus_free_text():
    """Same failure mode as the approval card: a malformed keyboard silently degrades to plain text."""
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send_clarify(
            "123456", "Какой вариант?", ["первый", "второй"], "cl-1", "sess-7"))
    assert result.success
    payloads = _payloads(adapter.client.sent[-1]["keyboard"])
    assert [p["c"] for p in payloads] == ["0", "1", "other"]
    assert all(p["v"] == "cl" and p["id"] == "cl-1" for p in payloads)
    assert adapter._clarify_state["cl-1"] == "sess-7"


def test_slash_confirm_card_renders_all_three_choices_in_one_row():
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send_slash_confirm(
            "123456", "Подтверждение", "Выполнить /restart?", "sess-8", "sc-1"))
    assert result.success
    payloads = _payloads(adapter.client.sent[-1]["keyboard"])
    assert [p["c"] for p in payloads] == ["once", "always", "cancel"]
    assert adapter._slash_confirm_state["sc-1"] == "sess-8"


# ── editing a sent message in place ─────────────────────────────────────────

def test_edit_message_rewrites_plain_text_in_place():
    """``messages.edit`` takes no ``format_data``, so markup must be flattened, not sent verbatim."""
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.edit_message("123456", "1000", "привет **мир**"))
    assert result.success and result.message_id == "1000"
    assert adapter.client.edits == [{"peer_id": 123456, "message_id": 1000, "message": "привет мир"}]


def test_edit_message_reports_content_that_cannot_fit_one_message():
    """An edit cannot split, so over-long content is handed back for the caller to send anew."""
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.edit_message("123456", "1000", "абв " * 1200))
    assert not result.success
    assert "one VK message" in (result.error or "")
    assert adapter.client.edits == []


def test_edit_message_refuses_invalid_ids_without_calling_the_api():
    adapter = make_adapter()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.edit_message("не-id", "1000", "текст"))
    assert not result.success
    assert adapter.client.edits == []


# ── reactions: opt-in acks driven by the core's lifecycle hooks ──────────────

REACTION_MSG = {**GROUP_MSG, "id": 910, "conversation_message_id": 4567}


def _event_for(adapter, message):
    """Drive one inbound message and hand back the event the hooks receive."""
    seen = capture_events(adapter)
    run_inbound(adapter, message, f"r{message['id']}")
    assert seen, "сообщение должно было дойти до агента"
    return seen[0]


def test_reaction_ids_parse_with_a_default_for_junk():
    assert _reaction_id(None, 4) == 4
    assert _reaction_id("", 4) == 4
    assert _reaction_id("9", 4) == 9
    assert _reaction_id("0", 4) == 0          # 0 is meaningful: the step is off
    assert _reaction_id("что-то", 4) == 4
    assert _reaction_id("-3", 4) == 4


def test_reactions_are_off_by_default():
    adapter = make_adapter()
    event = _event_for(adapter, REACTION_MSG)
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(event))
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
    assert adapter.client.reactions == [] and adapter.client.reaction_deletes == []


def test_progress_reaction_addresses_the_inbound_message_by_cmid():
    """VK reactions use ``cmid``; sending the message id instead would fail with error 100."""
    adapter = make_adapter({"reactions_enabled": True})
    event = _event_for(adapter, REACTION_MSG)
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(event))
    assert adapter.client.reactions == [(GROUP_PEER, 4567, 10)]


def test_final_reaction_replaces_the_ack_with_the_outcome():
    adapter = make_adapter({"reactions_enabled": True})
    event = _event_for(adapter, REACTION_MSG)
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(event))
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.FAILURE))
    assert adapter.client.reactions == [(GROUP_PEER, 4567, 10), (GROUP_PEER, 4567, 4), (GROUP_PEER, 4567, 8)]


def test_cancelled_turn_is_left_without_a_reaction():
    adapter = make_adapter({"reactions_enabled": True})
    event = _event_for(adapter, REACTION_MSG)
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED))
    assert adapter.client.reaction_deletes == [(GROUP_PEER, 4567)]
    assert adapter.client.reactions == []


def test_reaction_steps_are_configurable_and_zero_turns_one_off():
    adapter = make_adapter({"reactions_enabled": True, "reaction_progress": 0, "reaction_ok": 6})
    event = _event_for(adapter, REACTION_MSG)
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(event))
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS))
    assert adapter.client.reactions == [(GROUP_PEER, 4567, 6)]  # only the configured final step


def test_reaction_is_skipped_when_the_message_has_no_cmid():
    adapter = make_adapter({"reactions_enabled": True})
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "id": 911}, "r-no-cmid")  # no conversation_message_id
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(seen[0]))
        loop.run_until_complete(adapter.on_processing_complete(seen[0], ProcessingOutcome.SUCCESS))
    assert adapter.client.reactions == [] and adapter.client.reaction_deletes == []


def test_rejected_reaction_does_not_disturb_the_turn():
    """Errors 1009/1010/1011 are setup facts about the community's reaction map, not failures."""
    from vk.vk_api import VkApiError as _Err
    adapter = make_adapter({"reactions_enabled": True})
    event = _event_for(adapter, REACTION_MSG)

    async def reject(*args, **kwargs):
        raise _Err("messages.sendReaction", 1010, "This reaction has been disabled")

    adapter.client.send_reaction = reject
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_start(event))  # must not raise


def test_missing_delete_reaction_is_remembered_not_retried():
    """An older API answers ``deleteReaction`` with error 3; later calls then skip it."""
    from vk.vk_api import VkApiError as _Err
    adapter = make_adapter({"reactions_enabled": True})
    event = _event_for(adapter, REACTION_MSG)
    calls = {"n": 0}

    async def boom(peer_id, cmid):
        calls["n"] += 1
        raise _Err("messages.deleteReaction", 3, "Unknown method passed")

    adapter.client.delete_reaction = boom
    with open_loop() as loop:
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED))
        loop.run_until_complete(adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED))
    assert calls["n"] == 1                     # the second call is skipped, not retried
    assert adapter._delete_reaction_supported is False


if __name__ == "__main__":
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures = []
    for name, fn in tests:
        try:
            fn()
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"FAIL {name}")
        else:
            print(f"ok   {name}")
    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    for name, tb in failures:
        print(f"\n=== {name} ===\n{tb}")
    sys.exit(1 if failures else 0)
