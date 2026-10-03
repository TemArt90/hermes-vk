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
import glob
import json
import logging
import mimetypes
import os
import random
import re
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
from gateway.platforms.event import MessageEvent, MessageType, ProcessingOutcome
from gateway.platforms.helpers import MessageDeduplicator, cancel_task, compile_mention_patterns

from .vk_api import DEFAULT_API_VERSION, VkApiError, VkClient
from .vk_markdown import render_chunks, to_plain

logger = logging.getLogger(__name__)

GROUP_PEER_OFFSET = 2_000_000_000  # VK: conversation peer ids start here
LONG_POLL_WAIT = 25
FIRST_POLL_TIMEOUT = 35.0
TRANSPORT_SILENCE_SECONDS = 150.0
# Fallback sweep: a history poll used only while Long Poll has been silent for a whole interval. It is
# a net under the primary path, never a second path — a healthy Long Poll must see no traffic from it.
FALLBACK_POLL_INTERVAL = 60      # seconds between sweeps
FALLBACK_POLL_BATCH = 20         # conversations examined per sweep
# Deduplication window for inbound update ids. VK replays buffered updates for ~5 minutes after a
# hiccup, so a window shorter than that trades away protection and buys nothing.
DEFAULT_DEDUPE_TTL_SECONDS = 900
MIN_DEDUPE_TTL_SECONDS = 30
MAX_BUTTON_LABEL = 40
MAX_CALLBACK_PAYLOAD = 250
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# Inbound media cap. A document or an audio message can exceed it; photos and voice notes do not.
DEFAULT_MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# VK reaction numbers. 4 = 👍, 10 = 👌, 8 = 👎 in the default community set — but VK numbers reactions
# per community, so every step stays overridable and 0 switches a step off.
DEFAULT_REACTION_PROGRESS = 10
DEFAULT_REACTION_OK = 4
DEFAULT_REACTION_FAIL = 8
VOICE_MAX_SECONDS = 300
# Update types that are expected noise for a bot (never worth a log line when enabled).
_QUIET_UPDATE_TYPES = frozenset({"message_typing_state", "message_read", "message_allow", "message_deny"})


def _peer_bool_map(raw: Any) -> Dict[int, bool]:
    """Parse per-chat booleans: a ``{peer: bool}`` map (config) or ``"peer:true,peer:false"`` (env).

    Anything unparsable is dropped rather than defaulted, so a typo cannot silently flip a chat's
    policy — an unlisted chat always inherits the global setting.
    """
    pairs: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, str):
        for part in raw.split(","):
            if ":" in part:
                key, _, value = part.partition(":")
                pairs.append((key, value))
    out: Dict[int, bool] = {}
    for key, value in pairs:
        try:
            peer = int(str(key).strip())
        except (TypeError, ValueError):
            continue
        if isinstance(value, bool):  # a real boolean keeps its own value, unlike the string "false"
            out[peer] = value
            continue
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            out[peer] = True
        elif text in {"0", "false", "no", "off"}:
            out[peer] = False
    return out


def _reaction_id(raw: Any, default: int) -> int:
    """Parse a configured reaction number; anything unparsable falls back to the platform default."""
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _dedupe_ttl(raw: Any) -> int:
    """The deduplication window in seconds; junk or too-small values fall back to the default.

    Deliberately NOT "0 disables deduplication" (the semantics the sibling VK plugin gives this knob): an
    operator reaching for 0 almost always wants "back to defaults", and reading it as "turn protection
    off" would let the channel answer the same redelivered message twice. To genuinely disable dedupe,
    pass 0 through ``extra``/``.env`` and it is ignored the same way — there is no silent switch for it.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_DEDUPE_TTL_SECONDS
    return value if value >= MIN_DEDUPE_TTL_SECONDS else DEFAULT_DEDUPE_TTL_SECONDS


def _vk_mention_patterns(group_id: int, group_name: str) -> List[str]:
    """Wake-word patterns for one community: its link-mention, ``@club<id>`` and its plain name.

    VK writes a community mention as ``[club<id>|<name>]`` inside the message text, so the id forms
    are exact. The plain name is a convenience for people who type it; a name shorter than three
    characters is skipped, because it would match unrelated words.
    """
    patterns: List[str] = []
    if group_id:
        patterns.append(rf"\[club{int(group_id)}\|[^\]]*\]")
        patterns.append(rf"(?<![\w@])@club{int(group_id)}\b")
    name = (group_name or "").strip()
    if len(name) >= 3:
        patterns.append(rf"(?<![\w@])@?{re.escape(name)}\b")
    return patterns


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


def keyboard_for_peer(extra: Optional[dict], chat_id: str, fallback: bool = False) -> Optional[str]:
    """The command keyboard for one chat, or ``None``.

    Shared by the live adapter and the out-of-process cron sender on purpose: both must reach the same
    decision, or a scheduled report arrives without the buttons the operator enabled for that chat. A
    per-chat entry (``VK_COMMAND_KEYBOARD_BY_PEER='123456:true,789:false'``) wins over the global flag;
    a chat with no entry inherits it.
    """
    extra = extra or {}
    enabled = _truthy(extra, "VK_COMMAND_KEYBOARD", "command_keyboard", fallback)
    overrides = _peer_bool_map(
        _env(extra, "VK_COMMAND_KEYBOARD_BY_PEER", "command_keyboard_by_peer", None))
    text = str(chat_id if chat_id is not None else "")
    if text.isdigit() and int(text) in overrides:
        enabled = overrides[int(text)]
    return command_keyboard() if enabled else None


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


def _find_ffmpeg() -> Optional[str]:
    """ffmpeg for voice transcoding: ``PATH`` first, then the copy Hermes bundles under ``tools/``.

    The fallback covers hosts whose service PATH carries no ffmpeg at all. It is *not* a repair of a
    broken voice path here: measured afterwards, ``/usr/bin/ffmpeg`` is on the gateway's PATH and
    encodes Opus fine. Keep both, prefer whatever the operator put on PATH.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    hermes_home = os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
    for candidate in sorted(glob.glob(os.path.join(hermes_home, "tools", "ffmpeg-*", "bin", "ffmpeg")),
                            reverse=True):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


async def _voice_to_ogg_opus(data: bytes, *, max_seconds: int = VOICE_MAX_SECONDS) -> bytes:
    """VK voice messages must be Ogg Opus; transcode with ffmpeg when it is available."""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        logger.info("VK: ffmpeg not found — the voice message is sent uncompressed (VK may reject it)")
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
        # Per-chat override of that keyboard: one chat may want the buttons, another may find them in
        # the way. Chats without an entry inherit the global flag.
        self.command_keyboard_by_peer = _peer_bool_map(
            _env(extra, "VK_COMMAND_KEYBOARD_BY_PEER", "command_keyboard_by_peer", None))
        # Group chats: with require_mention on, the community answers only when addressed. Off by
        # default — it changes what the bot responds to, so it is opt-in like every other switch.
        self.require_mention = _truthy(extra, "VK_REQUIRE_MENTION", "require_mention", False)
        self.require_mention_by_peer = _peer_bool_map(
            _env(extra, "VK_REQUIRE_MENTION_BY_PEER", "require_mention_by_peer", None))
        self.mention_patterns_raw = _env(extra, "VK_MENTION_PATTERNS", "mention_patterns", None)
        # Inbound media: the download itself can be switched off, and its cap is per install.
        self.download_attachments = _truthy(extra, "VK_DOWNLOAD_ATTACHMENTS", "download_attachments", True)
        try:
            self.max_attachment_bytes = int(
                _env(extra, "VK_MAX_ATTACHMENT_BYTES", "max_attachment_bytes", DEFAULT_MAX_ATTACHMENT_BYTES)
                or DEFAULT_MAX_ATTACHMENT_BYTES)
        except (TypeError, ValueError):
            self.max_attachment_bytes = DEFAULT_MAX_ATTACHMENT_BYTES
        self._mention_patterns: List[Any] = []
        # Reactions: opt-in acks on the inbound message, driven by the processing hooks below.
        self.reactions_enabled = _truthy(extra, "VK_REACTIONS_ENABLED", "reactions_enabled", False)
        self.reaction_progress = _reaction_id(
            _env(extra, "VK_REACTION_PROGRESS", "reaction_progress", None), DEFAULT_REACTION_PROGRESS)
        self.reaction_ok = _reaction_id(
            _env(extra, "VK_REACTION_OK", "reaction_ok", None), DEFAULT_REACTION_OK)
        self.reaction_fail = _reaction_id(
            _env(extra, "VK_REACTION_FAIL", "reaction_fail", None), DEFAULT_REACTION_FAIL)
        # VK addresses a reaction by cmid, so the pair (chat, message id) -> cmid is kept for the hooks.
        self._inbound_cmids: Dict[str, int] = {}
        self._delete_reaction_supported: Optional[bool] = None
        self.client: Optional[VkClient] = None
        self._poll_task: Optional[asyncio.Task] = None
        self.dedupe_ttl_seconds = _dedupe_ttl(
            _env(extra, "VK_DEDUPE_TTL_SECONDS", "dedupe_ttl_seconds", None))
        self._dedup = MessageDeduplicator(ttl_seconds=self.dedupe_ttl_seconds)
        self._last_inbound: Dict[str, str] = {}
        self._conn: Tuple[str, str, Any] = ("", "", 0)
        self._last_poll_ok = 0.0
        # Fallback sweep (opt-in): if Long Poll goes quiet for a whole interval, poll the history of the
        # newest conversations so an answer still arrives. Clamped: a zero interval would hammer the API.
        self.fallback_poll = _truthy(extra, "VK_FALLBACK_POLL_ENABLED", "fallback_poll_enabled", False)
        try:
            self.fallback_interval = max(15, int(_env(
                extra, "VK_FALLBACK_POLL_INTERVAL_SECONDS", "fallback_poll_interval_seconds",
                FALLBACK_POLL_INTERVAL) or FALLBACK_POLL_INTERVAL))
        except (TypeError, ValueError):
            self.fallback_interval = FALLBACK_POLL_INTERVAL
        try:
            self.fallback_batch = max(1, int(_env(
                extra, "VK_FALLBACK_POLL_BATCH_SIZE", "fallback_poll_batch_size",
                FALLBACK_POLL_BATCH) or FALLBACK_POLL_BATCH))
        except (TypeError, ValueError):
            self.fallback_batch = FALLBACK_POLL_BATCH
        self._fallback_task: Optional[asyncio.Task] = None
        self._fallback_since = 0.0
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
        # Mention recognition needs the community's id and name, which exist only after resolve_group().
        self._mention_patterns = compile_mention_patterns(
            self.mention_patterns_raw, log_prefix="vk",
            defaults=_vk_mention_patterns(self.client.group_id, self.client.group_name))
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
        # Started only after the first poll answered: a connect that times out never leaves a sweep
        # behind (disconnect() cancels it either way, this keeps the intent obvious).
        self._fallback_since = time.time()
        if self.fallback_poll:
            self._fallback_task = asyncio.create_task(self._fallback_poll_loop())
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
        await cancel_task(getattr(self, "_fallback_task", None))
        self._fallback_task = None
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

    # ------------------------------------------------------------------ fallback sweep

    async def _fallback_sweep(self) -> None:
        """One history sweep: the net under Long Poll for messages it never delivered.

        Runs only while Long Poll has been silent for longer than one interval — a safety net, not a
        second path, and a healthy Long Poll must see no traffic from it. Everything goes through
        ``_handle_inbound``, whose deduplicator already covers a message Long Poll delivers late, so the
        two paths can never hand the agent the same message twice.
        """
        if not self.fallback_poll or self.client is None:
            return
        if time.monotonic() - self._last_poll_ok < self.fallback_interval:
            return
        newest = self._fallback_since
        for item in await self.client.get_conversations(count=self.fallback_batch):
            message = item.get("last_message") or {}
            try:
                date = float(message.get("date") or 0)
            except (TypeError, ValueError):
                continue
            if date > self._fallback_since:
                await self._handle_inbound(message, update_id=f"fb{message.get('id')}")
                newest = max(newest, date)
        self._fallback_since = newest

    async def _fallback_poll_loop(self) -> None:
        """Drive the sweep until ``disconnect()`` cancels this task (same shape as ``_poll_loop``)."""
        while True:
            if self.client is None:
                return
            try:
                await self._fallback_sweep()
            except asyncio.CancelledError:
                raise
            except Exception as exc:      # a sweep must never take the channel down
                logger.debug("VK: fallback sweep failed: %s", exc)
            await self._sleep(self.fallback_interval)

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

    def keyboard_for(self, chat_id: str) -> Optional[str]:
        """Per-chat command keyboard; the rule itself lives in ``keyboard_for_peer`` (shared with cron)."""
        return keyboard_for_peer(
            {"command_keyboard": self.command_keyboard,
             "command_keyboard_by_peer": self.command_keyboard_by_peer},
            chat_id, fallback=self.command_keyboard)

    def _requires_mention(self, peer_id: int) -> bool:
        """Group gating: a per-chat override wins, otherwise the global flag."""
        return self.require_mention_by_peer.get(int(peer_id), self.require_mention)

    def _mentions_bot(self, text: str) -> bool:
        return bool(text) and any(pattern.search(text) for pattern in self._mention_patterns)

    def _strip_mention(self, text: str) -> str:
        """Drop the mention so the agent sees the actual request, not the addressing of the bot."""
        for pattern in self._mention_patterns:
            if pattern.search(text):
                trimmed = pattern.sub("", text, count=1).strip(" ,:;—-—\t")
                return trimmed or text
        return text

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
            with contextlib.suppress(TypeError, ValueError):
                cmid = int(message.get("conversation_message_id") or 0)
                if cmid:
                    # Reactions need VK's cmid, which is not the id messages.send returns, so keep the
                    # pair for the processing hooks. Capped like the inbound-id cache above.
                    self._inbound_cmids[f"{chat_id}:{message_id}"] = cmid
                    if len(self._inbound_cmids) > 500:
                        self._inbound_cmids.pop(next(iter(self._inbound_cmids)))

        text = (message.get("text") or "").strip()
        if is_group and self._requires_mention(peer_id):
            # Gate BEFORE the downloads below: an unmentioned group message must not pull every
            # attachment through the API only to be dropped.
            if not self._mentions_bot(text):
                logger.debug("VK: ignoring group message (require_mention, not addressed): peer=%s", peer_id)
                return
            text = self._strip_mention(text)
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

    async def _download_attachment(self, url: str) -> Optional[bytes]:
        """Fetch one inbound attachment, honouring the size cap; ``None`` when downloads are off.

        One place decides whether inbound media is fetched at all, so the switch cannot drift apart
        between the photo, document and voice paths.
        """
        if not self.download_attachments or self.client is None:
            return None
        return await self.client.download(url, max_bytes=self.max_attachment_bytes)

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
                    data = await self._download_attachment(url) if url else None
                    if data is not None:
                        media_urls.append(cache_image_from_bytes(data, ".jpg"))
                        media_types.append("image/jpeg")
                    notes.append("[фото]")
                elif kind == "doc":
                    url, title = body.get("url"), str(body.get("title") or "file")
                    data = await self._download_attachment(url) if url else None
                    if data is not None:
                        media_urls.append(cache_document_from_bytes(data, title))
                        media_types.append(mimetypes.guess_type(title)[0] or "application/octet-stream")
                    notes.append(f"[документ: {title}]")
                elif kind == "audio_message":
                    url = body.get("link_ogg") or body.get("link_mp3")
                    data = await self._download_attachment(url) if url else None
                    if data is not None:
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
                    keyboard=self.keyboard_for(chat_id))
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

    async def edit_message(self, chat_id: str, message_id: str, content: str,
                           *, finalize: bool = False) -> SendResult:
        """Rewrite an already-sent message in place (``messages.edit``).

        Two VK facts shape this: ``messages.edit`` accepts no ``format_data``, so the text is
        flattened with ``to_plain`` (markup only survives on the original send), and an edit cannot
        split — content that does not fit one message reports failure so the caller sends it anew,
        where splitting works as usual.
        """
        if self.client is None:
            return SendResult(success=False, error="Not connected")
        text = to_plain(content or "", self.MAX_MESSAGE_LENGTH)
        if len(text) > self.MAX_MESSAGE_LENGTH:
            return SendResult(success=False, error="content is longer than one VK message")
        try:
            await self.client.edit_message(int(chat_id), int(message_id), text)
            return SendResult(success=True, message_id=str(message_id))
        except (TypeError, ValueError):
            return SendResult(
                success=False, error=f"invalid VK ids: chat={chat_id!r} message={message_id!r}")
        except Exception as exc:
            logger.warning("VK: message edit failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    # ------------------------------------------------------------------ reactions (opt-in acks)
    #
    # The core drives these through its processing lifecycle hooks — ``on_processing_start`` and
    # ``on_processing_complete(event, outcome)``, which the gateway calls itself around every turn.
    # The generic base implementation swaps *emoji* reactions; VK numbers reactions per community, so
    # these hooks use the configured numbers and the primitives below stay numeric.

    def _reactions_enabled(self, event: Optional[MessageEvent] = None) -> bool:
        return bool(self.reactions_enabled) and self.client is not None

    def _cmid_for(self, chat_id: str, message_id: str) -> Optional[int]:
        return self._inbound_cmids.get(f"{chat_id}:{message_id}")

    async def _add_reaction(self, chat_id: str, message_id: str, reaction_id: int) -> bool:
        """Set our reaction on a message we received; False when it cannot be addressed."""
        cmid = self._cmid_for(str(chat_id), str(message_id))
        if not reaction_id or cmid is None or self.client is None:
            return False
        try:
            await self.client.send_reaction(int(chat_id), cmid, int(reaction_id))
            return True
        except VkApiError as exc:
            # 1009/1010/1011 mean this community's reaction map differs (or is closed) — a fact about
            # the setup, not a transport failure, so it stays at info level and never disturbs a turn.
            logger.info("VK: reaction %s rejected (%s): %s", reaction_id, exc.code, exc.message)
            return False
        except Exception as exc:
            logger.debug("VK: reaction failed: %s", exc)
            return False

    async def _remove_reaction(self, chat_id: str, message_id: str) -> bool:
        """Drop our reaction; skipped entirely once VK reports it has no ``deleteReaction``."""
        cmid = self._cmid_for(str(chat_id), str(message_id))
        if cmid is None or self.client is None or self._delete_reaction_supported is False:
            return False
        try:
            await self.client.delete_reaction(int(chat_id), cmid)
            self._delete_reaction_supported = True
            return True
        except VkApiError as exc:
            if exc.code == 3:  # "Unknown method passed": this community's API has no deleteReaction
                self._delete_reaction_supported = False
            logger.debug("VK: deleteReaction failed (%s): %s", exc.code, exc.message)
            return False
        except Exception as exc:
            logger.debug("VK: deleteReaction failed: %s", exc)
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Ack the triggering message while the agent works (opt-in, off by default)."""
        if not self._reactions_enabled(event) or self.reaction_progress <= 0:
            return
        await self._add_reaction(str(getattr(event.source, "chat_id", "") or ""),
                                 str(getattr(event, "message_id", "") or ""), self.reaction_progress)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Replace the ack with the outcome (👍 / 👎); a cancelled turn is left unreacted.

        VK replaces the sender's previous reaction, so the final number can be sent straight over the
        ack; the delete path exists for the cancelled case and for communities that stack instead.
        """
        if not self._reactions_enabled(event):
            return
        chat_id = str(getattr(event.source, "chat_id", "") or "")
        message_id = str(getattr(event, "message_id", "") or "")
        final_id = {ProcessingOutcome.SUCCESS: self.reaction_ok,
                    ProcessingOutcome.FAILURE: self.reaction_fail}.get(outcome, 0)
        if final_id <= 0:
            await self._remove_reaction(chat_id, message_id)
            return
        await self._add_reaction(chat_id, message_id, final_id)

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
        # Two buttons per row; a lone trailing button is fine. The previous pairing wrapped the second
        # row in a list (``rows[index + 1:index + 2]``), so the keyboard never built and an approval
        # silently fell back to plain text — without buttons AND without the "/approve" instructions
        # the core only adds when an adapter has no button support. Pinned by
        # test_exec_approval_card_carries_every_choice_bound_to_one_prompt.
        buttons = [
            (label, {"v": "ea", "id": approval_id, "c": choice},
             "negative" if choice == "deny" else "positive")
            for label, choice, _style in (prompt.actions or [])]
        rows = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        return await self._send_keyboard(prompt.chat_id, prompt.text, rows, prompt.metadata)

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

    async def _upload_bytes(self, data: bytes, filename: str, *, kind: str, chat_id: str) -> str:
        if self.client is None:
            raise RuntimeError("not connected")
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"{filename} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
        if kind == "photo":
            return await self.client.upload_photo(data, filename)
        if kind == "video":
            # video.save → upload → attach. The caller (send_video) falls back to a document when VK
            # refuses the video path for this community.
            return await self.client.upload_video(data, filename)
        # Documents and voice messages need the conversation peer: VK rejects peer_id=0 there
        # ("peer_id is invalid"), which silently broke every file and voice send.
        return await self.client.upload_document(data, filename, kind=kind, peer_id=int(chat_id))

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
            attachment = await self._upload_bytes(data, name, kind=upload_kind, chat_id=chat_id)
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

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                         **kwargs) -> SendResult:
        """Send a video as a native VK video attachment (``video.save`` + upload).

        The base class default only reports that native video is unavailable, so without this override
        every ``.mp4`` (cron reports, ``MEDIA:`` deliveries) reached the user as a warning instead of a
        file. When VK refuses the video path for a community, the same bytes go as a document.
        """
        result = await self._send_attachment(
            chat_id, video_path, kind="video", caption=caption, reply_to=reply_to, metadata=metadata)
        if not result.success:  # e.g. the community is not allowed to upload video
            return await self._send_attachment(
                chat_id, video_path, kind="doc", caption=caption, reply_to=reply_to, metadata=metadata)
        return result

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
    client = VkClient(token, api_version=str(extra.get("api_version") or DEFAULT_API_VERSION))
    try:
        try:
            peer_id = int(chat_id)
        except (TypeError, ValueError):
            return send_error(f"VK standalone send: invalid peer id {chat_id!r}")
        # Same keyboard as the live adapter: cron reports and `hermes send` land in the SAME chat, and a
        # message without it may leave the client without the buttons the operator enabled for it.
        keyboard = keyboard_for_peer(extra, chat_id)
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
                attachment = await client.upload_document(data, name, kind="audio_message", peer_id=peer_id)
            elif not force_document and (mimetypes.guess_type(path)[0] or "").startswith("video/"):
                # Native video is best-effort: VK answers ``video.save`` with error 5 ("User
                # authorization failed") for a community token — measured live, not assumed — so a
                # refusal must degrade to a document instead of losing the whole report.
                try:
                    attachment = await client.upload_video(data, name)
                except VkApiError as exc:
                    logger.info("VK: video upload refused (%s) — sending %s as a document", exc.code, name)
                    attachment = await client.upload_document(data, name, peer_id=peer_id)
            elif not force_document and (mimetypes.guess_type(path)[0] or "").startswith("image/"):
                attachment = await client.upload_photo(data, name)
            else:
                attachment = await client.upload_document(data, name, peer_id=peer_id)
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
            # Measured, not assumed: VK voids the WHOLE `format_data` payload when one item type is
            # unsupported, so the adapter emits only the three types proven to survive live (bold,
            # italic, url) and every other marker arrives as plain text. The hint used to promise
            # `~~strike~~`, which made the model write markup the user never saw as struck.
            "You are chatting via VK (ВКонтакте) as a community bot. VK renders only **bold**, "
            "*italic* and [links](url); there are no code blocks, and other markdown (strike, "
            "underline, headings) arrives as plain text. Keep code short and plain. Messages are "
            "capped at 4096 characters and are split automatically. In group chats "
            "your reply quotes the user's message. Buttons (choice prompts, command approvals) arrive as "
            "keyboard taps. Keep answers conversational."),
    )
