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
import secrets
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiohttp

API_BASE = "https://api.vk.com/method/"
DEFAULT_API_VERSION = "5.199"

# VK error codes seen in practice, mapped to the ``SendResult.error_kind`` vocabulary.
_RETRYABLE_CODES = {1, 6, 9, 10, 16}
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


class VkClient:
    """Thin async wrapper over ``https://api.vk.com/method/*`` + the Long Poll endpoint."""

    def __init__(
        self, token: str, *, api_version: str = DEFAULT_API_VERSION,
        group_id: Optional[int] = None, session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self.token = token
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
        """POST ``method`` and return ``response``; raises :class:`VkApiError` on ``error``."""
        session = await self._get_session()
        payload = {k: v for k, v in params.items() if v is not None}
        payload["access_token"] = self.token
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

    async def upload_document(self, data: bytes, filename: str, *, kind: str = "doc") -> str:
        """Upload a file (``doc``) or a voice message (``audio_message``, Ogg Opus only)."""
        server = await self.call("docs.getMessagesUploadServer", type=kind, peer_id=0) or {}
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
