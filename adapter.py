"""VK (VKontakte) community-bot adapter for Hermes Agent.

Ports the channel flow proven by ``pfrankov/openclaw-vk`` into a Hermes platform
plugin: Bots Long Poll for inbound (no public URL, no domain, no webhook), the
community token as the only credential, a community that any individual can
create — which is what makes VK a usable phone channel from RF without a proxy.

Design notes
------------
* **Transport**: ``groups.getLongPollServer`` → ``a_check`` long poll (``wait=25``,
  ``version=3``).  ``failed`` 1 advances the cursor, 2/3 re-acquire the server.
  A completed poll request — not the cursor — is the liveness signal, so a quiet
  chat never looks like a dead transport.
* **Inbound**: ``message_new`` / ``message_reply`` / ``message_event`` (button
  press).  Community (outgoing) messages are dropped, as are redelivered ids.
* **Group chats** (``peer_id >= 2e9``) get the inbound message quoted on reply,
  matching the reference implementation; DMs stay unquoted.
* **Buttons** are VK callback keyboards.  A press arrives as ``message_event``,
  which must be acknowledged (``messages.sendMessageEventAnswer``) or the user's
  client keeps spinning.  Payloads reuse the shared Hermes convention
  (``cl:``/``ea:``/``sc:`` id triplets) so ``clarify``, dangerous-command approval
  and slash-command confirmation resolve through the same core resolvers every
  other platform uses.
* **Authorization** is the core gateway's: ``VK_ALLOWED_USERS`` /
  ``VK_ALLOW_ALL_USERS`` plus the shared pairing store (``hermes pairing approve
  vk <code>``), which is why this adapter ships no allowlist logic of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import logging
import mimetypes
import os
import random
import shutil
import tempfile
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from gateway.config import Platform
from gateway.platforms._shared import (
    extra_or_secret, get_scoped_secret, seed_extra_from_env, send_error,
)
from gateway.platforms.base import (
    BasePlatformAdapter, ExecApprovalPrompt, SendResult,
    cache_audio_from_bytes, cache_document_from_bytes, cache_image_from_bytes,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, cancel_task

from .vk_api import DEFAULT_API_VERSION, VkApiError, VkClient
from .vk_markdown import render_chunks, to_plain

logger = logging.getLogger(__name__)

GROUP_PEER_OFFSET = 2_000_000_000  # VK: conversation peer ids start here
LONG_POLL_WAIT = 25
FIRST_POLL_TIMEOUT = 35.0
TRANSPORT_SILENCE_SECONDS = 150.0
MAX_BUTTON_LABEL = 40
MAX_CALLBACK_PAYLOAD = 250
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
VOICE_MAX_SECONDS = 300
# Update types that are expected noise for a bot (never worth a log line when enabled).
_QUIET_UPDATE_TYPES = frozenset({"message_typing_state", "message_read", "message_allow", "message_deny"})


def _is_group(peer_id: int) -> bool:
    return int(peer_id) >= GROUP_PEER_OFFSET


def _kill(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "…"


def _env(extra: dict, env: str, key: str, default: Any = "") -> Any:
    """Env var wins over ``config.yaml`` ``extra`` (the plugin convention)."""
    value = get_scoped_secret(env)
    return value if value not in (None, "") else (extra.get(key, default) if extra else default)


def _truthy(extra: dict, env: str, key: str, default: bool = False) -> bool:
    raw = _env(extra, env, key, None)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


# Commands offered as one-tap buttons. VK has no `/` autocomplete API for community bots — no
# endpoint declares a command list the way Telegram's setMyCommands does — so a "bot keyboard" whose
# text buttons send their own label is the platform's substitute. Two rows of two keep it readable on
# a phone; limits are <=10 rows x 5 buttons and a <=40 char label.
_COMMAND_KEYBOARD_ROWS: tuple = (("/help", "/status"), ("/new", "/stop"))


def command_keyboard() -> Optional[str]:
    """VK bot keyboard (not inline) whose buttons send the command text when tapped.

    A ``text`` button sends its own label as the user's message, so a button labelled ``/help`` is
    indistinguishable from typing it — the tap arrives as an ordinary slash command.

    ``"inline": false`` is set explicitly: VK's documentation shows the field in every keyboard
    example and it is what selects the chat keyboard (under the input) over an inline one. The
    ``payload`` rides along in the inbound event, which is how a tap can be identified server-side —
    VK never returns a bot keyboard back through ``messages.getById``/``getHistory``.
    """
    rows = [[{"action": {"type": "text", "label": _kill(label, MAX_BUTTON_LABEL),
                         "payload": json.dumps({"cmd": label.lstrip("/")}, separators=(",", ":"))},
              "color": "secondary"} for label in row] for row in _COMMAND_KEYBOARD_ROWS]
    return json.dumps({"inline": False, "one_time": False, "buttons": rows}, ensure_ascii=False)


def _keyboard(rows: List[List[Tuple[str, Optional[Dict[str, Any]], str]]]) -> Optional[str]:
    """VK inline keyboard JSON; callback rows carry a compact JSON payload."""
    buttons: List[List[Dict[str, Any]]] = []
    for row in rows:
        built: List[Dict[str, Any]] = []
        for label, payload, color in row:
            if payload is None:
                continue
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            built.append({
                "action": {"type": "callback", "label": _kill(label, MAX_BUTTON_LABEL), "payload": data[:MAX_CALLBACK_PAYLOAD]},
                "color": color if color in {"primary", "secondary", "negative", "positive"} else "secondary",
            })
        if built:
            buttons.append(built)
    return json.dumps({"inline": True, "buttons": buttons}, ensure_ascii=False) if buttons else None


async def _voice_to_ogg_opus(data: bytes, *, max_seconds: int = VOICE_MAX_SECONDS) -> bytes:
    """VK voice messages must be Ogg Opus; transcode with ffmpeg when it is available."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return data
    tmpdir = tempfile.mkdtemp(prefix="vk-voice-")
    try:
        src, dst = os.path.join(tmpdir, "in"), os.path.join(tmpdir, "out.ogg")
        with open(src, "wb") as handle:
            handle.write(data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-i", src, "-ac", "1", "-ar", "48000", "-c:a", "libopus",
            "-b:a", "32k", "-t", str(max_seconds), dst,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as handle:
                return handle.read()
    except Exception as exc:
        logger.info("VK: voice transcode skipped (%s)", exc)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return data


class VKAdapter(BasePlatformAdapter):
    """Async VK community adapter (Bots Long Poll in, messages.send out)."""

    MAX_MESSAGE_LENGTH = 4000
    supports_code_blocks = False
    supports_status_text = False
    typed_command_prefix = "/"

    def __init__(self, config, **kwargs) -> None:
        super().__init__(config=config, platform=Platform("vk"))
        extra = getattr(config, "extra", {}) or {}
        self.token = str(_env(extra, "VK_TOKEN", "token", "") or "").strip()
        self.api_version = str(_env(extra, "VK_API_VERSION", "api_version", DEFAULT_API_VERSION) or DEFAULT_API_VERSION)
        try:
            self.group_id = int(_env(extra, "VK_GROUP_ID", "group_id", 0) or 0)
        except (TypeError, ValueError):
            self.group_id = 0
        self.quote_in_groups = _truthy(extra, "VK_QUOTE_IN_GROUPS", "quote_in_groups", True)
        # Off by default: a persistent keyboard occupies space above the input field.
        self.command_keyboard = _truthy(extra, "VK_COMMAND_KEYBOARD", "command_keyboard", False)
        self.client: Optional[VkClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(ttl_seconds=900)
        self._last_inbound: Dict[str, str] = {}
        self._conn: Tuple[str, str, Any] = ("", "", 0)
        self._last_poll_ok = 0.0
        self._first_poll_done: Optional[asyncio.Event] = None
        self._user_names: Dict[int, str] = {}
        self._chat_titles: Dict[str, str] = {}
        self._approval_state: Dict[str, str] = {}
        self._slash_confirm_state: Dict[str, str] = {}
        self._clarify_state: Dict[str, str] = {}

    # ------------------------------------------------------------------ lifecycle

    @property
    def name(self) -> str:
        return "VK"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self.token:
            self._set_fatal_error("config_missing", "VK_TOKEN is not set", retryable=False)
            return False
        if not self._acquire_platform_lock("vk", self.token, "VK community token"):
            return False
        self.client = VkClient(self.token, api_version=self.api_version, group_id=self.group_id)
        try:
            await self.client.resolve_group()
        except VkApiError as exc:
            self._set_fatal_error("auth_failed", f"VK token rejected: {exc.message}", retryable=False)
            return False
        except Exception as exc:  # network
            self._set_fatal_error("connect_failed", f"VK unreachable: {exc}", retryable=True)
            return False
        try:
            self._conn = await self._acquire_long_poll()
        except Exception as exc:
            self._set_fatal_error("connect_failed", f"VK long poll unavailable: {exc}", retryable=True)
            return False
        self._first_poll_done = asyncio.Event()
        self._last_poll_ok = time.monotonic()
        self._poll_task = asyncio.create_task(self._poll_loop())
        try:
            await asyncio.wait_for(self._first_poll_done.wait(), timeout=FIRST_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            await self.disconnect()
            self._set_fatal_error(
                "longpoll_timeout",
                "VK long poll did not answer in time — check that Long Poll API and the "
                "'Входящие сообщения' event type are enabled for the community",
                retryable=True)
            return False
        self._mark_connected()
        logger.info(
            "VK: connected to community %s (id=%s) as @%s", self.client.group_name or "?", self.client.group_id,
            self.client.group_name or "vk")
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        with contextlib.suppress(Exception):
            self._release_platform_lock()
        self._mark_disconnected()
        await cancel_task(self._poll_task)
        self._poll_task = None
        await cancel_task(getattr(self, "_activity_task", None))
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.close()
            self.client = None

    async def _acquire_long_poll(self) -> Tuple[str, str, Any]:
        if self.client is None:
            raise RuntimeError("not connected")
        server = await self.client.get_long_poll_server()
        return str(server.get("server", "")), str(server.get("key", "")), server.get("ts", 0)

    # ------------------------------------------------------------------ long poll

    async def _poll_loop(self) -> None:
        backoff = 1.0
        consecutive_errors = 0
        while True:
            if self.client is None:
                return
            server, key, ts = self._conn
            try:
                body = await self.client.poll(server, key, ts, wait=LONG_POLL_WAIT)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                # RETRY FROM THE SAME CURSOR. VK buffers updates for ~5 minutes and replays them for a
                # repeated ``ts``, so a transport failure costs nothing if we keep the cursor.
                # Re-acquiring here (``groups.getLongPollServer`` hands back the CURRENT position) jumps
                # past everything that arrived during the outage — the real symptom was a callback
                # button spinning forever with nothing in the log, while the community had the event
                # enabled. ``vk-io`` (the reference implementation) also restarts the loop, not the
                # session, for transport errors; only ``failed`` codes invalidate the cursor.
                logger.warning("VK: long poll error #%d (%s); retrying from the same cursor in %.0fs",
                               consecutive_errors, exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                if consecutive_errors >= 5:
                    # The cursor itself has gone stale (VK keeps ~5 minutes); take the server's.
                    logger.warning("VK: %d consecutive poll failures; re-acquiring the long-poll session",
                                   consecutive_errors)
                    try:
                        self._conn = await self._acquire_long_poll()
                    except Exception as exc:
                        logger.warning("VK: re-acquire failed: %s", exc)
                    else:
                        backoff = 1.0  # the network just answered, so stop backing off
                    consecutive_errors = 0
                continue
            backoff, consecutive_errors = 1.0, 0
            self._last_poll_ok = time.monotonic()
            if self._first_poll_done is not None:
                self._first_poll_done.set()
            failed = body.get("failed")
            if failed:
                if int(failed) == 1 and "ts" in body:
                    self._conn = (server, key, body.get("ts"))
                    continue
                logger.info("VK: long poll session dropped (failed=%s); re-acquiring", failed)
                try:
                    self._conn = await self._acquire_long_poll()
                except Exception as exc:
                    logger.warning("VK: re-acquire failed: %s", exc)
                    await self._sleep(5.0)
                continue
            self._conn = (server, key, body.get("ts", ts))
            for update in body.get("updates") or []:
                try:
                    await self._dispatch(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("VK: update handling failed (%s)", (update or {}).get("type"))

    @staticmethod
    async def _sleep(seconds: float) -> None:
        await asyncio.sleep(seconds * (0.8 + random.random() * 0.4))

    def transport_liveness(self) -> Dict[str, Any]:
        """Seconds since the last completed poll request (diagnostics / tests)."""
        return {"last_poll_ok_seconds_ago": round(time.monotonic() - self._last_poll_ok, 1),
                "silent": (time.monotonic() - self._last_poll_ok) > TRANSPORT_SILENCE_SECONDS}

    # ------------------------------------------------------------------ inbound dispatch

    async def _dispatch(self, update: Dict[str, Any]) -> None:
        kind = (update or {}).get("type") or ""
        obj = (update or {}).get("object") or {}
        if kind in {"message_new", "message_reply", "message_edit"}:
            message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
            await self._handle_inbound(message, update_id=str((update or {}).get("event_id") or ""))
        elif kind == "message_event":
            logger.info("VK: message_event received (peer=%s user=%s event=%s)",
                        obj.get("peer_id"), obj.get("user_id"), obj.get("event_id"))
            await self._handle_button_event(obj)
        elif kind not in _QUIET_UPDATE_TYPES:
            # Log anything unexpected: an unhandled update type used to be dropped in total silence,
            # which is indistinguishable from "the event never arrived" when debugging a button press.
            logger.info("VK: ignoring update type %s", kind or "<empty>")

    async def _handle_inbound(self, message: Dict[str, Any], *, update_id: str = "") -> None:
        if not isinstance(message, dict):
            return
        if message.get("out"):  # an outgoing (bot/community) message
            return
        peer_id = int(message.get("peer_id") or 0)
        from_id = int(message.get("from_id") or 0)
        if peer_id <= 0 or from_id <= 0 or from_id == self.group_id:
            return
        message_id = str(message.get("id") or "")
        key = update_id or message_id
        if not key or self._dedup.is_duplicate(key):
            return
        chat_id = str(peer_id)
        is_group = _is_group(peer_id)
        if message_id:
            self._last_inbound[chat_id] = message_id
            if len(self._last_inbound) > 500:
                self._last_inbound.pop(next(iter(self._last_inbound)))

        text = (message.get("text") or "").strip()
        media_urls: List[str] = []
        media_types: List[str] = []
        notes: List[str] = []
        await self._collect_attachments(message.get("attachments") or [], media_urls, media_types, notes)
        if message.get("geo"):
            notes.append("[геопозиция]")
        forwards = message.get("fwd_messages") or []
        if forwards:
            notes.append(f"[пересланных сообщений: {len(forwards)}]")
        if notes and not text.startswith("/"):
            # Sent text and attachment/geo/forward notes share one string so the agent can see what
            # arrived. For a slash command they must stay apart: the core reads everything after the
            # command word as its arguments, so "/new" + a photo would arrive as "/new\n[вложение: …]"
            # and hand the note to /new (session name), /title, /save … as an argument. The attachment
            # itself is unaffected — it still travels in media_urls/media_types.
            text = (text + "\n" + " ".join(notes)).strip()

        reply = message.get("reply_message") or {}
        reply_to_text = (reply.get("text") or "").strip() if isinstance(reply, dict) else ""
        reply_to_author = None
        if reply_to_text or reply.get("id"):
            reply_to_text = reply_to_text[:2000]
            if int(reply.get("from_id") or 0) == -self.group_id:
                reply_to_author = self.name

        user_name = await self._user_name(from_id)
        chat_name = await self._chat_name(chat_id, peer_id, user_name)
        source = self.build_source(
            chat_id=chat_id, chat_name=chat_name, chat_type="group" if is_group else "dm",
            user_id=str(from_id), user_name=user_name, message_id=message_id or None)
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, user_id=str(from_id), user_name=user_name,
            source=source, raw_message=message, message_id=message_id or None,
            media_urls=media_urls, media_types=media_types,
            reply_to_message_id=str(reply.get("id")) if reply.get("id") else None,
            reply_to_text=reply_to_text or None, reply_to_author_name=reply_to_author,
            timestamp=datetime.datetime.fromtimestamp(int(message.get("date") or time.time())),
        )
        await self.handle_message(event)

    async def _collect_attachments(
        self, attachments: List[Dict[str, Any]], media_urls: List[str], media_types: List[str], notes: List[str],
    ) -> None:
        """Download inbound media into the agent's media cache; describe the rest."""
        for att in attachments:
            if not isinstance(att, dict):
                continue
            kind = att.get("type")
            body = att.get(kind) or {}
            try:
                if kind == "photo":
                    sizes = body.get("sizes") or []
                    url = max(sizes, key=lambda s: int(s.get("width") or 0)).get("url") if sizes else None
                    if url:
                        data = await self.client.download(url)
                        media_urls.append(cache_image_from_bytes(data, ".jpg"))
                        media_types.append("image/jpeg")
                    notes.append("[фото]")
                elif kind == "doc":
                    url, title = body.get("url"), str(body.get("title") or "file")
                    if url:
                        data = await self.client.download(url)
                        media_urls.append(cache_document_from_bytes(data, title))
                        media_types.append(mimetypes.guess_type(title)[0] or "application/octet-stream")
                    notes.append(f"[документ: {title}]")
                elif kind == "audio_message":
                    url = body.get("link_ogg") or body.get("link_mp3")
                    if url:
                        data = await self.client.download(url)
                        media_urls.append(cache_audio_from_bytes(data, ".ogg"))
                        media_types.append("audio/ogg")
                    notes.append(f"[голосовое сообщение, {int(body.get('duration') or 0)} с]")
                elif kind == "audio":
                    notes.append(f"[аудио: {body.get('artist', '')} — {body.get('title', '')}]")
                elif kind == "video":
                    notes.append(f"[видео: {body.get('title') or body.get('id')}]")
                elif kind == "sticker":
                    notes.append("[стикер]")
                elif kind == "wall":
                    notes.append("[запись со стены]")
                elif kind == "link":
                    notes.append(f"[ссылка: {body.get('url')}]")
                else:
                    notes.append(f"[{kind}]")
            except Exception as exc:
                logger.info("VK: attachment %s could not be fetched: %s", kind, exc)
                notes.append(f"[{kind}: не удалось загрузить]")

    async def _user_name(self, user_id: int) -> str:
        if user_id in self._user_names:
            return self._user_names[user_id]
        name = f"id{user_id}"
        with contextlib.suppress(Exception):
            if self.client is not None:
                names = await self.client.user_names([user_id])
                name = names.get(user_id) or name
        self._user_names[user_id] = name
        return name

    async def _chat_name(self, chat_id: str, peer_id: int, user_name: str) -> str:
        if not _is_group(peer_id):
            return user_name
        if chat_id in self._chat_titles and self._chat_titles[chat_id]:
            return self._chat_titles[chat_id]
        title = ""
        with contextlib.suppress(Exception):
            if self.client is not None:
                title = await self.client.chat_title(peer_id)
        self._chat_titles[chat_id] = title or f"беседа {peer_id - GROUP_PEER_OFFSET}"
        return self._chat_titles[chat_id]

    # ------------------------------------------------------------------ buttons

    def _sender_authorized(self, user_id: int, chat_id: str, is_group: bool) -> bool:
        """Gate a button press with the same authorization the inbound path uses."""
        verdict = self._is_sender_authorized(
            str(user_id), "group" if is_group else "dm", chat_id)
        return verdict is not False

    async def _handle_button_event(self, obj: Dict[str, Any]) -> None:
        if not isinstance(obj, dict):
            return
        user_id = int(obj.get("user_id") or 0)
        peer_id = int(obj.get("peer_id") or 0)
        event_id = str(obj.get("event_id") or "")
        payload = obj.get("payload")
        if isinstance(payload, str):
            with contextlib.suppress(Exception):
                payload = json.loads(payload)
        payload = payload if isinstance(payload, dict) else {}
        if self._dedup.is_duplicate(event_id or f"{user_id}:{peer_id}:{payload}"):
            return
        if not self._sender_authorized(user_id, str(peer_id), _is_group(peer_id)):
            logger.warning("VK: button press refused — user %s is not authorized", user_id)
            await self._answer_event(event_id, user_id, peer_id, "Недостаточно прав")
            return
        action = str(payload.get("v") or "")
        logger.info("VK: button press from user=%s peer=%s action=%s payload_id=%s",
                    user_id, peer_id, action or "<none>", payload.get("id"))
        if action == "cl":
            await self._resolve_clarify(payload, event_id, user_id, peer_id)
        elif action == "ea":
            await self._resolve_approval(payload, event_id, user_id, peer_id)
        elif action == "sc":
            await self._resolve_slash_confirm(payload, event_id, user_id, peer_id)
        else:
            await self._answer_event(event_id, user_id, peer_id, "Кнопка устарела")

    async def _answer_event(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        if not event_id or self.client is None:
            return
        try:
            await self.client.answer_event(event_id, user_id, peer_id, to_plain(text))
        except Exception as exc:
            # Never swallow silently: an unanswered press leaves the user's button spinning forever,
            # and without this line that failure is indistinguishable from "the event never arrived".
            logger.warning("VK: could not answer button event %s: %s", event_id, exc)

    async def _resolve_clarify(self, payload: Dict[str, Any], event_id: str, user_id: int, peer_id: int) -> None:
        clarify_id = str(payload.get("id") or "")
        token = str(payload.get("c") or "")
        if not clarify_id:
            return
        if token == "other":
            flipped = False
            with contextlib.suppress(Exception):
                from tools.clarify_gateway import mark_awaiting_text
                flipped = mark_awaiting_text(clarify_id)
            if not flipped:
                self._clarify_state.pop(clarify_id, None)
                await self._answer_event(event_id, user_id, peer_id, "Вопрос устарел")
                return
            await self._answer_event(event_id, user_id, peer_id, "Напишите ответ сообщением")
            return
        response: Optional[str] = None
        with contextlib.suppress(Exception):
            from tools.clarify_gateway import _entries as _clarify_entries
            entry = _clarify_entries.get(clarify_id)
            if entry is not None and entry.choices and token.isdigit() and int(token) < len(entry.choices):
                response = str(entry.choices[int(token)])
        # Deliberately no fallback to a made-up "choice N": resolve_gateway_clarify stores this
        # string verbatim as the user's answer, so an index the prompt never offered (stale or
        # forged payload) would reach the agent as if the user had chosen it.
        if not response:
            await self._answer_event(event_id, user_id, peer_id, "Некорректный вариант")
            return
        resolved = False
        with contextlib.suppress(Exception):
            from tools.clarify_gateway import resolve_gateway_clarify
            resolved = resolve_gateway_clarify(clarify_id, response)
        self._clarify_state.pop(clarify_id, None)
        await self._answer_event(event_id, user_id, peer_id, f"✓ {response[:60]}" if resolved else "Вопрос устарел")

    async def _resolve_approval(self, payload: Dict[str, Any], event_id: str, user_id: int, peer_id: int) -> None:
        choice = str(payload.get("c") or "")
        approval_id = str(payload.get("id") or "")
        session_key = self._approval_state.pop(approval_id, None)
        if choice not in {"once", "session", "always", "deny"} or not session_key:
            await self._answer_event(event_id, user_id, peer_id, "Команда уже неактуальна")
            return
        count = 0
        with contextlib.suppress(Exception):
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(session_key, choice)
        label = {"once": "Разрешено один раз", "session": "Разрешено до конца сессии",
                 "always": "Разрешено всегда", "deny": "Отклонено"}[choice]
        await self._answer_event(event_id, user_id, peer_id, label if count else "Команда уже неактуальна")
        if count:
            self.resume_typing_for_chat(str(peer_id))
            await self.send(str(peer_id), f"✅ {label}")

    async def _resolve_slash_confirm(self, payload: Dict[str, Any], event_id: str, user_id: int, peer_id: int) -> None:
        choice = str(payload.get("c") or "")
        confirm_id = str(payload.get("id") or "")
        session_key = self._slash_confirm_state.pop(confirm_id, None)
        if choice not in {"once", "always", "cancel"} or not session_key:
            await self._answer_event(event_id, user_id, peer_id, "Подтверждение устарело")
            return
        label = {"once": "Выполнено", "always": "Выполнено всегда", "cancel": "Отменено"}[choice]
        await self._answer_event(event_id, user_id, peer_id, label)
        with contextlib.suppress(Exception):
            from tools import slash_confirm as _slash_confirm_mod
            result_text = await _slash_confirm_mod.resolve(session_key, confirm_id, choice)
            if result_text:
                await self.send(str(peer_id), result_text)

    # ------------------------------------------------------------------ outbound

    def _reply_anchor(self, chat_id: str, reply_to: Optional[str]) -> Optional[int]:
        candidate = reply_to or (self._last_inbound.get(str(chat_id)) if self.quote_in_groups else None)
        if not candidate:
            return None
        with contextlib.suppress(TypeError, ValueError):
            return int(candidate)
        return None

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if self.client is None:
            return SendResult(success=False, error="Not connected")
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error=f"invalid VK peer id: {chat_id!r}")
        chunks = render_chunks(content or "", self.MAX_MESSAGE_LENGTH)
        # DMs are never quoted; a group answer quotes the inbound message (the reference behaviour).
        anchor = self._reply_anchor(chat_id, reply_to) if _is_group(peer_id) else None
        last_id, sent_any = None, False
        for index, (text, format_data) in enumerate(chunks):
            if not text.strip() and len(chunks) > 1:
                continue
            try:
                last_id = await self.client.send_message(
                    peer_id, text, reply_to=anchor if index == 0 else None, format_data=format_data,
                    keyboard=command_keyboard() if self.command_keyboard else None)
                sent_any = True
            except VkApiError as exc:
                # VK error 100 on messages.send usually means a malformed format_data/keyboard;
                # the plain text always goes through, so degrade instead of dropping the answer.
                if exc.code == 100 and format_data:
                    try:
                        last_id = await self.client.send_message(
                            peer_id, text, reply_to=anchor if index == 0 else None)
                        sent_any = True
                        continue
                    except Exception as retry_exc:
                        exc = VkApiError("messages.send", 0, str(retry_exc))
                logger.warning("VK: send failed to peer %s: %s", peer_id, exc)
                return SendResult(success=False, error=str(exc), error_kind=exc.error_kind, retryable=exc.retryable)
            except Exception as exc:
                return SendResult(success=False, error=str(exc), error_kind="unknown", retryable=True)
            await asyncio.sleep(0.05)  # be polite to the 20 req/s community limit
        if not sent_any:
            return SendResult(success=False, error="nothing to send")
        return SendResult(success=True, message_id=str(last_id or ""))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        if self.client is None:
            return
        with contextlib.suppress(Exception):
            await self.client.set_activity(int(chat_id), activity="typing")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return {"name": str(chat_id), "type": "dm", "chat_id": str(chat_id)}
        if _is_group(peer_id):
            name = await self._chat_name(str(peer_id), peer_id, "")
            return {"name": name, "type": "group", "chat_id": str(peer_id)}
        name = await self._user_name(peer_id)
        return {"name": name, "type": "dm", "chat_id": str(peer_id)}

    # ------------------------------------------------------------------ interactive prompts

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Numbered callback buttons per choice plus "✏️ Свой ответ" (flips to text capture)."""
        text = f"❓ {question}"
        rows: List[List[Tuple[str, Optional[Dict[str, Any]], str]]] = []
        if choices:
            text += "\n\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(choices))
            rows = [[(str(i + 1), {"v": "cl", "id": clarify_id, "c": str(i)}, "secondary")]
                    for i in range(len(choices))]
            rows.append([("✏️ Свой ответ", {"v": "cl", "id": clarify_id, "c": "other"}, "secondary")])
            self._clarify_state[clarify_id] = session_key
        else:
            with contextlib.suppress(Exception):
                from tools.clarify_gateway import mark_awaiting_text
                mark_awaiting_text(clarify_id)
        return await self._send_keyboard(chat_id, text, rows, metadata)

    async def _send_exec_approval_prompt(self, prompt: ExecApprovalPrompt) -> SendResult:
        """Native Approve/Deny buttons; a press resolves via ``tools.approval``."""
        approval_id = uuid.uuid4().hex[:12]
        self._approval_state[approval_id] = prompt.session_key
        rows = [[(label, {"v": "ea", "id": approval_id, "c": choice}, "positive" if choice != "deny" else "negative")]
                for label, choice, _style in (prompt.actions or [])]
        pairs: List[List[Tuple[str, Optional[Dict[str, Any]], str]]] = []
        for index in range(0, len(rows), 2):
            pairs.append(rows[index] + rows[index + 1: index + 2])
        return await self._send_keyboard(prompt.chat_id, prompt.text, pairs, prompt.metadata)

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        self._slash_confirm_state[confirm_id] = session_key
        rows = [[
            ("Выполнить", {"v": "sc", "id": confirm_id, "c": "once"}, "positive"),
            ("Всегда", {"v": "sc", "id": confirm_id, "c": "always"}, "secondary"),
            ("Отмена", {"v": "sc", "id": confirm_id, "c": "cancel"}, "negative"),
        ]]
        return await self._send_keyboard(chat_id, f"*{title}*\n\n{message}", rows, metadata)

    async def _send_keyboard(self, chat_id: str, text: str, rows, metadata=None) -> SendResult:
        if self.client is None:
            return SendResult(success=False, error="Not connected")
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return SendResult(success=False, error=f"invalid VK peer id: {chat_id!r}")
        try:
            message_id = await self.client.send_message(
                peer_id, to_plain(text, self.MAX_MESSAGE_LENGTH)[: self.MAX_MESSAGE_LENGTH],
                keyboard=_keyboard(rows))
            return SendResult(success=True, message_id=str(message_id or ""))
        except Exception as exc:
            logger.warning("VK: keyboard send failed: %s", exc)
            return await self.send(chat_id, text, metadata=metadata)

    # ------------------------------------------------------------------ outbound media

    async def _upload_bytes(self, data: bytes, filename: str, *, kind: str) -> str:
        if self.client is None:
            raise RuntimeError("not connected")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"{filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        return await (self.client.upload_photo(data, filename) if kind == "photo"
                      else self.client.upload_document(data, filename, kind=kind))

    async def _send_attachment(self, chat_id: str, source: str, *, kind: str, caption: Optional[str] = None,
                               filename: Optional[str] = None, reply_to: Optional[str] = None,
                               metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        """Upload a local file (or fetch a URL) and send it with an optional caption."""
        if self.client is None:
            return SendResult(success=False, error="Not connected")
        name = filename or os.path.basename(source.split("?")[0]) or ("file" if kind != "photo" else "image.jpg")
        try:
            if source.startswith(("http://", "https://")):
                data = await self.client.download(source)
            else:
                path = source[7:] if source.startswith("file://") else source
                if not os.path.exists(path):
                    return SendResult(success=False, error=f"file not found: {path}", error_kind="not_found")
                with open(path, "rb") as handle:
                    data = handle.read()
            upload_kind = "audio_message" if kind == "voice" else kind
            if kind == "voice":
                data = await self._to_ogg_opus(data)
                name = (os.path.splitext(name)[0] or "voice") + ".ogg"
            attachment = await self._upload_bytes(data, name, kind=upload_kind)
        except Exception as exc:
            logger.warning("VK: %s upload failed: %s", kind, exc)
            return SendResult(success=False, error=str(exc), error_kind="unknown", retryable=True)
        try:
            peer_id = int(chat_id)
            message_id = await self.client.send_message(
                peer_id, to_plain(caption or "", self.MAX_MESSAGE_LENGTH)[: self.MAX_MESSAGE_LENGTH],
                attachment=attachment, reply_to=self._reply_anchor(chat_id, reply_to) if _is_group(peer_id) else None)
            return SendResult(success=True, message_id=str(message_id or ""))
        except Exception as exc:
            return SendResult(success=False, error=str(exc), error_kind="unknown", retryable=True)

    async def _to_ogg_opus(self, data: bytes) -> bytes:
        return await _voice_to_ogg_opus(data)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return await self._send_attachment(
            chat_id, image_url, kind="photo", caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                              **kwargs) -> SendResult:
        return await self._send_attachment(
            chat_id, image_path, kind="photo", caption=caption, reply_to=reply_to, metadata=metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: Optional[Dict[str, Any]] = None, **kwargs) -> SendResult:
        return await self._send_attachment(
            chat_id, file_path, kind="doc", caption=caption, filename=file_name,
            reply_to=reply_to, metadata=metadata)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        result = await self._send_attachment(
            chat_id, audio_path, kind="voice", caption=caption, reply_to=reply_to, metadata=metadata)
        if not result.success:  # e.g. no ffmpeg → deliver the audio as a file instead of losing it
            return await self._send_attachment(
                chat_id, audio_path, kind="doc", caption=caption, reply_to=reply_to, metadata=metadata)
        return result


# ── plugin registration ──────────────────────────────────────────────────────

def check_requirements() -> bool:
    """PASSIVE probe: is a VK community token configured?"""
    return bool(str(get_scoped_secret("VK_TOKEN", "") or "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    return bool(str(extra_or_secret(extra, "token", "VK_TOKEN", "") or "").strip())


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env so env-only setups appear in status/cron."""
    token = str(get_scoped_secret("VK_TOKEN", "") or "").strip()
    if not token:
        return None
    seed = seed_extra_from_env((
        ("VK_GROUP_ID", "group_id", int),
        ("VK_API_VERSION", "api_version", None),
        ("VK_QUOTE_IN_GROUPS", "quote_in_groups", lambda v: v.strip().lower() in {"1", "true", "yes", "on"}),
    ), home_env="VK_HOME_CHANNEL")
    return {"token": token, **seed}


async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[List[str]] = None, force_document: bool = False) -> Dict[str, Any]:
    """Out-of-process cron delivery: open a client, send, close."""
    extra = getattr(pconfig, "extra", {}) or {}
    token = str(extra_or_secret(extra, "token", "VK_TOKEN", "") or "").strip()
    if not token:
        return send_error("VK standalone send: VK_TOKEN is not configured")
    # Same keyboard as the live adapter: cron reports and `hermes send` land in the SAME chat, and a
    # message without it may leave the client without the buttons the user enabled.
    keyboard = command_keyboard() if _truthy(extra, "VK_COMMAND_KEYBOARD", "command_keyboard", False) else None
    client = VkClient(token, api_version=str(extra.get("api_version") or DEFAULT_API_VERSION))
    try:
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return send_error(f"VK standalone send: invalid peer id {chat_id!r}")
        await client.resolve_group()
        chunks = render_chunks(message or "", VKAdapter.MAX_MESSAGE_LENGTH)
        # The host passes ``(path, is_voice)`` tuples (BasePlatformAdapter.filter_media_delivery_paths);
        # a bare path is accepted too so ad-hoc callers work.
        media = [(item if isinstance(item, (tuple, list)) else (item, False)) for item in (media_files or [])]
        media = [(path, bool(is_voice)) for path, is_voice in media if path and os.path.exists(path)]
        last_id = None
        # VK carries text and one attachment in the same message, so a single-chunk caption rides
        # along with the first file instead of costing the user an extra message.
        pending = [(text, fmt) for text, fmt in chunks if text.strip() or len(chunks) == 1]
        pairing = pending[0] if (media and len(pending) == 1) else None
        if pairing is None:
            for text, format_data in pending:
                if not text.strip() and len(pending) > 1:
                    continue
                last_id = await client.send_message(peer_id, text, format_data=format_data, keyboard=keyboard)
        for index, (path, is_voice) in enumerate(media):
            with open(path, "rb") as handle:
                data = handle.read()
            name = os.path.basename(path)
            if is_voice:
                data = await _voice_to_ogg_opus(data)
                name = (os.path.splitext(name)[0] or "voice") + ".ogg"
                attachment = await client.upload_document(data, name, kind="audio_message")
            elif not force_document and (mimetypes.guess_type(path)[0] or "").startswith("image/"):
                attachment = await client.upload_photo(data, name)
            else:
                attachment = await client.upload_document(data, name)
            caption, caption_fmt = pairing if (pairing and index == 0) else ("", None)
            last_id = await client.send_message(
                peer_id, caption, format_data=caption_fmt, attachment=attachment, keyboard=keyboard)
        return {"success": True, "message_id": str(last_id or "")}
    except VkApiError as exc:
        return send_error(f"VK standalone send failed: {exc}")
    except Exception as exc:
        logger.debug("VK standalone send raised", exc_info=True)
        return send_error(f"VK standalone send failed: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await client.close()


def interactive_setup() -> None:
    """``hermes gateway setup`` entry: token + access control, with community instructions."""
    from hermes_cli.setup import (
        get_env_value, print_header, print_info, print_success, print_warning, prompt,
        prompt_yes_no, save_env_value,
    )
    from hermes_cli.setup_platforms import declines_reconfigure

    print_header("VK (ВКонтакте)")
    if declines_reconfigure("VK", "Перенастроить VK?", "VK_TOKEN"):
        return
    print_info(
        "Нужно сообщество ВКонтакте (не личная страница):",
        "  1. Создайте сообщество → Управление → Сообщения → включите сообщения.",
        "  2. Управление → Работа с API → Ключи доступа → Создать ключ:",
        "     права «Сообщения сообщества» + «Управление сообществом».",
        "  3. Управление → Работа с API → Long Poll API: включить и отметить",
        "     события «Входящие сообщения» и «Действие с сообщением».",
    )
    token = prompt("Ключ доступа сообщества", password=True, default="")
    if not token:
        print_warning("Токен не введён — настройка VK пропущена")
        return
    save_env_value("VK_TOKEN", token.strip())
    home = prompt("Ваш VK id или id беседы для отчётов по расписанию (необязательно)",
                  default=get_env_value("VK_HOME_CHANNEL") or "")
    if home:
        save_env_value("VK_HOME_CHANNEL", home.strip())
    if prompt_yes_no("Разрешить писать боту всем?", False):
        save_env_value("VK_ALLOW_ALL_USERS", "true")
        save_env_value("VK_ALLOWED_USERS", "")
        print_warning("⚠️  Открытый доступ — боту сможет писать любой пользователь VK.")
    else:
        save_env_value("VK_ALLOW_ALL_USERS", "false")
        allowed = prompt("Разрешённые VK id (через запятую; пусто — доступ через pairing-код)",
                         default=get_env_value("VK_ALLOWED_USERS") or "")
        save_env_value("VK_ALLOWED_USERS", allowed.replace(" ", ""))
    print_success("Настройки VK сохранены в ~/.hermes/.env")
    print_info("Перезапустите шлюз: hermes gateway restart")


def register(ctx) -> None:
    """Plugin entry point."""
    ctx.register_platform(
        name="vk",
        label="VK",
        emoji="🔵",
        adapter_factory=VKAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        setup_fn=interactive_setup,
        required_env=["VK_TOKEN"],
        install_hint="No extra packages needed (aiohttp ships with Hermes)",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="VK_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="VK_ALLOWED_USERS",
        allow_all_env="VK_ALLOW_ALL_USERS",
        max_message_length=VKAdapter.MAX_MESSAGE_LENGTH,
        allow_update_command=True,
        pii_safe=False,
        platform_hint=(
            "You are chatting via VK (ВКонтакте) as a community bot. VK renders only **bold**, "
            "*italic*, ~~strike~~ and [links](url); there are no code blocks, so keep code short and "
            "plain. Messages are capped at 4096 characters and are split automatically. In group chats "
            "your reply quotes the user's message. Buttons (choice prompts, command approvals) arrive as "
            "keyboard taps. Keep answers conversational."),
    )
