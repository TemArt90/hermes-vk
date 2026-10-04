"""Agent-facing tools for the VK plugin.

``vk_checklist`` turns a plain list into a message the recipient ticks with buttons: the agent keeps
the content and the wording, the widget layer keeps the state, and a press rewrites the same message
(see ``widgets.py``).

Why a tool rather than a directive in the reply text: the target chat is known from the session
context (``gateway.session_context``), which a tool can read and a prose convention cannot. Outside a
VK turn (cron, CLI) the peer falls back to ``VK_HOME_CHANNEL``, so a scheduled job can post the same
tickable list.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from .vk_api import DEFAULT_API_VERSION, VkApiError, VkClient
from .widgets import WidgetStore, counts, render_text

logger = logging.getLogger(__name__)

TOOLSET = "vk"
TOOL_NAME = "vk_checklist"

DESCRIPTION = (
    "Интерактивный список в чате VK: сообщение с кнопками-позициями, нажатие вычёркивает позицию "
    "прямо в сообщении (живой чек-лист). Действия: create — отправить список, update — заменить "
    "позиции (отметки сохраняются), reset — снять все отметки, close — закрыть список, "
    "status — показать текущее состояние, не трогая сообщение."
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["create", "update", "reset", "close", "status"],
            "description": "Что сделать со списком.",
        },
        "items": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Позиции списка по порядку. Нужны для create и update.",
        },
        "title": {
            "type": "string",
            "description": "Заголовок списка, например «Список покупок». По умолчанию «Список».",
        },
        "widget_id": {
            "type": "string",
            "description": "id существующего списка. Если не указан, берётся активный список этого чата.",
        },
        "peer": {
            "type": "string",
            "description": (
                "VK peer id получателя. Обычно не нужен: берётся текущий чат, а вне чата — "
                "VK_HOME_CHANNEL."
            ),
        },
    },
    "required": ["action"],
}


# ── environment helpers ─────────────────────────────────────────────────────

def _secret(name: str, default: str = "") -> str:
    """Profile-scoped credential first (the gateway's own reader), then the process env."""
    value: Any = None
    with contextlib.suppress(Exception):
        from gateway.platforms._shared import get_scoped_secret

        value = get_scoped_secret(name)
    if not value:
        value = os.environ.get(name)
    return str(value or default).strip()


def _store() -> WidgetStore:
    return WidgetStore()


def _resolve_peer(args: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Target chat: explicit ``peer`` → the current session's chat → ``VK_HOME_CHANNEL``."""
    explicit = args.get("peer")
    if explicit not in (None, ""):
        with contextlib.suppress(TypeError, ValueError):
            return int(str(explicit).strip()), "argument"
        return None, "argument"
    platform = chat_id = ""
    with contextlib.suppress(Exception):
        from gateway.session_context import get_session_env

        platform = str(get_session_env("HERMES_SESSION_PLATFORM", "") or "")
        chat_id = str(get_session_env("HERMES_SESSION_CHAT_ID", "") or "")
    if platform == "vk" and chat_id:
        with contextlib.suppress(TypeError, ValueError):
            return int(chat_id), "session"
    home = _secret("VK_HOME_CHANNEL")
    if home:
        with contextlib.suppress(TypeError, ValueError):
            return int(home.strip()), "home_channel"
    return None, "none"


def _ok(**payload: Any) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _fail(message: str, **payload: Any) -> str:
    return json.dumps({"ok": False, "error": message, **payload}, ensure_ascii=False)


def _summary(widget: Dict[str, Any]) -> Dict[str, Any]:
    done, total = counts(widget)
    return {
        "widget_id": widget.get("id"),
        "title": widget.get("title"),
        "peer": widget.get("peer_id"),
        "message_id": widget.get("message_id"),
        "done": done,
        "total": total,
        "items": [{"label": i.get("t"), "done": bool(i.get("d"))} for i in widget.get("items") or []],
    }


async def _send(peer: int, widget: Dict[str, Any], store: WidgetStore) -> Optional[int]:
    from .widgets import render

    text, keyboard = render(widget)
    client = VkClient(_secret("VK_TOKEN"), api_version=_secret("VK_API_VERSION", DEFAULT_API_VERSION) or DEFAULT_API_VERSION)
    try:
        message_id = await client.send_message(peer, text, keyboard=keyboard)
    finally:
        with contextlib.suppress(Exception):
            await client.close()
    if message_id:
        store.set_message_id(str(widget.get("id")), message_id)
    return message_id or None


async def _edit(peer: int, widget: Dict[str, Any]) -> Tuple[bool, str]:
    """Rewrite the widget's message; ``(False, reason)`` when the edit cannot happen."""
    from .widgets import render

    message_id = widget.get("message_id")
    if not message_id:
        return False, "у списка нет сообщения — отправь его заново"
    text, keyboard = render(widget)
    client = VkClient(_secret("VK_TOKEN"), api_version=_secret("VK_API_VERSION", DEFAULT_API_VERSION) or DEFAULT_API_VERSION)
    try:
        await client.edit_message(int(peer), int(message_id), text, keyboard=keyboard)
        return True, ""
    except VkApiError as exc:
        return False, f"VK отклонил правку ({exc.code}): {exc}"
    except Exception as exc:  # network / transport
        return False, f"правка не прошла: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.close()


def _lookup(store: WidgetStore, args: Dict[str, Any], peer: Optional[int]) -> Optional[Dict[str, Any]]:
    widget_id = str(args.get("widget_id") or "").strip()
    if widget_id:
        return store.get(widget_id)
    if peer is not None:
        return store.active_for(peer)
    return None


# ── the handler ─────────────────────────────────────────────────────────────

async def _handle(args: Dict[str, Any], **_kw: Any) -> str:
    if not isinstance(args, dict):
        return _fail("аргументы не объект")
    action = str(args.get("action") or "").strip().lower()
    store = _store()
    if action not in {"create", "update", "reset", "close", "status"}:
        return _fail(f"неизвестное действие {action!r}; ожидается create/update/reset/close/status")

    if action == "status":
        peer, _source = _resolve_peer(args)
        widget = _lookup(store, args, peer)
        if widget is None:
            return _fail("активного списка нет")
        return _ok(**_summary(widget), text=render_text(widget))

    if action == "create":
        peer, source = _resolve_peer(args)
        if peer is None:
            return _fail("не понятно, в какой чат отправлять: укажи peer или задай VK_HOME_CHANNEL")
        tokens = _secret("VK_TOKEN")
        if not tokens:
            return _fail("VK_TOKEN не задан — сообщение отправить нечем")
        items: List[Any] = list(args.get("items") or [])
        if not items:
            return _fail("для create нужен непустой items")
        try:
            widget = store.create(peer, str(args.get("title") or ""), items)
        except ValueError as exc:
            return _fail(str(exc))
        try:
            message_id = await _send(peer, widget, store)
        except Exception as exc:
            store.close(str(widget.get("id")))
            return _fail(f"отправка не удалась: {exc}")
        if not message_id:
            store.close(str(widget.get("id")))
            return _fail("VK не вернул id сообщения")
        widget = store.get(str(widget.get("id"))) or widget
        logger.info("VK widget %s created for peer %s (%s) with %s items",
                    widget.get("id"), peer, source, len(widget.get("items") or []))
        return _ok(**_summary(widget), text=render_text(widget))

    # update / reset / close act on an existing widget
    peer, _source = _resolve_peer(args)
    widget = _lookup(store, args, peer)
    if widget is None:
        return _fail("список не найден: неверный widget_id или в чате нет активного списка")
    widget_id = str(widget.get("id"))
    try:
        peer_id = int(widget.get("peer_id") or 0)
    except (TypeError, ValueError):
        return _fail("у списка испорчен peer_id")

    if action == "update":
        items = list(args.get("items") or [])
        if not items:
            return _fail("для update нужен непустой items")
        updated = store.set_items(widget_id, items)
    elif action == "reset":
        updated = store.reset(widget_id)
    else:  # close
        updated = store.close(widget_id)
    if updated is None:
        return _fail("список исчез во время правки")
    edited, reason = await _edit(peer_id, updated)
    if not edited:
        return _fail(reason, **_summary(updated))
    return _ok(**_summary(updated), text=render_text(store.get(widget_id) or updated))


# ── registration ────────────────────────────────────────────────────────────

def register_tools(ctx) -> None:
    """Register the widget tool on the plugin context."""
    ctx.register_tool(
        name=TOOL_NAME,
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=_handle,
        is_async=True,
        description=DESCRIPTION,
        emoji="🛒",
    )
