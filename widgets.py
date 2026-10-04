"""Interactive chat widgets — a bot message whose inline keyboard carries state.

A "widget" is one message the community sent, plus the state behind its buttons. On a press the
adapter rewrites that same message (``messages.edit`` with a fresh ``keyboard``), so the recipient
ticks items in place instead of sending a message per tick.

Design notes
------------
* **State lives on disk, not in memory.** Clarify/approval buttons keep their pending id in a dict
  because they are short-lived; a shopping list has to survive a gateway restart, so every widget is
  one JSON record in ``vk_widgets.json`` (atomic replace + an flock sidecar, since the gateway turn
  and a cron/CLI process can both touch it).
* **Plain text on purpose.** ``messages.edit`` takes no ``format_data`` (measured live: losing the
  markup is documented in ``vk_api.VkClient.edit_message``), so the rendered text uses only
  characters that need no formatting — ☐ / ✅ and emoji.
* **Two hard VK limits are encoded here, both measured on a live community (04.10.2026):**
  an inline keyboard may carry **at most 10 buttons** (11 → error 911 "keyboard contains too much
  buttons"), and ``messages.edit`` **does** accept ``keyboard`` (verified by reading the message back
  through ``messages.getById``, which returns inline keyboards). Hence 8 items per page plus a
  two-button pager, or 10 items when the whole list fits one page.
* **Keys are stable, labels are the identity.** Each item carries a short key (``k1``, ``k2``…) so a
  button payload stays small (payload cap is 255 bytes) and a re-render after adding items keeps the
  ticks of items whose label is unchanged.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

ITEMS_PER_PAGE = 8          # 4 rows of two + a pager row = 10 buttons (the live VK ceiling)
ITEMS_SINGLE_PAGE = 10      # no pager needed: 5 rows of two
MAX_ITEMS = 60              # a widget taller than this is a document, not a keyboard
MAX_PAGES = 99
LABEL_MAX = 34              # the adapter truncates to 40; leave room for the ☐/✅ prefix
TEXT_LIMIT = 3500           # community messages are capped at 4096; stay clear of the edge
DEFAULT_TITLE = "Список"
PRUNE_AFTER_DAYS = 90

UNCHECKED = "☐"
CHECKED = "✅"


# ── locations ───────────────────────────────────────────────────────────────

def default_path() -> Path:
    """Where widgets live: ``VK_WIDGETS_FILE`` → Hermes home → ``~/.hermes``."""
    override = os.environ.get("VK_WIDGETS_FILE")
    if override:
        return Path(override).expanduser()
    home: Optional[Path] = None
    with contextlib.suppress(Exception):
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    return (home or Path.home() / ".hermes") / "vk_widgets.json"


# ── small helpers ───────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def new_id() -> str:
    return secrets.token_hex(3)


def button_label(item: Dict[str, Any]) -> str:
    """``✅ Хлеб`` / ``☐ Хлеб`` — the tick is visible on the button itself."""
    mark = CHECKED if item.get("d") else UNCHECKED
    text = " ".join(str(item.get("t") or "").split()) or "позиция"
    room = LABEL_MAX - len(mark)
    if len(text) > room:
        text = text[: room - 1].rstrip() + "…"
    return f"{mark} {text}"


def counts(widget: Dict[str, Any]) -> Tuple[int, int]:
    items = widget.get("items") or []
    return sum(1 for i in items if i.get("d")), len(items)


def page_size(total: int) -> int:
    return ITEMS_SINGLE_PAGE if total <= ITEMS_SINGLE_PAGE else ITEMS_PER_PAGE


def page_count(widget: Dict[str, Any]) -> int:
    total = len(widget.get("items") or [])
    if not total:
        return 1
    size = page_size(total)
    return max(1, min(MAX_PAGES, -(-total // size)))


def page_items(widget: Dict[str, Any], page: Optional[int] = None) -> List[Dict[str, Any]]:
    items = widget.get("items") or []
    size = page_size(len(items))
    total_pages = page_count(widget)
    index = total_pages - 1 if page is None else max(0, min(total_pages - 1, int(page)))
    return items[index * size:(index + 1) * size]


# ── rendering ───────────────────────────────────────────────────────────────

def render_text(widget: Dict[str, Any]) -> str:
    """The message body: the whole list, so a page change never hides an item."""
    title = " ".join(str(widget.get("title") or DEFAULT_TITLE).split()) or DEFAULT_TITLE
    items = widget.get("items") or []
    pages = page_count(widget)
    page = max(0, min(pages - 1, int(widget.get("page") or 0)))
    lines = [title, ""]
    shown = 0
    for item in items:
        line = f"{CHECKED if item.get('d') else UNCHECKED} {item.get('t') or ''}".rstrip()
        if sum(len(part) + 1 for part in lines) + len(line) > TEXT_LIMIT:
            break
        lines.append(line)
        shown += 1
    if shown < len(items):
        lines.append(f"…и ещё {len(items) - shown} поз. (на других страницах)")
    done, total = counts(widget)
    footer = f"Отмечено {done} из {total}"
    if pages > 1:
        footer += f" · стр. {page + 1}/{pages}"
    lines += ["", footer]
    return "\n".join(lines)


def render_rows(widget: Dict[str, Any], page: Optional[int] = None,
                callback: str = "w") -> List[List[Tuple[str, Optional[Dict[str, Any]], str]]]:
    """Keyboard rows in the adapter's ``(label, payload, color)`` convention."""
    items = widget.get("items") or []
    if not items:
        return []
    pages = page_count(widget)
    index = max(0, min(pages - 1, int(widget.get("page") or 0) if page is None else int(page)))
    widget_id = str(widget.get("id") or "")
    rows: List[List[Tuple[str, Optional[Dict[str, Any]], str]]] = []
    row: List[Tuple[str, Optional[Dict[str, Any]], str]] = []
    for item in page_items(widget, index):
        color = "positive" if item.get("d") else "secondary"
        row.append((button_label(item), {"v": callback, "id": widget_id, "a": "t", "k": item.get("k")}, color))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if pages > 1:
        rows.append([
            ("◀", {"v": callback, "id": widget_id, "a": "p", "d": -1}, "secondary"),
            ((f"{index + 1}/{pages} ▶"), {"v": callback, "id": widget_id, "a": "p", "d": 1}, "secondary"),
        ])
    return rows


def render(widget: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """``(text, keyboard_json)`` — keyboard ``None`` when there is nothing to press."""
    from .adapter import _keyboard  # local import: adapter imports this module

    return render_text(widget), _keyboard(render_rows(widget))


# ── the store ───────────────────────────────────────────────────────────────

class WidgetStore:
    """JSON-file store: one record per widget, read-modify-write under an exclusive lock."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_path()
        self._thread_lock = threading.RLock()

    # -- plumbing ----------------------------------------------------------

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # A torn file must not take the feature down; the next write replaces it.
            return {}
        widgets = raw.get("widgets") if isinstance(raw, dict) else None
        if not isinstance(widgets, dict):
            return {}
        return {str(k): v for k, v in widgets.items() if isinstance(v, dict)}

    def _write(self, widgets: Dict[str, Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "widgets": widgets}, ensure_ascii=False, indent=1)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[Dict[str, Dict[str, Any]]]:
        with self._thread_lock:
            lock_path = self.path.with_suffix(self.path.suffix + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                handle = open(lock_path, "a+")
            except OSError:
                handle = None
            try:
                if handle is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield self._read()
            finally:
                if handle is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    handle.close()

    def _mutate(self, widget_id: str, fn) -> Optional[Dict[str, Any]]:
        """Apply *fn* to one widget inside the lock and persist; ``None`` when unknown."""
        with self._locked() as widgets:
            widget = widgets.get(str(widget_id))
            if not isinstance(widget, dict):
                return None
            updated = fn(widget)
            if updated is None:
                return None
            updated["updated_at"] = _now()
            widgets[str(widget_id)] = updated
            self._write(widgets)
            return updated

    # -- lifecycle ---------------------------------------------------------

    def create(self, peer_id: int, title: str, items: Sequence[Any], *,
               message_id: Optional[int] = None) -> Dict[str, Any]:
        labels = [str(i if not isinstance(i, dict) else i.get("label") or i.get("t") or "").strip()
                  for i in items]
        labels = [label for label in labels if label]
        if not labels:
            raise ValueError("пустой список: нужна хотя бы одна позиция")
        if len(labels) > MAX_ITEMS:
            raise ValueError(f"слишком много позиций: {len(labels)} (максимум {MAX_ITEMS})")
        widget: Dict[str, Any] = {
            "id": new_id(), "peer_id": int(peer_id), "message_id": int(message_id) if message_id else None,
            "title": " ".join(str(title or DEFAULT_TITLE).split()) or DEFAULT_TITLE,
            "items": [], "next": 1, "page": 0,
            "created_at": _now(), "updated_at": _now(), "closed_at": None,
        }
        for label in labels:
            widget["items"].append({"k": f"k{widget['next']}", "t": label, "d": False})
            widget["next"] += 1
        with self._locked() as widgets:
            widgets[widget["id"]] = widget
            self._write(widgets)
        return widget

    def get(self, widget_id: str) -> Optional[Dict[str, Any]]:
        with self._locked() as widgets:
            widget = widgets.get(str(widget_id))
            return dict(widget) if isinstance(widget, dict) else None

    def owned(self, widget_id: str, peer_id: int) -> Optional[Dict[str, Any]]:
        """A widget may only be driven from the chat it was posted to."""
        widget = self.get(widget_id)
        if widget is None:
            return None
        try:
            if int(widget.get("peer_id") or 0) != int(peer_id):
                return None
        except (TypeError, ValueError):
            return None
        return widget

    def active_for(self, peer_id: int) -> Optional[Dict[str, Any]]:
        """The newest open widget of a chat — what "обнови список" means without an id."""
        latest: Optional[Dict[str, Any]] = None
        for widget in self.all().values():
            if widget.get("closed_at"):
                continue
            try:
                if int(widget.get("peer_id") or 0) != int(peer_id):
                    continue
            except (TypeError, ValueError):
                continue
            if latest is None or str(widget.get("created_at") or "") >= str(latest.get("created_at") or ""):
                latest = widget
        return latest

    def all(self) -> Dict[str, Dict[str, Any]]:
        return self._read()

    # -- mutations ---------------------------------------------------------

    def toggle(self, widget_id: str, key: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Flip one item; returns ``(widget, item)`` (``(None, None)`` when the key is unknown)."""
        flipped: List[Dict[str, Any]] = []

        def apply(widget: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            for item in widget.get("items") or []:
                if str(item.get("k")) == str(key):
                    item["d"] = not bool(item.get("d"))
                    flipped.append(dict(item))
                    return widget
            return None

        widget = self._mutate(widget_id, apply)
        return widget, (flipped[0] if flipped else None)

    def set_page(self, widget_id: str, page: int) -> Optional[Dict[str, Any]]:
        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            widget["page"] = max(0, min(page_count(widget) - 1, int(page)))
            return widget

        return self._mutate(widget_id, apply)

    def bump_page(self, widget_id: str, delta: int) -> Optional[Dict[str, Any]]:
        """Pager buttons wrap around — the client shows no disabled state for a boundary."""
        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            pages = page_count(widget)
            widget["page"] = (int(widget.get("page") or 0) + int(delta)) % pages
            return widget

        return self._mutate(widget_id, apply)

    def set_items(self, widget_id: str, items: Sequence[Any]) -> Optional[Dict[str, Any]]:
        """Replace the list, keeping the ticks of items whose label is unchanged."""
        labels = [str(i if not isinstance(i, dict) else i.get("label") or i.get("t") or "").strip()
                  for i in items]
        labels = [label for label in labels if label]

        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            previous = {_norm(i.get("t")): i for i in widget.get("items") or []}
            rebuilt: List[Dict[str, Any]] = []
            for label in labels:
                old = previous.get(_norm(label))
                if old is not None:
                    rebuilt.append({"k": old.get("k"), "t": label, "d": bool(old.get("d"))})
                else:
                    rebuilt.append({"k": f"k{widget['next']}", "t": label, "d": False})
                    widget["next"] = int(widget.get("next") or 1) + 1
            widget["items"] = rebuilt
            widget["page"] = max(0, min(page_count(widget) - 1, int(widget.get("page") or 0)))
            return widget

        return self._mutate(widget_id, apply)

    def reset(self, widget_id: str) -> Optional[Dict[str, Any]]:
        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            for item in widget.get("items") or []:
                item["d"] = False
            widget["page"] = 0
            return widget

        return self._mutate(widget_id, apply)

    def close(self, widget_id: str) -> Optional[Dict[str, Any]]:
        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            widget["closed_at"] = _now()
            return widget

        return self._mutate(widget_id, apply)

    def set_message_id(self, widget_id: str, message_id: int) -> Optional[Dict[str, Any]]:
        def apply(widget: Dict[str, Any]) -> Dict[str, Any]:
            widget["message_id"] = int(message_id)
            return widget

        return self._mutate(widget_id, apply)

    def prune(self, max_age_days: int = PRUNE_AFTER_DAYS) -> int:
        """Drop widgets nobody touched for *max_age_days*; returns how many went."""
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
        removed = 0
        with self._locked() as widgets:
            for key in list(widgets):
                stamp = widgets[key].get("updated_at") or widgets[key].get("created_at") or ""
                try:
                    when = datetime.datetime.fromisoformat(str(stamp))
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=datetime.timezone.utc)
                if when < cutoff:
                    widgets.pop(key, None)
                    removed += 1
            if removed:
                self._write(widgets)
        return removed
