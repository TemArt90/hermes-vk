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
import pathlib
import re
import shutil
import sys
import time
import traceback
from types import SimpleNamespace

import _paths  # noqa: E402  (registers the plugin as `vk`, whatever this directory is called)

# Tests must not inherit the developer's profile environment. The plugin reads VK_* from os.environ
# first (by design), so a `VK_REACTIONS_ENABLED=true` sitting in ~/.hermes/.env silently flips every
# test that asserts an off-by-default branch — measured, not theoretical: that is exactly how this
# suite went red locally while CI, which has no profile, stayed green.
for _inherited in [name for name in list(os.environ) if name.startswith("VK_")]:
    os.environ.pop(_inherited, None)

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
        self.doc_uploads = []
        self.photo_uploads = []
        self.group_id = -777
        self.group_name = "Тестовое сообщество"
        self.conversations: list = []
        self.conversations_calls = 0
        self.video_uploads: list = []
        self.video_lookups: list = []

    async def get_video_file(self, video, **kwargs):
        self.video_lookups.append(dict(video))
        return {"url": "https://cdn.example/v.mp4", "title": video.get("title") or "video",
                "duration": int(video.get("duration") or 0), "ext": ".mp4"}

    async def upload_video(self, data, filename, **kwargs):
        self.video_uploads.append((filename, len(data)))
        return "video-777_5"

    async def get_conversations(self, *, count=20):
        self.conversations_calls += 1
        return list(self.conversations)

    async def upload_photo(self, data, filename="image.jpg"):
        self.photo_uploads.append((filename, len(data)))
        return "photo-777_1"

    async def upload_document(self, data, filename, kind="doc", **kwargs):
        self.doc_uploads.append((kwargs.get("peer_id"), filename, kind))
        return "doc-777_2"

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

    tmpdir = _tempfile.mkdtemp(prefix="vk-test-")
    png = os.path.join(tmpdir, "chart.png")
    with open(png, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    uploaded, sent = [], []

    class FakeClient:
        def __init__(self, token, api_version=None, **kwargs):
            self.token = token

        async def resolve_group(self):
            return (777, "Mock")

        async def send_message(self, peer_id, message, **kwargs):
            sent.append({"peer_id": peer_id, "message": message, **kwargs})
            return 1

        async def upload_photo(self, data, filename="image.jpg"):
            uploaded.append(("photo", os.path.basename(filename), len(data)))
            return "photo-777_1"

        async def upload_document(self, data, filename, kind="doc", peer_id=None):
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
    # mkdtemp alone leaked one directory per run — about 120 of them had piled up in the scratch
    # area before anyone looked. Cleaned here, after the assertions that need the file.
    shutil.rmtree(tmpdir, ignore_errors=True)


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
        def __init__(self, token, api_version=None, **kwargs):
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


def test_reactions_can_be_enabled_by_environment():
    """The other half of the default: the env var turns them on (and the module guard above is why
    this test cannot leak into the one before it)."""
    os.environ["VK_REACTIONS_ENABLED"] = "true"
    try:
        assert make_adapter().reactions_enabled is True
    finally:
        os.environ.pop("VK_REACTIONS_ENABLED", None)


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


def test_document_upload_carries_the_conversation_peer():
    """VK answers ``docs.getMessagesUploadServer`` with ``peer_id is invalid`` for 0.

    Found in the gateway log, not in a test: every document and voice message had been failing.
    """
    adapter = make_adapter()
    with open_loop() as loop:
        loop.run_until_complete(
            adapter._upload_bytes(b"%PDF-1.4 test", "док.pdf", kind="doc", chat_id="13580122"))
    assert adapter.client.doc_uploads == [(13580122, "док.pdf", "doc")]


def test_voice_upload_uses_the_same_peer_scoped_server():
    adapter = make_adapter()
    with open_loop() as loop:
        loop.run_until_complete(
            adapter._upload_bytes(b"OggS", "voice.ogg", kind="audio_message", chat_id="13580122"))
    assert adapter.client.doc_uploads == [(13580122, "voice.ogg", "audio_message")]


def test_photo_upload_keeps_working_without_a_peer():
    """The asymmetry is VK's: ``photos.getMessagesUploadServer`` accepts 0, the docs endpoint doesn't."""
    adapter = make_adapter()
    with open_loop() as loop:
        loop.run_until_complete(
            adapter._upload_bytes(b"\x89PNG!", "x.jpg", kind="photo", chat_id="13580122"))
    assert adapter.client.photo_uploads == [("x.jpg", 5)]
    assert adapter.client.doc_uploads == []


def test_ffmpeg_is_found_in_the_bundled_tools_when_it_is_not_on_path():
    """The gateway service PATH holds no ffmpeg; a which-only lookup silently broke voice transcoding."""
    import tempfile as _tempfile
    import vk.adapter as adapter_mod

    root = pathlib.Path(_tempfile.mkdtemp(prefix="hermes-verify-ffmpeg-"))
    try:
        binary = root / "tools" / "ffmpeg-9.0.1-linux-x64" / "bin" / "ffmpeg"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
        saved_home = os.environ.get("HERMES_HOME")
        saved_which = adapter_mod.shutil.which
        os.environ["HERMES_HOME"] = str(root)
        adapter_mod.shutil.which = lambda name: None
        try:
            assert adapter_mod._find_ffmpeg() == str(binary)
        finally:
            adapter_mod.shutil.which = saved_which
            if saved_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = saved_home
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_ffmpeg_is_absent_when_neither_path_nor_tools_has_it():
    import tempfile as _tempfile
    import vk.adapter as adapter_mod

    root = pathlib.Path(_tempfile.mkdtemp(prefix="hermes-verify-ffmpeg-"))
    saved_home = os.environ.get("HERMES_HOME")
    saved_which = adapter_mod.shutil.which
    os.environ["HERMES_HOME"] = str(root)
    adapter_mod.shutil.which = lambda name: None
    try:
        assert adapter_mod._find_ffmpeg() is None
    finally:
        adapter_mod.shutil.which = saved_which
        if saved_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = saved_home
        shutil.rmtree(root, ignore_errors=True)


# ── рендер: вложенность, длинные токены, границы чанков ──────────────────────

def test_link_inside_bold_keeps_both_items():
    """Nested markup the model really writes: a bold line containing a tappable link.

    Both facts must survive — the link has to stay tappable (that is what a user notices) and the bold
    must not be dropped, because VK voids the WHOLE ``format_data`` payload when any item is malformed.
    """
    text, fmt = render_chunks("**см. [док](https://example.com)**")[0]
    assert text == "см. док"
    kinds = {item["type"] for item in fmt["items"]}
    assert kinds == {"bold", "url"}
    url_item = next(item for item in fmt["items"] if item["type"] == "url")
    assert url_item["url"] == "https://example.com"
    assert text[url_item["offset"]:url_item["offset"] + url_item["length"]] == "док"


def test_long_unbroken_token_is_split_not_dropped():
    """A 9000-character token (a long URL, a base64 blob) has no space to split on. VK rejects such a
    message whole, so the renderer must break it itself — and lose nothing while doing it."""
    source = "a" * 9000
    chunks = render_chunks(source, 4000)
    assert len(chunks) >= 3
    assert "".join(text for text, _ in chunks) == source
    assert all(len(text) <= 4000 for text, _ in chunks)


def test_emoji_heavy_text_survives_chunking_whole():
    """2000 rockets are 4000 UTF-16 units — exactly the limit, so the split lands next to a surrogate
    pair. A chunk cutting a pair in half renders as a broken symbol, and anything dropped is invisible."""
    source = "🚀" * 2000 + " конец"
    chunks = render_chunks(source, 4000)
    assert "".join(text for text, _ in chunks) == source
    assert sum(text.count("🚀") for text, _ in chunks) == 2000


def test_empty_content_renders_one_empty_chunk():
    """Callers pass ``content or ""``: an exception here would kill the answer before it is attempted."""
    chunks = render_chunks("")
    assert len(chunks) == 1 and chunks[0][0] == ""


def test_table_inside_a_quote_is_converted_like_a_table():
    """A model quoting a table (``> |Сервис|Статус|``) must not put pipes and dash rows on a phone."""
    text, _ = render_chunks("> | Сервис | Статус |\n> |---|---|\n> | API | ок |")[0]
    assert "|" not in text and "---" not in text
    assert "API" in text and "ок" in text


def _registered_platform_kwargs() -> dict:
    """What the plugin hands the core in ``register()`` — the model reads ``platform_hint`` as truth."""
    from vk.adapter import register
    captured: dict = {}

    class _Ctx:
        def register_platform(self, **kwargs):
            captured.update(kwargs)

        def register_tool(self, **kwargs):
            # The plugin also registers the widget tool (`vk_checklist`); a ctx without this method
            # would make the whole register() call fail, so the stub mirrors both halves.
            captured.setdefault("tools", []).append(kwargs)

    register(_Ctx())
    return captured


def test_platform_hint_promises_only_markup_the_renderer_can_deliver():
    """Measured defect: the hint claimed VK renders ``~~strike~~``. It does not — every marker outside
    ``VK_SAFE_ITEM_TYPES`` is stripped, so the emphasis arrived as plain text on the user's phone.

    The hint may still *mention* strike and underline, but only as markup that does NOT survive.
    """
    hint = _registered_platform_kwargs()["platform_hint"]
    assert "~~" not in hint                    # strikethrough is never offered as usable syntax
    assert "arrives as plain text" in hint     # the limitation is stated, not left to be discovered
    assert "**" in hint and "italic" in hint and "link" in hint
    assert "4096" in hint                      # the split limit stays documented for the model


# ── интерфейсы, которые вызывает ядро (плагин-менеджер, статус, cron) ────────

def test_check_requirements_is_false_without_a_token(monkeypatch):
    """`hermes plugins` shows a channel as ready/disabled by this probe: a false positive would
    advertise a channel that cannot connect, and a false negative hides a working one."""
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret", lambda name, default="": default)
    assert mod.check_requirements() is False


def test_check_requirements_is_true_with_a_token(monkeypatch):
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret",
                        lambda name, default="": "vk1.a.TOKEN" if name == "VK_TOKEN" else default)
    assert mod.check_requirements() is True


def test_validate_config_accepts_a_token_from_config_or_environment(monkeypatch):
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret", lambda name, default="": default)
    assert mod.validate_config(SimpleNamespace(extra={"token": "t"})) is True
    assert mod.validate_config(SimpleNamespace(extra={})) is False
    assert mod.is_connected(SimpleNamespace(extra={"token": "t"})) is True


def test_env_enablement_seeds_the_cron_home_channel(monkeypatch):
    """Env-only setups (no config.yaml) must still show up in status and know where cron delivers:
    this is the function the core calls to learn VK_HOME_CHANNEL."""
    import vk.adapter as mod
    import gateway.platforms._shared as shared

    def _reader(name, default=""):
        # Both readers must be stubbed: _env_enablement reads the token itself, while the home-channel
        # row is seeded inside the framework helper, which imports its own get_scoped_secret.
        return {"VK_TOKEN": "vk1.a.TOKEN", "VK_HOME_CHANNEL": "13580122",
                "VK_QUOTE_IN_GROUPS": "false"}.get(name, default)

    monkeypatch.setattr(mod, "get_scoped_secret", _reader)
    monkeypatch.setattr(shared, "get_scoped_secret", _reader)
    seeded = mod._env_enablement()
    assert seeded and seeded["token"] == "vk1.a.TOKEN"
    assert "13580122" in str(seeded)                 # the home peer travelled to the core
    assert seeded.get("quote_in_groups") is False    # the string "false" was parsed, not copied
    monkeypatch.setattr(mod, "get_scoped_secret", lambda name, default="": default)
    assert mod._env_enablement() is None             # no token → nothing to seed


# ── кавычки, состояние «печатает», сведения о чате ───────────────────────────

def test_group_answer_quotes_the_last_inbound_while_a_dm_never_does():
    """VK's reference behaviour: in a беседа the answer quotes the message it answers; a DM has nothing
    to quote. The rule is enforced in send(), not in the caller — hence the test goes through send()."""
    adapter = make_adapter({"quote_in_groups": True})
    adapter._last_inbound[str(GROUP_PEER)] = "900"
    adapter._last_inbound["123456"] = "500"
    with open_loop() as loop:
        loop.run_until_complete(adapter.send(str(GROUP_PEER), "ответ"))
        loop.run_until_complete(adapter.send("123456", "ответ"))
    group_call, dm_call = adapter.client.sent
    assert group_call["reply_to"] == 900
    assert dm_call["reply_to"] is None


def test_send_typing_marks_the_dialog_as_typing():
    adapter = make_adapter()
    with open_loop() as loop:
        loop.run_until_complete(adapter.send_typing("123456"))
    assert adapter.client.activity == (123456, "typing")


def test_get_chat_info_names_a_dm_by_user_and_a_group_by_title():
    adapter = make_adapter()
    with open_loop() as loop:
        dm = loop.run_until_complete(adapter.get_chat_info("123456"))
        group = loop.run_until_complete(adapter.get_chat_info(str(GROUP_PEER)))
    assert dm == {"name": "Иван", "type": "dm", "chat_id": "123456"}
    assert group["type"] == "group" and group["name"] == "Рабочая беседа"


def test_media_wrappers_map_to_the_expected_upload_kind():
    """The core picks the wrapper (send_image_file / send_document / send_voice); a wrong kind would
    silently deliver a picture as a file, so the mapping is pinned here."""
    from gateway.platforms.base import SendResult
    adapter = make_adapter()
    seen: list = []

    async def _capture(chat_id, source, *, kind, caption=None, **kwargs):
        seen.append(kind)
        return SendResult(success=True, message_id="1")

    adapter._send_attachment = _capture
    with open_loop() as loop:
        loop.run_until_complete(adapter.send_image_file("123456", __file__))
        loop.run_until_complete(adapter.send_document("123456", __file__))
        loop.run_until_complete(adapter.send_voice("123456", __file__))
    assert seen == ["photo", "doc", "voice"]


def test_voice_falls_back_to_a_file_when_the_transcode_fails():
    """No ffmpeg → the audio still has to arrive, as a document. Losing the user's recording because a
    transcoder is missing is the failure this guards."""
    from gateway.platforms.base import SendResult
    adapter = make_adapter()
    seen: list = []

    async def _capture(chat_id, source, *, kind, caption=None, **kwargs):
        seen.append(kind)
        return SendResult(success=(kind != "voice"), error=None if kind != "voice" else "no ffmpeg")

    adapter._send_attachment = _capture
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send_voice("123456", __file__))
    assert seen == ["voice", "doc"] and result.success


# ── входящее: служебные апдейты, альбомы ─────────────────────────────────────

def test_service_updates_never_reach_the_agent():
    """Typing/read/allow/deny updates arrive constantly. Dispatching them would wake the agent (and
    bill a model call) for nothing."""
    adapter = make_adapter()
    seen = capture_events(adapter)
    with open_loop() as loop:
        for kind in ("message_typing_state", "message_read", "message_allow", "message_deny"):
            loop.run_until_complete(adapter._dispatch({"type": kind, "object": {"peer_id": 123456}}))
    assert seen == []


def test_album_of_photos_is_cached_whole():
    """Two screenshots in one message: both must reach the agent — a model asked to compare two
    pictures sees a single one otherwise."""
    adapter = make_adapter()
    adapter.client = RecordingClient()
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "photo", "photo": {"sizes": [{"width": 10, "url": "https://sun9-1.userapi.com/a.jpg"}]}},
        {"type": "photo", "photo": {"sizes": [{"width": 10, "url": "https://sun9-1.userapi.com/b.jpg"}]}},
    ]}, "album")
    assert len(seen[0].media_urls) == 2
    assert len(adapter.client.downloads) == 2


def test_unknown_attachment_types_are_described_not_swallowed():
    """A survey, a sticker or a wall post must still be *visible* to the agent as a note — silence made
    the model answer as if nothing had been sent."""
    adapter = make_adapter()
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "sticker", "sticker": {"id": 1}},
        {"type": "wall", "wall": {"id": 2}},
        {"type": "poll", "poll": {"id": 3}},
    ]}, "odd")
    assert "[стикер]" in seen[0].text and "[запись со стены]" in seen[0].text and "[poll]" in seen[0].text


# ── метки кнопок и пределы VK ────────────────────────────────────────────────

def test_button_labels_are_truncated_to_the_vk_limit():
    """VK rejects a keyboard whose label is longer than 40 characters; a trimmed label with an ellipsis
    beats an answer that never arrives with its buttons."""
    from vk.adapter import MAX_BUTTON_LABEL, _kill
    assert _kill("x" * 100, MAX_BUTTON_LABEL) == "x" * (MAX_BUTTON_LABEL - 1) + "…"
    assert _kill("коротко", MAX_BUTTON_LABEL) == "коротко"
    assert _kill("  обрезать пробелы  ", MAX_BUTTON_LABEL) == "обрезать пробелы"


# ── автономная отправка (cron) ───────────────────────────────────────────────

def test_standalone_send_without_a_token_reports_an_error_instead_of_raising(monkeypatch):
    """Cron delivery returns a failure dict the scheduler can log; an exception would abort the job and
    the reason would vanish into a traceback. The envelope itself carries ``error`` (no ``success``)."""
    import vk.adapter as mod
    monkeypatch.setattr(mod, "extra_or_secret", lambda extra, key, env, default="": default)
    with open_loop() as loop:
        result = loop.run_until_complete(mod._standalone_send(SimpleNamespace(extra={}), "123456", "отчёт"))
    assert "error" in result and "VK_TOKEN" in result["error"]


def test_standalone_send_skips_a_missing_file_and_keeps_the_report(monkeypatch, tmp_path):
    """A report must not be lost because one attachment disappeared between listing and sending — and a
    single-chunk caption rides with the first file instead of costing the user a second message."""
    import vk.adapter as mod

    class _Client:
        def __init__(self, *args, **kwargs):
            self.sent: list = []

        async def resolve_group(self):
            return (777, "Тест")

        async def send_message(self, peer_id, message, **kwargs):
            self.sent.append((peer_id, message, kwargs))
            return 42

        async def upload_document(self, *args, **kwargs):
            return "doc-777_9"

        async def upload_video(self, *args, **kwargs):
            return "video-777_9"

        async def close(self):
            return None

    holder: dict = {}

    def _factory(*args, **kwargs):
        holder["client"] = _Client()
        return holder["client"]

    monkeypatch.setattr(mod, "VkClient", _factory)
    monkeypatch.setattr(mod, "extra_or_secret", lambda extra, key, env, default="": "vk1.a.TOKEN")
    report = tmp_path / "report.txt"
    report.write_text("данные", encoding="utf-8")
    with open_loop() as loop:
        result = loop.run_until_complete(mod._standalone_send(
            SimpleNamespace(extra={}), "123456", "отчёт", media_files=[str(tmp_path / "нет.txt"), str(report)]))
    assert result.get("success") is True
    calls = holder["client"].sent
    assert len(calls) == 1                                  # подпись уехала с файлом, а не отдельно
    assert calls[0][1] == "отчёт" and calls[0][2]["attachment"] == "doc-777_9"


def test_standalone_send_uploads_a_video_natively(monkeypatch, tmp_path):
    """Cron reports can carry an .mp4: it must go out as a video attachment, not as a plain file, or the
    scheduled path and the live path disagree about the same file."""
    import vk.adapter as mod

    class _Client:
        def __init__(self, *args, **kwargs):
            self.sent: list = []

        async def resolve_group(self):
            return (777, "Тест")

        async def send_message(self, peer_id, message, **kwargs):
            self.sent.append((peer_id, message, kwargs))
            return 42

        async def upload_video(self, *args, **kwargs):
            return "video-777_9"

        async def close(self):
            return None

    holder: dict = {}

    def _factory(*args, **kwargs):
        holder["client"] = _Client()
        return holder["client"]

    monkeypatch.setattr(mod, "VkClient", _factory)
    monkeypatch.setattr(mod, "extra_or_secret", lambda extra, key, env, default="": "vk1.a.TOKEN")
    clip = tmp_path / "report.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
    with open_loop() as loop:
        result = loop.run_until_complete(mod._standalone_send(
            SimpleNamespace(extra={}), "123456", "отчёт", media_files=[str(clip)]))
    assert result.get("success") is True
    assert holder["client"].sent[0][2]["attachment"] == "video-777_9"


def test_standalone_send_falls_back_to_a_document_when_video_is_refused(monkeypatch, tmp_path):
    """Measured live: a community token gets ``video.save`` error 5, and the earlier code lost the WHOLE
    report because the refusal escaped the media loop. The file must arrive as a document instead."""
    import vk.adapter as mod
    from vk.vk_api import VkApiError

    class _Client:
        def __init__(self, *args, **kwargs):
            self.sent: list = []
            self.documents: list = []

        async def resolve_group(self):
            return (777, "Тест")

        async def send_message(self, peer_id, message, **kwargs):
            self.sent.append((peer_id, message, kwargs))
            return 42

        async def upload_video(self, *args, **kwargs):
            raise VkApiError("video.save", 5, "User authorization failed")

        async def upload_document(self, data, filename, **kwargs):
            self.documents.append(filename)
            return "doc-777_9"

        async def close(self):
            return None

    holder: dict = {}

    def _factory(*args, **kwargs):
        holder["client"] = _Client()
        return holder["client"]

    monkeypatch.setattr(mod, "VkClient", _factory)
    monkeypatch.setattr(mod, "extra_or_secret", lambda extra, key, env, default="": "vk1.a.TOKEN")
    clip = tmp_path / "report.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
    with open_loop() as loop:
        result = loop.run_until_complete(mod._standalone_send(
            SimpleNamespace(extra={}), "123456", "отчёт", media_files=[str(clip)]))
    assert result.get("success") is True
    assert holder["client"].documents == ["report.mp4"]
    assert holder["client"].sent[0][2]["attachment"] == "doc-777_9"


def test_standalone_send_hands_the_personal_token_to_the_client(monkeypatch, tmp_path):
    """The cron/`hermes send` path builds its own client, and it must receive the personal token.

    Measured live 2026-10-04: the gateway held VK_USER_TOKEN, but this path constructed
    ``VkClient(community_token, ...)`` — so ``video.save`` went out with the community key, VK answered
    error 5, and the .mp4 degraded to a document while the direct API call worked fine.
    """
    import vk.adapter as mod

    seen: dict = {}

    class _Client:
        def __init__(self, *args, **kwargs):
            seen["args"], seen["kwargs"] = args, kwargs
            self.sent: list = []

        async def resolve_group(self):
            return (777, "Тест")

        async def send_message(self, peer_id, message, **kwargs):
            self.sent.append((peer_id, message, kwargs))
            return 42

        async def upload_video(self, *args, **kwargs):
            return "video-777_9"

        async def close(self):
            return None

    monkeypatch.setattr(mod, "VkClient", lambda *a, **kw: _Client(*a, **kw))
    monkeypatch.setattr(mod, "extra_or_secret",
                        lambda extra, key, env, default="": "vk1.a.PERSONAL" if env == "VK_USER_TOKEN"
                        else "vk1.a.COMMUNITY")
    clip = tmp_path / "report.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
    with open_loop() as loop:
        result = loop.run_until_complete(mod._standalone_send(
            SimpleNamespace(extra={}), "123456", "отчёт", media_files=[str(clip)]))
    assert result.get("success") is True
    assert seen["args"][0] == "vk1.a.COMMUNITY", "the community token still opens the connection"
    assert seen["kwargs"].get("user_token") == "vk1.a.PERSONAL", (
        "without the personal token the video is refused with error 5 and arrives as a document")


# ── клавиатура команд по чатам ───────────────────────────────────────────────

def test_command_keyboard_is_decided_per_chat():
    """A global flag plus a per-chat map: a chat's own entry wins in BOTH directions, an unlisted chat
    inherits the global value — otherwise the setting would work for exactly one chat."""
    adapter = make_adapter({"command_keyboard": True, "command_keyboard_by_peer": {"123456": False}})
    assert adapter.keyboard_for("123456") is None                # этот чат себе отключил
    assert adapter.keyboard_for("777777") == command_keyboard()  # остальные наследуют общий флаг

    opted_in = make_adapter({"command_keyboard": False, "command_keyboard_by_peer": {"123456": True}})
    assert opted_in.keyboard_for("123456") == command_keyboard()
    assert opted_in.keyboard_for("777777") is None

    assert make_adapter().keyboard_for("123456") is None         # по умолчанию выключено у всех


def test_send_attaches_the_keyboard_only_for_the_chats_that_want_it():
    """The decision must reach the wire: the per-chat map is worthless if send() still uses the global."""
    adapter = make_adapter({"command_keyboard": True, "command_keyboard_by_peer": {str(GROUP_PEER): False}})
    with open_loop() as loop:
        loop.run_until_complete(adapter.send(str(GROUP_PEER), "без кнопок"))
        loop.run_until_complete(adapter.send("123456", "с кнопками"))
    without, with_keyboard = adapter.client.sent
    assert without["keyboard"] is None
    assert with_keyboard["keyboard"] == command_keyboard()


def test_standalone_send_honours_the_per_chat_keyboard_map(monkeypatch):
    """The cron path must reach the same decision as the live one: the buttons a chat enabled have to
    ride on scheduled reports too, or the two paths disagree and nobody notices."""
    import vk.adapter as mod

    class _Client:
        def __init__(self, *args, **kwargs):
            self.sent: list = []

        async def resolve_group(self):
            return (777, "Тест")

        async def send_message(self, peer_id, message, **kwargs):
            self.sent.append(kwargs)
            return 1

        async def close(self):
            return None

    holder: dict = {"clients": []}

    def _factory(*args, **kwargs):
        # Each call builds its own client (that is the point of the standalone sender), so both are kept.
        client = _Client()
        holder["clients"].append(client)
        return client

    monkeypatch.setattr(mod, "VkClient", _factory)
    monkeypatch.setattr(mod, "extra_or_secret", lambda extra, key, env, default="": "vk1.a.TOKEN")
    extra = {"command_keyboard": True, "command_keyboard_by_peer": {"123456": False}}
    with open_loop() as loop:
        loop.run_until_complete(mod._standalone_send(SimpleNamespace(extra=extra), "123456", "отчёт"))
        loop.run_until_complete(mod._standalone_send(SimpleNamespace(extra=extra), "777777", "отчёт"))
    off, on = holder["clients"]
    assert off.sent[0]["keyboard"] is None and on.sent[0]["keyboard"] == command_keyboard()


# ── резервный опрос истории (страховка под Long Poll) ────────────────────────

def _conversation(mid: int, date: int, text: str) -> dict:
    return {"last_message": {"id": mid, "date": date, "peer_id": 123456, "from_id": 123456,
                             "text": text, "out": 0, "attachments": []}}


def test_fallback_sweep_takes_only_messages_newer_than_the_marker():
    """The marker is what keeps a sweep from replaying history at the agent on every pass."""
    adapter = make_adapter({"fallback_poll_enabled": True})
    adapter._last_poll_ok = time.monotonic() - 10_000        # Long Poll молчит давно
    adapter._fallback_since = 1_700_000_100.0
    adapter.client.conversations = [_conversation(1, 1_700_000_050, "старое"),
                                    _conversation(2, 1_700_000_200, "новое")]
    seen = capture_events(adapter)
    with open_loop() as loop:
        loop.run_until_complete(adapter._fallback_sweep())
    assert [event.text for event in seen] == ["новое"]
    assert adapter._fallback_since == 1_700_000_200.0        # маркер сдвинулся


def test_fallback_sweep_is_skipped_while_long_poll_is_alive():
    """Long Poll — основной путь: опрашивать историю при живом Long Poll значило бы тянуть беседы
    впустую и отдавать дедупликатору работу, которой не должно быть."""
    adapter = make_adapter({"fallback_poll_enabled": True})
    adapter._last_poll_ok = time.monotonic()
    adapter.client.conversations = [_conversation(1, 1_700_000_200, "не должно дойти")]
    seen = capture_events(adapter)
    with open_loop() as loop:
        loop.run_until_complete(adapter._fallback_sweep())
    assert adapter.client.conversations_calls == 0 and seen == []


def test_fallback_poll_is_off_by_default_and_clamped():
    """Выключено по умолчанию (как всё, что меняет поведение) и зажато: нулевой интервал бил бы по API."""
    assert make_adapter().fallback_poll is False
    adapter = make_adapter({"fallback_poll_enabled": True, "fallback_poll_interval_seconds": 0,
                            "fallback_poll_batch_size": 0})
    assert adapter.fallback_interval >= 15 and adapter.fallback_batch >= 1


def test_fallback_sweep_survives_a_client_error():
    """Сбой опроса не должен ронять канал: цикл ловит исключение и продолжает крутиться."""
    adapter = make_adapter({"fallback_poll_enabled": True})
    attempts = {"n": 0}

    async def _boom(*args, **kwargs):
        attempts["n"] += 1
        raise RuntimeError("VK недоступен")

    adapter.client.get_conversations = _boom
    adapter._last_poll_ok = time.monotonic() - 10_000
    adapter.fallback_interval = 0.05     # зажим интервала — для значений из конфига, не для теста

    async def _drive():
        task = asyncio.create_task(adapter._fallback_poll_loop())
        await asyncio.sleep(0.3)
        alive = not task.done()          # цикл пережил ошибку
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return alive

    with open_loop() as loop:
        alive = loop.run_until_complete(_drive())
    assert alive and attempts["n"] >= 2  # ошибка не убила цикл, он попробовал снова


# ── исходящее видео ──────────────────────────────────────────────────────────

def test_video_goes_out_as_a_native_video_attachment():
    """The core routes ``.mp4`` to ``send_video``; without this override the base class only apologises
    and the user never gets the file."""
    adapter = make_adapter()
    payload = pathlib.Path(__file__).read_bytes()
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send_video("123456", __file__, caption="клип"))
    assert result.success
    assert adapter.client.video_uploads == [(pathlib.Path(__file__).name, len(payload))]
    assert adapter.client.sent[-1]["attachment"] == "video-777_5"


def test_video_falls_back_to_a_document_when_vk_refuses_the_video_path():
    """A community not allowed to upload video must still receive the file, not an apology."""
    from gateway.platforms.base import SendResult
    adapter = make_adapter()
    seen: list = []

    async def _capture(chat_id, source, *, kind, caption=None, **kwargs):
        seen.append(kind)
        return SendResult(success=(kind != "video"), error=None if kind != "video" else "video refused")

    adapter._send_attachment = _capture
    with open_loop() as loop:
        result = loop.run_until_complete(adapter.send_video("123456", __file__))
    assert seen == ["video", "doc"] and result.success


def test_client_video_upload_reserves_uploads_and_returns_the_attachment():
    """Three steps in one call: ``video.save`` reserves, the bytes go to the returned URL, and the
    reserved id becomes the attachment. A wrong order — or a skipped upload — yields a broken string.

    Note the stub point: ``upload_video`` goes through ``call_as`` (it must be able to use the optional
    user token), so stubbing ``call`` is not enough — measured the hard way when these two tests started
    reaching the real API after the refactor.
    """
    from vk.vk_api import VkClient
    client = VkClient("vk1.a.MOCK")
    calls: list = []
    uploads: list = []

    async def _call_as(token, method, **params):
        calls.append((token, method, params))
        return {"upload_url": "https://up.example/v", "video_id": 5, "owner_id": -777}

    async def _upload(url, data, filename):
        uploads.append((url, filename, len(data)))
        return {}

    client.call_as, client._upload = _call_as, _upload
    with open_loop() as loop:
        attachment = loop.run_until_complete(client.upload_video(b"f" * 10, "клип.mp4"))
    assert attachment == "video-777_5"
    assert calls == [("vk1.a.MOCK", "video.save",
                      {"name": "клип.mp4", "is_private": 1, "wallpost": 0, "timeout": 30})]
    assert uploads == [("https://up.example/v", "клип.mp4", 10)]


def test_client_video_upload_keeps_the_access_key_so_the_recipient_can_play_it():
    """A private video (``is_private=1``) is playable only through its access key.

    Measured live 2026-10-04: ``video.save`` returns ``access_key``, and dropping it hands the recipient
    a player that refuses to play — worse for the user than the document fallback this path exists to beat.
    """
    from vk.vk_api import VkClient
    client = VkClient("vk1.a.MOCK")

    async def _call_as(token, method, **params):
        return {"upload_url": "https://up.example/v", "video_id": 5, "owner_id": -777,
                "access_key": "abc123"}

    async def _upload(url, data, filename):
        return {}

    client.call_as, client._upload = _call_as, _upload
    with open_loop() as loop:
        attachment = loop.run_until_complete(client.upload_video(b"f" * 10, "клип.mp4"))
    assert attachment == "video-777_5_abc123"


def test_client_video_upload_fails_loudly_without_an_upload_url():
    """Without this check a community that cannot take video would get a blank attachment string, which
    VK rejects — and the user would see nothing at all."""
    from vk.vk_api import VkApiError, VkClient
    client = VkClient("vk1.a.MOCK")

    async def _call_as(token, method, **params):
        return {"video_id": 0}

    client.call_as = _call_as
    caught = None
    with open_loop() as loop:
        try:
            loop.run_until_complete(client.upload_video(b"f" * 10, "клип.mp4"))
        except VkApiError as exc:
            caught = exc
    assert caught is not None and "video_id" in caught.message


# ── окно дедупликации (VK_DEDUPE_TTL_SECONDS) ────────────────────────────────

def test_dedupe_window_defaults_to_900_seconds():
    """VK replays buffered updates for ~5 minutes, so the default window covers a reconnect hiccup."""
    assert make_adapter().dedupe_ttl_seconds == 900


def test_dedupe_window_is_configurable_through_both_channels(monkeypatch):
    """`extra` (config.yaml) and the env var both work, env wins — the plugin convention."""
    assert make_adapter({"dedupe_ttl_seconds": 1800}).dedupe_ttl_seconds == 1800
    monkeypatch.setenv("VK_DEDUPE_TTL_SECONDS", "2400")
    assert make_adapter().dedupe_ttl_seconds == 2400
    monkeypatch.setenv("VK_DEDUPE_TTL_SECONDS", "120")
    assert make_adapter({"dedupe_ttl_seconds": 1800}).dedupe_ttl_seconds == 120


def test_dedupe_window_ignores_zero_junk_and_negative_values():
    """A `0` here must NOT silently switch deduplication off (the sibling plugin reads it that way): the
    protection against answering a redelivered message twice is the entire point of the window."""
    from vk.adapter import DEFAULT_DEDUPE_TTL_SECONDS, MIN_DEDUPE_TTL_SECONDS
    for raw in ("0", "-5", "1", str(MIN_DEDUPE_TTL_SECONDS - 1), "abc", "", None):
        assert make_adapter({"dedupe_ttl_seconds": raw}).dedupe_ttl_seconds == DEFAULT_DEDUPE_TTL_SECONDS
    assert make_adapter({"dedupe_ttl_seconds": MIN_DEDUPE_TTL_SECONDS}).dedupe_ttl_seconds == MIN_DEDUPE_TTL_SECONDS


def test_dedupe_window_of_zero_in_the_environment_keeps_the_default(monkeypatch):
    monkeypatch.setenv("VK_DEDUPE_TTL_SECONDS", "0")
    assert make_adapter().dedupe_ttl_seconds == 900


def test_configured_window_reaches_the_deduplicator_itself():
    """Asserting the attribute alone would pass while the deduplicator kept its own built-in default."""
    adapter = make_adapter({"dedupe_ttl_seconds": 1200})
    assert adapter._dedup._ttl == 1200


# ── изоляция профилей: секреты только через scoped-читатель ──────────────────

def test_token_never_comes_from_the_ambient_environment(monkeypatch):
    """A second profile must not borrow the default profile's token. Credentials are read only through
    `get_scoped_secret` (which resolves the ACTIVE profile's .env), so a bare VK_TOKEN in os.environ —
    exactly what a multiplexed gateway process has lying around — must be invisible to the adapter."""
    import vk.adapter as mod
    monkeypatch.setenv("VK_TOKEN", "token-from-the-wrong-profile")
    monkeypatch.setattr(mod, "get_scoped_secret", lambda name, default="": default)
    assert make_adapter().token == ""


def test_two_adapters_resolve_their_own_profiles_token(monkeypatch):
    """The multiplexing contract in one test: same plugin class, two profiles, two different tokens."""
    import vk.adapter as mod
    active = {"profile": "default"}
    tokens = {"default": "token-default", "independent": "token-independent"}
    monkeypatch.setattr(mod, "get_scoped_secret",
                        lambda name, default="": tokens[active["profile"]] if name == "VK_TOKEN" else default)
    first = make_adapter()
    active["profile"] = "independent"
    second = make_adapter()
    assert (first.token, second.token) == ("token-default", "token-independent")


def test_a_profile_without_its_own_token_fails_closed(monkeypatch):
    """Fail-closed, not fallback: no token in THIS profile means the platform probe is False, no cron
    channel is seeded and the adapter cannot authenticate — never a neighbouring profile's secrets."""
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret", lambda name, default="": default)
    assert mod.check_requirements() is False
    assert mod.validate_config(SimpleNamespace(extra={})) is False
    assert mod._env_enablement() is None
    assert make_adapter().token == ""


def test_credentials_are_never_read_from_os_environ():
    """Catches the common literal form of the leak: `os.getenv("VK_…")` / `os.environ["VK_…"]` would work
    for the default profile and then hand that value to every other one. This is a lint, not the guard —
    a variable name (`os.environ.get(name)`) slips past it, which is exactly what the behavioural test
    above catches (measured: mutating `_env` to read the environment first failed that test, not this one)."""
    plugin_dir = pathlib.Path(__file__).resolve().parents[1]
    patterns = ('os.getenv("VK_', "os.getenv('VK_", 'os.environ["VK_', "os.environ['VK_",
                'os.environ.get("VK_', "os.environ.get('VK_")
    offenders = [f"{name}: {pattern}" for name in ("adapter.py", "vk_api.py", "vk_markdown.py")
                 for pattern in patterns if pattern in (plugin_dir / name).read_text(encoding="utf-8")]
    assert offenders == []


def test_adapter_construction_does_not_write_credentials_into_the_environment(monkeypatch):
    """Two profiles share one process: a plugin that exports its token into os.environ hands it to the
    next profile, and that leak is invisible until the wrong community answers."""
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret",
                        lambda name, default="": "token-x" if name == "VK_TOKEN" else default)
    before = {name: value for name, value in os.environ.items() if name.startswith("VK_")}
    make_adapter()
    make_adapter()
    assert {name: value for name, value in os.environ.items() if name.startswith("VK_")} == before


# ── входящее видео через пользовательский токен ──────────────────────────────

def test_video_lookup_uses_the_user_token_not_the_community_one():
    """`video.get` is a user-scope method — the community key gets error 5 (measured live), so which
    token travels here is the whole point of the feature."""
    from vk.vk_api import VkClient
    client = VkClient("community-token", user_token="user-token")
    seen: list = []

    async def _call_as(token, method, **params):
        seen.append((token, method, params))
        return {"items": [{"files": {"mp4_720": "https://cdn.example/x.mp4"}, "title": "клип",
                           "duration": 12}]}

    client.call_as = _call_as
    with open_loop() as loop:
        best = loop.run_until_complete(client.get_video_file({"owner_id": -1, "id": 7, "access_key": "ak"}))
    assert best == {"url": "https://cdn.example/x.mp4", "title": "клип", "duration": 12, "ext": ".mp4"}
    assert seen == [("user-token", "video.get", {"videos": "-1_7_ak", "timeout": 30})]


def test_video_lookup_is_not_attempted_without_a_user_token():
    """No user token means do not even ask: a community-key call would fail and log noise every time."""
    from vk.vk_api import VkClient
    client = VkClient("community-token")
    calls: list = []

    async def _fail(*args, **kwargs):
        calls.append(args)
        raise AssertionError("video.get must not be attempted without a user token")

    client.call_as = _fail
    with open_loop() as loop:
        assert loop.run_until_complete(client.get_video_file({"owner_id": -1, "id": 7})) is None
    assert calls == []


def test_video_lookup_prefers_the_largest_available_mp4():
    from vk.vk_api import VkClient
    client = VkClient("c", user_token="u")

    async def _call_as(token, method, **params):
        return {"items": [{"files": {"mp4_240": "low", "mp4_720": "high"}}]}

    client.call_as = _call_as
    with open_loop() as loop:
        best = loop.run_until_complete(client.get_video_file({"owner_id": 1, "id": 2}))
    assert best["url"] == "high"


def test_video_exposing_only_a_watch_page_is_not_an_error():
    """Some videos have no `files` block at all: nothing to download, so the caller keeps its note."""
    from vk.vk_api import VkClient
    client = VkClient("c", user_token="u")

    async def _call_as(token, method, **params):
        return {"items": [{"id": 2, "title": "стрим"}]}

    client.call_as = _call_as
    with open_loop() as loop:
        assert loop.run_until_complete(client.get_video_file({"owner_id": 1, "id": 2})) is None


def test_video_upload_prefers_the_user_token():
    """`video.save` is user-scope too: with a user token the native path becomes possible instead of
    always falling back to a document."""
    from vk.vk_api import VkClient
    client = VkClient("community-token", user_token="user-token")
    seen: list = []

    async def _call_as(token, method, **params):
        seen.append((token, method))
        return {"upload_url": "https://up.example/v", "video_id": 5, "owner_id": -777}

    async def _upload(url, data, filename):
        return {}

    client.call_as, client._upload = _call_as, _upload
    with open_loop() as loop:
        loop.run_until_complete(client.upload_video(b"f" * 10, "клип.mp4"))
    assert seen == [("user-token", "video.save")]


def test_redaction_strips_a_token_and_an_access_token_parameter():
    """The user token is a person's credential: an error string quoting a URL must not carry it into
    errors.log, a chat message, or the agent transcript."""
    from vk.vk_api import redact_secrets
    text = redact_secrets("boom vk1.a.SECRETTOKEN https://x/y?access_token=abc123&v=5.199", "vk1.a.SECRETTOKEN")
    assert "SECRETTOKEN" not in text and "abc123" not in text
    assert text.count("[REDACTED]") == 2


def test_inbound_video_is_downloaded_for_the_agent_when_a_user_token_is_set():
    adapter = make_adapter({"user_token": "user-token"})
    adapter.client = RecordingClient(payload=b"\x00\x00\x00\x18ftypmp42" + b"0" * 32)
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "video", "video": {"id": 7, "owner_id": -1, "title": "скринкаст", "duration": 42}}]}, "v1")
    assert adapter.client.video_lookups == [{"id": 7, "owner_id": -1, "title": "скринкаст", "duration": 42}]
    assert len(seen[0].media_urls) == 1 and seen[0].media_types == ["video/mp4"]
    assert "скринкаст" in seen[0].text and "42 с" in seen[0].text


def test_inbound_video_stays_a_note_without_a_user_token():
    """The default: the agent learns that a video arrived, and no API call is made."""
    adapter = make_adapter()
    adapter.client = RecordingClient()
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "video", "video": {"id": 7, "owner_id": -1, "title": "скринкаст", "duration": 42}}]}, "v2")
    assert adapter.client.video_lookups == []
    assert seen[0].media_urls == []
    assert "[видео: скринкаст, 42 с]" in seen[0].text


def test_inbound_video_is_not_fetched_when_downloads_are_switched_off():
    """The download switch is checked before the API call, not after — otherwise a personal token would
    be spent on files the operator explicitly asked not to receive."""
    adapter = make_adapter({"user_token": "user-token", "download_attachments": False})
    adapter.client = RecordingClient()
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "video", "video": {"id": 7, "owner_id": -1, "title": "скринкаст", "duration": 42}}]}, "v3")
    assert adapter.client.video_lookups == [] and seen[0].media_urls == []


def test_a_refused_video_lookup_never_costs_the_message():
    """VK refusing (no video scope, deleted video, whatever) must leave the message intact with its note —
    and the logged reason must be redacted."""
    adapter = make_adapter({"user_token": "user-token"})
    adapter.client = RecordingClient()

    async def _refuse(video, **kwargs):
        from vk.vk_api import VkApiError
        raise VkApiError("video.get", 5, "User authorization failed: user-token was sent")

    adapter.client.get_video_file = _refuse
    seen = capture_events(adapter)
    run_inbound(adapter, {**GROUP_MSG, "attachments": [
        {"type": "video", "video": {"id": 7, "owner_id": -1, "title": "скринкаст", "duration": 42}}]}, "v4")
    assert len(seen) == 1 and "[видео: скринкаст, 42 с]" in seen[0].text
    assert seen[0].media_urls == []


def test_user_token_is_read_from_config_and_environment(monkeypatch):
    assert make_adapter().user_token == ""                      # по умолчанию — не задан
    assert make_adapter({"user_token": "from-config"}).user_token == "from-config"
    monkeypatch.setenv("VK_USER_TOKEN", "from-env")
    assert make_adapter({"user_token": "from-config"}).user_token == "from-env"  # переменная важнее конфига


def test_a_user_token_alone_does_not_make_the_platform_ready(monkeypatch):
    """Fail-closed stays about the COMMUNITY token: a personal token without a community key is not a
    working channel."""
    import vk.adapter as mod
    monkeypatch.setattr(mod, "get_scoped_secret",
                        lambda name, default="": "user-token" if name == "VK_USER_TOKEN" else default)
    assert mod.check_requirements() is False
    assert mod._env_enablement() is None
    assert make_adapter().token == "" and make_adapter().user_token == "user-token"


class _MiniMonkeypatch:
    """The slice of pytest's ``monkeypatch`` the standalone runner needs, with the same undo semantics.

    Without it the pytest-less path — which the README advertises as a way to check the plugin on hosts
    where pytest is absent — crashed on every test that takes a fixture (measured: the whole file exited
    with ``TypeError: missing 1 required positional argument``).
    """

    def __init__(self):
        self._undo: list = []

    def setattr(self, target, name, value=None):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)
        return value

    def setenv(self, name, value):
        self._undo.append((os.environ, name, os.environ.get(name)))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        self._undo.append((os.environ, name, os.environ.get(name)))
        os.environ.pop(name, None)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            if target is os.environ and old is None:
                os.environ.pop(name, None)
            else:
                setattr(target, name, old)


if __name__ == "__main__":
    import inspect
    import tempfile

    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures, skipped = [], []
    for name, fn in tests:
        kwargs: dict = {}
        tmpdir = None
        unknown = [p for p in inspect.signature(fn).parameters
                   if p not in {"monkeypatch", "tmp_path"}]
        if unknown:
            skipped.append(name)
            print(f"skip {name} (нужна фикстура {', '.join(unknown)})")
            continue
        if "monkeypatch" in inspect.signature(fn).parameters:
            kwargs["monkeypatch"] = _MiniMonkeypatch()
        if "tmp_path" in inspect.signature(fn).parameters:
            tmpdir = tempfile.mkdtemp(prefix="hermes-vk-test-")
            kwargs["tmp_path"] = pathlib.Path(tmpdir)
        try:
            fn(**kwargs)
        except Exception:
            failures.append((name, traceback.format_exc()))
            print(f"FAIL {name}")
        else:
            print(f"ok   {name}")
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)
            if "monkeypatch" in kwargs:
                kwargs["monkeypatch"].undo()
    print(f"\n{len(tests) - len(failures) - len(skipped)}/{len(tests)} passed"
          + (f", {len(skipped)} skipped" if skipped else ""))
    for name, tb in failures:
        print(f"\n=== {name} ===\n{tb}")
    sys.exit(1 if failures else 0)
