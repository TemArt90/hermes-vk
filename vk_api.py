"""Minimal VK Bot API client — Bots Long Poll (v3) + the message methods Hermes needs.

No VK SDK: ``vk-io`` is JavaScript, the Python ``vk_api`` package would be a new
dependency, and the surface actually used here is five HTTP calls.  ``aiohttp`` is
already in the Hermes runtime, so this module adds nothing to install.

Token: a **community** (сообщество) access token with scopes ``messages`` +
``manage`` (manage is required for Bots Long Poll).  The community is resolved
with ``groups.getById``, which returns the community owning a community token —
so the operator never has to type a group id.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

API_BASE = "https://api.vk.com/method/"
DEFAULT_API_VERSION = "5.199"

# VK error codes seen in practice, mapped to the ``SendResult.error_kind`` vocabulary.
_RETRYABLE_CODES = {1, 6, 9, 10, 16, 995}
_RATE_LIMIT_CODES = {6, 9, 14}
_FORBIDDEN_CODES = {5, 7, 15, 900, 901, 902, 936, 945}
_NOT_FOUND_CODES = {100, 113, 936}

# Actionable hints for the codes that actually cost debugging time. A bare "error 901" in the
# gateway log says nothing about who has to do what; these do.
_ERROR_HINTS = {
    901: "пользователь ни разу не открывал диалог с сообществом — напишите боту первым, "
         "после этого сообщество сможет отвечать",
    902: "пользователь запретил сообщения от сообществ (Настройки → Приватность)",
    15: "нет доступа: у ключа нет права «Управление сообществом» либо не включён Long Poll API",
    27: "ключ сообщества недействителен или отозван — создайте новый ключ доступа",
    100: "неверные параметры вызова (частая причина — некорректная разметка сообщения)",
    912: "у сообщества не включён «Чат-бот»: Управление → Сообщения → Настройки для бота — "
         "без него кнопки (вопросы агента, подтверждение команд) недоступны",
    6: "превышен лимит запросов в секунду (20/с на сообщество)",
    995: "временная деградация сервиса VK — повторите позже",
    1009: "такой реакции нет — проверьте номер реакции для этого сообщества",
    1010: "эта реакция отключена в сообществе — выберите другую",
    1011: "на сообщении уже достигнут предел реакций",
}


class VkApiError(Exception):
    """A VK method returned ``{"error": {...}}`` or the transport failed."""

    def __init__(self, method: str, code: int, message: str, *, transport: bool = False):
        self.hint = _ERROR_HINTS.get(code, "")
        text = f"VK {method} failed ({code}): {message}"
        super().__init__(f"{text} — {self.hint}" if self.hint else text)
        self.method = method
        self.code = code
        self.message = message
        self.transport = transport

    @property
    def retryable(self) -> bool:
        return self.transport or self.code in _RETRYABLE_CODES

    @property
    def error_kind(self) -> str:
        if self.transport or self.code in _RETRYABLE_CODES:
            return "transient"
        if self.code in _RATE_LIMIT_CODES:
            return "rate_limited"
        if self.code in _FORBIDDEN_CODES:
            return "forbidden"
        if self.code in _NOT_FOUND_CODES:
            return "not_found"
        return "unknown"


def random_id() -> int:
    """VK de-duplication id: two sends with the same ``random_id`` collapse into one message."""
    return secrets.randbelow(2 ** 31 - 1) + 1


def redact_secrets(text: str, *secrets: str) -> str:
    """Strip credentials out of text before it reaches a log, an error surfaced to the user, or the
    agent's transcript.

    The optional user token is a person's OWN credential (unlike the community key, which belongs to the
    bot): an error string that happens to quote a request URL, or a VK message that echoes the token,
    must not be what lands in ``errors.log``.
    """
    out = str(text or "")
    for secret in secrets:
        secret = str(secret or "")
        if len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    return re.sub(r"(access_token=)[^&\s]+", r"\1[REDACTED]", out)


def _best_video_file(items: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The largest downloadable ``mp4`` VK lists for a video, or ``None``.

    A video attachment can expose only a watch page (no ``files`` block). That is not an error — it just
    means there is nothing to hand to the agent, so the caller keeps its textual note.
    """
    for item in items:
        files = item.get("files") or {}
        for key in ("mp4_1080", "mp4_720", "mp4_480", "mp4_360", "mp4_240"):
            url = files.get(key)
            if url:
                return {"url": str(url), "title": str(item.get("title") or "video"),
                        "duration": int(item.get("duration") or 0), "ext": ".mp4"}
    return None


class VkClient:
    """Thin async wrapper over ``https://api.vk.com/method/*`` + the Long Poll endpoint."""

    def __init__(
        self, token: str, *, api_version: str = DEFAULT_API_VERSION,
        group_id: Optional[int] = None, session: Optional[aiohttp.ClientSession] = None,
        user_token: str = "",
    ) -> None:
        self.token = token
        # Optional USER token — a person's own VK account, needed only for the calls a community token
        # cannot make (in practice ``video.get`` for inbound video). Empty means "do not even ask".
        self.user_token = str(user_token or "").strip()
        self.api_version = api_version or DEFAULT_API_VERSION
        self.group_id = int(group_id or 0)
        self.group_name = ""
        self._session = session
        self._owns_session = session is None
        self._api_sem = asyncio.Semaphore(4)  # VK allows ~20 req/s; stay well inside it

    # ------------------------------------------------------------------ plumbing

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=40, connect=15),
                headers={"User-Agent": "hermes-agent/vk"},
            )
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def call(self, method: str, *, timeout: float = 30.0, **params: Any) -> Any:
        """POST ``method`` as the community and return ``response``; raises :class:`VkApiError`."""
        return await self.call_as(self.token, method, timeout=timeout, **params)

    async def call_as(self, token: str, method: str, *, timeout: float = 30.0, **params: Any) -> Any:
        """The same call with an explicit token — how the optional user token reaches ``video.get``.

        One implementation for both: a second copy of the request/error handling would drift, and the
        error classification (``retryable``, ``error_kind``) is exactly what callers depend on.
        """
        session = await self._get_session()
        payload = {k: v for k, v in params.items() if v is not None}
        payload["access_token"] = token
        payload["v"] = self.api_version
        async with self._api_sem:
            try:
                async with session.post(
                    API_BASE + method, data=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout, connect=15),
                ) as resp:
                    body = await resp.json(content_type=None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network / decode
                raise VkApiError(method, 0, f"transport error: {exc}", transport=True) from exc
        if not isinstance(body, dict):
            raise VkApiError(method, 0, "malformed response", transport=True)
        if "error" in body:
            err = body["error"] or {}
            raise VkApiError(method, int(err.get("error_code", 0) or 0), str(err.get("error_msg", "unknown")))
        return body.get("response")

    # ------------------------------------------------------------------ identity / transport

    async def resolve_group(self) -> Tuple[int, str]:
        """Resolve the community the token belongs to (``groups.getById``)."""
        response = await self.call("groups.getById", timeout=20)
        groups: Sequence[Dict[str, Any]]
        if isinstance(response, dict):  # older API shape
            groups = response.get("groups") or ([response] if response.get("id") else [])
        else:
            groups = response or []
        if not groups:
            raise VkApiError("groups.getById", 0, "token is not a community token")
        self.group_id = int(groups[0].get("id") or 0)
        self.group_name = str(groups[0].get("name") or "")
        if not self.group_id:
            raise VkApiError("groups.getById", 0, "community id missing in response")
        return self.group_id, self.group_name

    async def get_long_poll_server(self) -> Dict[str, Any]:
        if not self.group_id:
            await self.resolve_group()
        response = await self.call(
            "groups.getLongPollServer", group_id=self.group_id, timeout=20)
        if not isinstance(response, dict) or "server" not in response:
            raise VkApiError("groups.getLongPollServer", 0, "no long-poll server in response")
        return response

    async def get_conversations(self, *, count: int = 20) -> List[Dict[str, Any]]:
        """Newest conversations with their last message (``messages.getConversations``).

        Used only by the fallback sweep: while Long Poll is silent this is how the adapter learns that
        something arrived. A community token sees the conversations the community takes part in; the
        entries without a ``last_message`` (possible in VK's payload) are dropped here so the caller can
        trust every item it iterates.
        """
        response = await self.call("messages.getConversations", count=count, filter="all", timeout=20)
        items = (response or {}).get("items") or []
        return [item for item in items
                if isinstance(item, dict) and isinstance(item.get("last_message"), dict)]

    async def poll(self, server: str, key: str, ts: Any, *, wait: int = 25) -> Dict[str, Any]:
        """One ``a_check`` request. Returns ``{"ts":…, "updates":[…]}`` or ``{"failed": n}``."""
        session = await self._get_session()
        params = {"act": "a_check", "key": key, "ts": str(ts), "wait": str(wait), "mode": "2", "version": "3"}
        try:
            async with session.get(
                server, params=params,
                timeout=aiohttp.ClientTimeout(total=wait + 25, connect=15, sock_read=wait + 20),
            ) as resp:
                if resp.status != 200:
                    raise VkApiError("a_check", resp.status, f"HTTP {resp.status}", transport=True)
                body = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except VkApiError:
            raise
        except Exception as exc:
            raise VkApiError("a_check", 0, f"transport error: {exc}", transport=True) from exc
        if not isinstance(body, dict):
            raise VkApiError("a_check", 0, "malformed long-poll response", transport=True)
        return body

    # ------------------------------------------------------------------ media

    async def download(self, url: str, *, max_bytes: int = 20 * 1024 * 1024) -> bytes:
        session = await self._get_session()
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=60, connect=15)) as resp:
            if resp.status != 200:
                raise VkApiError("download", resp.status, f"HTTP {resp.status}", transport=True)
            chunks, size = [], 0
            async for chunk in resp.content.iter_chunked(65536):
                size += len(chunk)
                if size > max_bytes:
                    raise VkApiError("download", 0, f"file exceeds {max_bytes} bytes")
                chunks.append(chunk)
        return b"".join(chunks)

    async def _upload(self, upload_url: str, data: bytes, filename: str) -> Dict[str, Any]:
        session = await self._get_session()
        form = aiohttp.FormData()
        form.add_field("file", data, filename=filename, content_type="application/octet-stream")
        async with session.post(upload_url, data=form, timeout=aiohttp.ClientTimeout(total=120)) as resp:
            if resp.status != 200:
                raise VkApiError("upload", resp.status, f"HTTP {resp.status}", transport=True)
            return await resp.json(content_type=None)

    async def upload_photo(self, data: bytes, filename: str = "image.jpg") -> str:
        """Upload an image and return the VK attachment string (``photo<owner>_<id>``)."""
        server = await self.call("photos.getMessagesUploadServer", peer_id=0) or {}
        upload = await self._upload(server["upload_url"], data, filename)
        if not upload.get("photo"):
            # Without this, a rejected upload degrades into a bare "photo is undefined" from
            # saveMessagesPhoto, which says nothing about what the upload endpoint actually returned.
            raise VkApiError("photos.getMessagesUploadServer", 0,
                             f"upload returned no photo field: {str(upload)[:200]}")
        saved = await self.call(
            "photos.saveMessagesPhoto", photo=upload.get("photo"), server=upload.get("server"),
            hash=upload.get("hash"))
        if not saved:
            raise VkApiError("photos.saveMessagesPhoto", 0, "empty response")
        photo = saved[0]
        return f"photo{photo['owner_id']}_{photo['id']}"

    async def upload_document(self, data: bytes, filename: str, *, kind: str = "doc",
                              peer_id: int = 0) -> str:
        """Upload a file (``doc``) or a voice message (``audio_message``, Ogg Opus only).

        ``peer_id`` must be the real conversation peer here: unlike ``photos.getMessagesUploadServer``
        (which accepts 0), VK answers ``docs.getMessagesUploadServer`` with error 100
        "peer_id is invalid" for 0, so every document and voice message fails without it.
        """
        server = await self.call("docs.getMessagesUploadServer", type=kind, peer_id=peer_id) or {}
        upload = await self._upload(server["upload_url"], data, filename)
        if not upload.get("file"):
            raise VkApiError("docs.getMessagesUploadServer", 0,
                             f"upload returned no file field: {str(upload)[:200]}")
        saved = await self.call("docs.save", file=upload.get("file"), title=filename)
        doc = None
        if isinstance(saved, dict):
            doc = saved.get(kind) or saved.get("doc") or (saved.get("audio_message"))
        elif isinstance(saved, list) and saved:
            doc = saved[0]
        if not doc:
            raise VkApiError("docs.save", 0, "empty response")
        return f"doc{doc['owner_id']}_{doc['id']}"

    async def upload_video(self, data: bytes, filename: str, *, name: str = "") -> str:
        """Upload a video and return the ``video<owner>_<id>`` attachment string.

        VK's flow is ``video.save`` — which reserves the video and hands back a per-file upload URL —
        then the upload, after which the reserved id is a usable attachment. A community token can be
        refused here (error 15) when video is restricted for the community; the adapter then sends the
        same bytes as a document, so the file still reaches the user.
        """
        # video.save is a USER-scope method: a community key is refused outright (measured live, error 5),
        # so the optional user token is what makes native video possible at all. Without it the call fails
        # the usual way and the adapter falls back to sending the file as a document.
        token = self.user_token or self.token
        saved = await self.call_as(token, "video.save", name=name or filename, is_private=1,
                                   wallpost=0, timeout=30) or {}
        upload_url, video_id = saved.get("upload_url"), saved.get("video_id")
        if not upload_url or not video_id:
            raise VkApiError("video.save", 0, f"no upload_url/video_id in response: {str(saved)[:200]}")
        await self._upload(upload_url, data, filename)
        # The reserved video is PRIVATE (``is_private=1``), so the attachment must carry its access key:
        # measured live (2026-10-04) — ``video.save`` returns ``access_key``, and the bare
        # ``video<owner>_<id>`` form leaves the recipient with a player that refuses to play. VK's own
        # attachment format for a private video is ``video<owner>_<id>_<access_key>``.
        attachment = f"video{saved.get('owner_id')}_{video_id}"
        if saved.get("access_key"):
            attachment += f"_{saved['access_key']}"
        return attachment

    async def get_video_file(self, video: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """A downloadable file for an INBOUND video, using the optional user token.

        MEASURED 2026-10-04: this no longer has anything to return. ``video.get`` answers with
        ``files: None`` for every video tested — the owner's own and a community's, re-checked 5 s and 35 s
        after an upload — and the only link it does expose (``direct_url``) is a ``vkvideo.ru`` HTML player
        page, not a media file. The code is kept because the field may come back and the "no user token"
        branch is still correct, but treat the return as permanently empty and keep the caller's note.

        ``video.get`` is one of the calls a community token cannot make — measured live on our own
        community key (2026-10-03): ``video.get`` and ``video.save`` both answer error 5
        "User authorization failed". ``None`` means "cannot ask" (no user token configured) and the
        caller keeps its note; a refusal by VK raises :class:`VkApiError` so the caller can log why —
        after redacting the credential.
        """
        if not self.user_token:
            return None
        reference = f"{int(video.get('owner_id') or 0)}_{int(video.get('id') or 0)}"
        if video.get("access_key"):
            reference += f"_{video['access_key']}"
        response = await self.call_as(self.user_token, "video.get", videos=reference, timeout=30)
        items = response.get("items") if isinstance(response, dict) else response
        if isinstance(items, dict):  # a single-video response shape
            items = [items]
        return _best_video_file(list(items or []))

    # ------------------------------------------------------------------ messages

    async def send_message(
        self, peer_id: int, message: str, *, random_id_: Optional[int] = None,
        reply_to: Optional[int] = None, format_data: Optional[Dict] = None,
        keyboard: Optional[str] = None, attachment: Optional[str] = None,
        dont_parse_links: bool = True,
    ) -> int:
        response = await self.call(
            "messages.send", peer_id=peer_id, message=message, random_id=random_id_ or random_id(),
            reply_to=reply_to, format_data=json.dumps(format_data, ensure_ascii=False) if format_data else None,
            keyboard=keyboard, attachment=attachment, dont_parse_links=1 if dont_parse_links else None,
            timeout=30,
        )
        return int(response or 0)

    async def edit_message(self, peer_id: int, message_id: int, message: str) -> None:
        """Rewrite a message the bot already sent (``messages.edit``).

        ``messages.edit`` takes no ``format_data`` parameter, so an edited message is plain text —
        the renderer's markup only ever survives on the original ``messages.send``. It also cannot
        split: content that does not fit one message has to be sent anew by the caller.
        """
        await self.call(
            "messages.edit", peer_id=peer_id, message_id=message_id, message=message, timeout=30)

    async def send_reaction(self, peer_id: int, cmid: int, reaction_id: int) -> bool:
        """React to a message (``messages.sendReaction``).

        Addressed by ``cmid`` — VK's conversation-local message number, not the ``message_id`` that
        ``messages.send`` returns — and ``reaction_id`` is a per-community number, not an emoji.
        A community token is accepted for this method (documented requirement: ``messages``).
        """
        response = await self.call(
            "messages.sendReaction", peer_id=peer_id, cmid=cmid, reaction_id=reaction_id, timeout=20)
        return bool(response)

    async def delete_reaction(self, peer_id: int, cmid: int) -> bool:
        """Remove the bot's reaction (``messages.deleteReaction``); takes the same ``cmid``."""
        response = await self.call(
            "messages.deleteReaction", peer_id=peer_id, cmid=cmid, timeout=20)
        return bool(response)

    async def set_activity(self, peer_id: int, *, activity: str = "typing") -> None:
        await self.call(
            "messages.setActivity", peer_id=peer_id, type=activity, group_id=self.group_id, timeout=15)

    async def answer_event(self, event_id: str, user_id: int, peer_id: int, text: str) -> None:
        """Acknowledge a callback-button press (stops the spinner on the user's client)."""
        payload: Dict[str, Any] = {"type": "show_snackbar", "text": (text or "")[:90]}
        await self.call(
            "messages.sendMessageEventAnswer", event_id=event_id, user_id=user_id,
            peer_id=peer_id, event_data=json.dumps(payload, ensure_ascii=False), timeout=20)

    async def user_names(self, user_ids: Sequence[int]) -> Dict[int, str]:
        ids = [int(u) for u in user_ids if int(u) > 0]
        if not ids:
            return {}
        response = await self.call(
            "users.get", user_ids=",".join(str(i) for i in ids[:1000]), timeout=20)
        names: Dict[int, str] = {}
        for user in response or []:
            full = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            if full:
                names[int(user["id"])] = full
        return names

    async def chat_title(self, peer_id: int) -> str:
        response = await self.call(
            "messages.getConversationsById", peer_ids=str(peer_id), timeout=20)
        items: List[Dict[str, Any]] = (response or {}).get("items") or []
        for item in items:
            chat = (item.get("chat_settings") or {})
            if chat.get("title"):
                return str(chat["title"])
        return ""
