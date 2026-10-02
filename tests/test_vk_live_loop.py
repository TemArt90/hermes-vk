"""Live-loop smoke test: a real VKAdapter against a mock VK API + Long Poll server.

Proves the parts unit tests cannot see: server discovery, the ``a_check`` loop, the
``failed=1`` cursor advance, reconnect after a dropped session, event dispatch into
``handle_message``, and clean shutdown.  Runs offline on 127.0.0.1.

    cd <hermes-install> && PYTHONPATH=$PWD venv/bin/python <plugin-dir>/tests/test_vk_live_loop.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import sys
import traceback
from typing import Any, Dict, List

# plugin parent, so `from vk.…` works when this file is run standalone (pytest.ini adds it too)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from aiohttp import web  # noqa: E402

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platform_registry import platform_registry  # noqa: E402

import vk.vk_api as vk_api  # noqa: E402
from vk.adapter import VKAdapter  # noqa: E402


class MockVk:
    """Minimal VK API + Long Poll surface with scripted behaviour."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.poll_ts_seen: List[int] = []
        self.poll_count = 0
        self.sent: List[Dict[str, Any]] = []
        self.fail_next_session = False
        # Scripted extra behaviour: ``batches`` replaces ``pending_updates`` (one batch per real poll,
        # ``[]`` = an empty poll) and ``http_500_on`` fails that poll number with a transport error.
        self.batches: List[List[Dict[str, Any]]] = []
        self.http_500_on: set = set()
        self.pending_updates: List[Dict[str, Any]] = [
            {"type": "message_new", "event_id": "e1",
             "object": {"message": {"id": 42, "date": 1_700_000_000, "peer_id": 555001, "from_id": 555001,
                                    "text": "Привет, Hermes", "out": 0, "attachments": []}}},
        ]
        self.runner: web.AppRunner | None = None
        self.port = 0

    # -------------------------------------------------------------- handlers

    async def _method(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        self.calls.append(method)
        if method == "groups.getById":
            return web.json_response({"response": [{"id": 777, "name": "Мок-сообщество"}]})
        if method == "groups.getLongPollServer":
            return web.json_response({"response": {"server": self.lp_url, "key": "mock-key", "ts": 1}})
        if method == "users.get":
            return web.json_response({"response": [{"id": 555001, "first_name": "Иван", "last_name": ""}]})
        if method == "messages.send":
            data = dict(await request.post())
            self.sent.append(data)
            return web.json_response({"response": 9001 + len(self.sent)})
        return web.json_response({"response": {}})

    async def _longpoll(self, request: web.Request) -> web.Response:
        self.poll_count += 1
        self.poll_ts_seen.append(int(request.query.get("ts", 0)))
        if self.poll_count in self.http_500_on:
            return web.Response(status=500, text="simulated transport failure")
        if self.fail_next_session and self.poll_count == 1:
            return web.json_response({"failed": 2})  # session expired → client must re-acquire
        if self.fail_next_session and self.poll_count == 2:
            return web.json_response({"failed": 1, "ts": 77})  # cursor moved on
        if self.batches:
            updates = self.batches.pop(0)
        else:
            updates, self.pending_updates = self.pending_updates, []
        await asyncio.sleep(0.01)
        return web.json_response({"ts": 100 + self.poll_count, "updates": updates})

    @property
    def lp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/longpoll"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/method/{method}", self._method)
        app.router.add_get("/longpoll", self._longpoll)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


def _make_adapter() -> VKAdapter:
    if not platform_registry.is_registered("vk"):
        Platform._add_pseudo_member("vk")
    return VKAdapter(PlatformConfig(extra={"token": "vk1.a.MOCKTOKEN", "group_id": 777}))


async def scenario_connect_and_receive(fail_first_session: bool = False) -> None:
    mock = MockVk()
    mock.fail_next_session = fail_first_session
    await mock.start()
    original_base = vk_api.API_BASE
    vk_api.API_BASE = f"http://127.0.0.1:{mock.port}/method/"
    adapter = _make_adapter()
    received = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture
    try:
        assert await adapter.connect() is True, "connect() failed"
        for _ in range(200):
            if received:
                break
            await asyncio.sleep(0.05)
        assert received, "no inbound message reached handle_message"
        event = received[0]
        assert event.text == "Привет, Hermes", event.text
        assert event.source.chat_id == "555001" and event.source.chat_type == "dm"
        assert event.source.user_name == "Иван"
        assert "groups.getById" in mock.calls and "groups.getLongPollServer" in mock.calls

        # outbound round trip through the same client
        result = await adapter.send("555001", "ответ **жирным**")
        assert result.success, result.error
        assert mock.sent and mock.sent[0]["message"] == "ответ жирным"
        assert json.loads(mock.sent[0]["format_data"])["items"][0]["type"] == "bold"

        if fail_first_session:
            assert mock.calls.count("groups.getLongPollServer") >= 2, "failed=2 must re-acquire the session"
            assert 77 in mock.poll_ts_seen, "failed=1 must advance the cursor"

        liveness = adapter.transport_liveness()
        assert liveness["silent"] is False and liveness["last_poll_ok_seconds_ago"] < 60

        await adapter.disconnect()
        assert not adapter.is_connected
    finally:
        with contextlib.suppress(Exception):
            await adapter.disconnect()
        vk_api.API_BASE = original_base
        await mock.stop()


def run() -> None:
    asyncio.run(scenario_connect_and_receive(fail_first_session=False))
    asyncio.run(scenario_connect_and_receive(fail_first_session=True))
    asyncio.run(scenario_cursor_survives_a_transport_error())


async def scenario_cursor_survives_a_transport_error() -> None:
    """Regression: a failed poll must NOT skip events (the button-spins-forever bug).

    Live failure it encodes: a socket read timeout made the adapter re-acquire the long-poll session,
    which returns the CURRENT cursor — so every update VK had buffered (including a callback-button
    press) was silently jumped over, leaving the user's button spinning with nothing in the log.
    """
    def message(mid: int, text: str) -> Dict[str, Any]:
        return {"type": "message_new", "event_id": f"evt-{mid}",
                "object": {"message": {"id": mid, "date": 1_700_000_000, "peer_id": 555001,
                                       "from_id": 555001, "text": text, "out": 0, "attachments": []}}}

    mock = MockVk()
    mock.pending_updates = []
    mock.batches = [[message(1, "первое")], [], [message(2, "второе")]]
    mock.http_500_on = {3}          # the third poll dies mid-flight
    await mock.start()
    original_base = vk_api.API_BASE
    vk_api.API_BASE = f"http://127.0.0.1:{mock.port}/method/"
    adapter = _make_adapter()
    received = []

    async def capture(event):
        received.append(event)

    adapter.handle_message = capture
    try:
        assert await adapter.connect() is True, "connect() failed"
        for _ in range(400):  # up to ~20s: the retry backs off once
            if len(received) >= 2:
                break
            await asyncio.sleep(0.05)
        texts = [event.text for event in received]
        assert texts == ["первое", "второе"], f"an update was lost across the failure: {texts}"
        assert mock.poll_ts_seen[3] == mock.poll_ts_seen[2], (
            f"cursor jumped after the transport error: {mock.poll_ts_seen}")
        assert mock.calls.count("groups.getLongPollServer") == 1, (
            "the session was re-acquired on a transport error (that is what skipped events)")
        await adapter.disconnect()
    finally:
        with contextlib.suppress(Exception):
            await adapter.disconnect()
        vk_api.API_BASE = original_base
        await mock.stop()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("FAIL live loop\n" + traceback.format_exc())
        sys.exit(1)
    print("ok   live loop: connect → long poll → inbound → send → disconnect (incl. failed=1/2 recovery)")
