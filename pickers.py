"""Interactive pickers for VK: the ``/model`` provider→model chooser and flat choice pickers.

The core already drives these flows — ``gateway/slash_commands_model.py`` calls
``adapter.send_model_picker(...)`` for ``/model`` with no arguments and ``adapter.send_choice_picker(...)``
for ``/reasoning`` and ``/fast``, falling back to a plain text list when the adapter type has no such
method. This module is the VK half of that contract: rendering, paging inside VK's button ceiling, and
callback bookkeeping.

Design notes
------------
* **State lives in memory, on purpose.** A picker is a question asked *now*: unlike a shopping-list
  widget it must NOT survive a restart (a stale picker whose closure died with the process would
  press into nothing). Records expire after ``PICKER_TTL`` and a press on an expired one answers
  «Выбор устарел», the same behaviour clarify prompts already have.
* **The core owns the switch; we own the UI.** The adapter renders providers/models and hands the tap
  to ``on_model_selected`` (which switches the model, applies the cost guard, and returns the text to
  show). Nothing about model semantics is re-implemented here.
* **Layout fits the measured VK ceiling of 10 inline buttons** (11 → error 911): 7 items per page,
  two per row, plus one control row of ≤3 buttons.
* **The selection guard is the picker's job** — the typed path confirms through
  ``send_slash_confirm`` and the core skips its guard for pickers (that is why Discord's picker has
  its own confirm step). We ask the same ``combined_selection_warning`` helper and, when it warns,
  show a two-button confirmation before calling the callback.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .widgets import LABEL_MAX, new_id

PICKER_TTL_SECONDS = 900          # 15 minutes: long enough to browse, short enough to stay honest
MAX_PICKERS = 200                 # a chat cannot accumulate unbounded relayable state
ITEMS_PER_PAGE = 7                # 4 rows of two (2+2+2+1) + a control row of three = 10 buttons
COLUMNS = 2
MARK_CURRENT = "• "
CONFIRM_LABEL = "Всё равно"

MODEL_ACTION = "mk"
CHOICE_ACTION = "cp"


# ── rendering helpers ───────────────────────────────────────────────────────

def _short(text: Any) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, limit: int = LABEL_MAX) -> str:
    text = _short(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def pages_for(count: int, size: int = ITEMS_PER_PAGE) -> int:
    return max(1, -(-int(count) // size))


def page_slice(seq: List[Any], page: int, size: int = ITEMS_PER_PAGE) -> Tuple[List[Any], int, int]:
    total_pages = pages_for(len(seq), size)
    index = max(0, min(total_pages - 1, int(page)))
    return seq[index * size:(index + 1) * size], index, total_pages


def _rows(items: List[Tuple[str, Optional[Dict[str, Any]], str]]) -> List[List[Any]]:
    """Pack ``(label, payload, color)`` into rows of two; a lone trailing item keeps its own row."""
    rows: List[List[Any]] = []
    row: List[Any] = []
    for entry in items:
        row.append(entry)
        if len(row) == COLUMNS:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def control_row(state: Dict[str, Any], *, page: int, total_pages: int, back: bool) -> List[Any]:
    """The picker's bottom row: paging when needed, plus back/close."""
    picker_id = state["id"]
    row: List[Any] = []
    if total_pages > 1:
        row.append(("◀", {"v": state["action"], "id": picker_id, "a": "pg", "p": page - 1}, "secondary"))
        row.append(("▶", {"v": state["action"], "id": picker_id, "a": "pg", "p": page + 1}, "secondary"))
    if back:
        row.append(("↩ провайдеры", {"v": state["action"], "id": picker_id, "a": "bk"}, "secondary"))
    else:
        row.append(("✖", {"v": state["action"], "id": picker_id, "a": "x"}, "secondary"))
    return row


# ── model picker views ──────────────────────────────────────────────────────

def _provider_by_slug(state: Dict[str, Any], slug: str) -> Optional[Dict[str, Any]]:
    return next((p for p in state.get("providers") or [] if str(p.get("slug")) == str(slug)), None)


def _provider_label(provider: Dict[str, Any]) -> str:
    return _short(provider.get("name") or provider.get("slug"))


def render_model_text(state: Dict[str, Any]) -> str:
    current_model = _short(state.get("current_model")) or "не выбран"
    current_provider = _short(state.get("current_provider"))
    view = state.get("view") or "providers"
    if view == "models":
        provider = _provider_by_slug(state, state.get("provider") or "")
        name = _provider_label(provider) if provider else _short(state.get("provider"))
        models = list((provider or {}).get("models") or [])
        if not models:
            return f"⚙ {name}\n\nУ этого провайдера список моделей пуст — вернись назад."
        _, index, total_pages = page_slice(models, state.get("page") or 0)
        total = (provider or {}).get("total_models", len(models))
        extra = f"\n*всего у провайдера: {total}*" if isinstance(total, int) and total > len(models) else ""
        return f"⚙ {name} — модели\n\nВыбери модель · стр. {index + 1}/{total_pages}{extra}"
    if view == "confirm":
        return f"⚠ {_short(state.get('warning_title'))}\n\n{_short(state.get('warning_message'))}"
    providers = state.get("providers") or []
    _, index, total_pages = page_slice(providers, state.get("page") or 0)
    now = f"Сейчас: {current_model}"
    if current_provider:
        now += f" · {current_provider}"
    return f"⚙ Модель\n\n{now}\nВыбери провайдера · стр. {index + 1}/{total_pages}"


def render_model_rows(state: Dict[str, Any]) -> List[List[Any]]:
    from .adapter import _keyboard  # noqa: F401  (deferred import: adapter imports this module)

    view = state.get("view") or "providers"
    if view == "confirm":
        return [[
            (CONFIRM_LABEL, {"v": state["action"], "id": state["id"], "a": "ok"}, "negative"),
            ("Отмена", {"v": state["action"], "id": state["id"], "a": "no"}, "secondary"),
        ]]
    if view == "models":
        provider = _provider_by_slug(state, state.get("provider") or "")
        models = list((provider or {}).get("models") or [])
        if not models:
            return [control_row(state, page=0, total_pages=1, back=True)]
        page_models, index, total_pages = page_slice(models, state.get("page") or 0)
        current = _short(state.get("current_model"))
        entries = []
        for model_id in page_models:
            label = _clip(model_id.split("/")[-1] or model_id)
            if current and str(model_id) == current:
                label = MARK_CURRENT + label
            entries.append((label, {"v": state["action"], "id": state["id"], "a": "md", "m": str(model_id)},
                            "primary" if str(model_id) == current else "secondary"))
        return _rows(entries) + [control_row(state, page=index, total_pages=total_pages, back=True)]
    providers = state.get("providers") or []
    page_providers, index, total_pages = page_slice(providers, state.get("page") or 0)
    current_provider = _short(state.get("current_provider"))
    entries = []
    for provider in page_providers:
        slug = str(provider.get("slug"))
        count = provider.get("total_models", len(provider.get("models") or []))
        label = _clip(f"{_provider_label(provider)} ({count})")
        if current_provider and slug == current_provider:
            label = MARK_CURRENT + label
        entries.append((label, {"v": state["action"], "id": state["id"], "a": "pr", "s": slug},
                        "primary" if slug == current_provider else "secondary"))
    return _rows(entries) + [control_row(state, page=index, total_pages=total_pages, back=False)]


# ── flat choice picker ( /reasoning, /fast ) ────────────────────────────────

def render_choice_text(state: Dict[str, Any]) -> str:
    title = (state.get("title") or "").strip() or "Выбери значение"
    lines = title.splitlines()
    head, rest = lines[0], lines[1:]
    choices = state.get("choices") or []
    _, index, total_pages = page_slice(choices, state.get("page") or 0)
    body = [head, ""]
    body.extend(line for line in rest if line.strip())
    if total_pages > 1:
        body.append(f"стр. {index + 1}/{total_pages}")
    return "\n".join(body).strip()


def render_choice_rows(state: Dict[str, Any]) -> List[List[Any]]:
    choices = state.get("choices") or []
    page_choices, index, total_pages = page_slice(choices, state.get("page") or 0)
    entries = []
    for choice in page_choices:
        value = str(choice.get("value"))
        label = _clip(f"{MARK_CURRENT if choice.get('is_current') else ''}{choice.get('label') or value}")
        entries.append((label, {"v": state["action"], "id": state["id"], "a": "ch", "c": value},
                        "primary" if choice.get("is_current") else "secondary"))
    rows = _rows(entries)
    row: List[Any] = []
    if total_pages > 1:
        row.append(("◀", {"v": state["action"], "id": state["id"], "a": "pg", "p": index - 1}, "secondary"))
        row.append(("▶", {"v": state["action"], "id": state["id"], "a": "pg", "p": index + 1}, "secondary"))
    row.append(("✖", {"v": state["action"], "id": state["id"], "a": "x"}, "secondary"))
    rows.append(row)
    return rows


def render(state: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    from .adapter import _keyboard

    if state.get("kind") == "choice":
        return render_choice_text(state), _keyboard(render_choice_rows(state))
    return render_model_text(state), _keyboard(render_model_rows(state))


def result_text(state: Dict[str, Any], body: str) -> Tuple[str, Optional[str]]:
    """The message after a selection: the outcome, and no buttons left."""
    head = "✓" if state.get("succeeded") else "✗"
    text = f"{head} {_short(body)[:3500]}"
    keyboard = json.dumps({"inline": True, "buttons": []}, ensure_ascii=False)
    return text, keyboard


# ── the store ───────────────────────────────────────────────────────────────

class PickerStore:
    """Short-lived picker records, keyed by a short id, with TTL and a hard cap."""

    def __init__(self, ttl_seconds: int = PICKER_TTL_SECONDS, max_records: int = MAX_PICKERS) -> None:
        self.ttl = int(ttl_seconds)
        self.max_records = int(max_records)
        self._records: Dict[str, Dict[str, Any]] = {}

    # -- lifecycle ---------------------------------------------------------

    def _sweep(self) -> None:
        cutoff = time.time() - self.ttl
        for key in [k for k, v in self._records.items() if float(v.get("created") or 0) < cutoff]:
            self._records.pop(key, None)
        if len(self._records) > self.max_records:
            oldest = sorted(self._records, key=lambda k: float(self._records[k].get("created") or 0))
            for key in oldest[: len(self._records) - self.max_records]:
                self._records.pop(key, None)

    def put(self, **fields: Any) -> Dict[str, Any]:
        self._sweep()
        record = {"id": new_id(), "created": time.time(), "page": 0, "view": "providers",
                  "resolved": False, "message_id": None}
        record.update(fields)
        self._records[record["id"]] = record
        return record

    def get(self, picker_id: str) -> Optional[Dict[str, Any]]:
        self._sweep()
        return self._records.get(str(picker_id))

    def drop(self, picker_id: str) -> None:
        self._records.pop(str(picker_id), None)

    def count(self) -> int:
        self._sweep()
        return len(self._records)

    # -- mutations ---------------------------------------------------------

    def update(self, picker_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        record = self.get(picker_id)
        if record is None:
            return None
        record.update(fields)
        return record


def new_model_record(chat_id: str, session_key: str, providers: List[dict], current_model: str,
                     current_provider: str, on_model_selected) -> Dict[str, Any]:
    return dict(action=MODEL_ACTION, kind="model", chat_id=str(chat_id), session_key=session_key,
                providers=providers, current_model=current_model, current_provider=current_provider,
                on_model_selected=on_model_selected, provider="", warning_title="",
                warning_message="", pending_model="")


def new_choice_record(chat_id: str, session_key: str, title: str, choices: List[dict],
                      on_choice_selected) -> Dict[str, Any]:
    return dict(action=CHOICE_ACTION, kind="choice", chat_id=str(chat_id), session_key=session_key,
                title=title, choices=choices, on_choice_selected=on_choice_selected)


async def selection_warning(model_id: str, provider_slug: str) -> Optional[Any]:
    """The core's unified guard (cost + data policy) for a model, or ``None``.

    Best-effort: pricing lookups can hit the network on a cache miss, so it runs off-loop and any
    failure degrades to "no warning" rather than blocking the switch.
    """
    import asyncio

    try:
        from hermes_cli.model_selection_guards import combined_selection_warning

        return await asyncio.to_thread(combined_selection_warning, model_id, provider=provider_slug)
    except Exception:
        return None
