"""One connected game: its pulse view, and the actions we send it.

The SDK pushes `PULSE` readings (0.1s samples, 1s batches) and answers our `ACTION`
messages with `ACTION_RESULT`. Claude never sees the stream; it calls a tool, and the tool
returns the view at that moment. `act_and_look` follows artel-agent-server's rule
(ARTEL-621): after an action, wait for a reading whose frame is later than the frame the
action finished on, so the view shown is the one the action produced and not the batch
captured just before it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from artel_mcp.pulse import PulseMemory, PulseReading

log = logging.getLogger("artel_mcp.game")

READING_WAIT_SECONDS = 1.5
ACTION_RESULT_TIMEOUT = 15.0

SendFn = Callable[[str], Awaitable[None]]


@dataclass
class ActionResult:
    request_id: int
    frame: int | None
    results: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return all(r.get("success", False) for r in self.results) if self.results else False

    def describe(self) -> str:
        parts = []
        for r in self.results:
            name = r.get("action") or ""
            if r.get("success"):
                rv = r.get("returnValue")
                parts.append(f"{name}: ok" + (f" → {json.dumps(rv, ensure_ascii=False)[:300]}" if rv not in (None, "", {}) else ""))
            else:
                err = r.get("error") or {}
                parts.append(f"{name}: FAILED {err.get('type', '')} {err.get('message', '')}".strip())
        return "; ".join(parts) if parts else "(no results)"


@dataclass
class GameSession:
    instance_id: int
    build_id: int | None
    send: SendFn
    pulse: PulseMemory = field(default_factory=PulseMemory)
    connected_at: float = field(default_factory=time.time)
    device: dict[str, Any] | None = None
    last_action_frame: int | None = None
    readings_started: bool = False
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _pending: dict[int, asyncio.Future] = field(default_factory=dict)
    _reading_arrived: asyncio.Event = field(default_factory=asyncio.Event)
    _last_pulse_at: float | None = None
    _pulse_errors: int = 0

    # ── inbound ───────────────────────────────────────────────────────────

    def on_message(self, text: str) -> None:
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            log.warning("non-JSON frame from SDK: %r", text[:120])
            return
        kind = msg.get("type")
        if kind == "PULSE":
            try:
                self.pulse.apply(PulseReading.model_validate(msg))
                self._last_pulse_at = time.time()
                self._reading_arrived.set()
            except Exception:  # noqa: BLE001 — a bad reading must not kill the socket
                self._pulse_errors += 1
                log.exception("pulse could not be applied")
        elif kind == "ACTION_RESULT":
            rid = msg.get("requestId")
            fut = self._pending.pop(int(rid), None) if rid is not None else None
            if fut is None:
                # Old SDKs echo nothing; fall back to the oldest pending request.
                if self._pending:
                    _, fut = sorted(self._pending.items())[0]
                    self._pending = {k: v for k, v in self._pending.items() if v is not fut}
            if fut is not None and not fut.done():
                fut.set_result(ActionResult(int(rid or 0), msg.get("frame"), msg.get("results") or []))
        elif kind == "DEVICE_CONTEXT":
            self.device = msg.get("device")
        elif kind in ("PERFORMANCE", "RUN_STATUS", "GAME_STATE", "ALL_SCENES", "SCAN_SCENE"):
            pass
        elif kind == "ERROR":
            log.warning("SDK error: %s", msg.get("message"))
        else:
            log.debug("unhandled SDK message type %s", kind)

    # ── outbound ──────────────────────────────────────────────────────────

    async def dispatch(self, actions: list[dict[str, Any]], timeout: float = ACTION_RESULT_TIMEOUT) -> ActionResult:
        rid = next(self._ids)
        envelope = {
            "type": "ACTION",
            "id": rid,
            "actions": [
                {"id": i + 1, "jsonrpc": "2.0", "method": a["method"], "params": a.get("params", [])}
                for i, a in enumerate(actions)
            ],
        }
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        await self.send(json.dumps(envelope, ensure_ascii=False))
        try:
            result: ActionResult = await asyncio.wait_for(fut, timeout)
            # Some answers omit `action`; fill it from what we asked so describe() reads well.
            for sent, got in zip(actions, result.results):
                got.setdefault("action", sent["method"])
            return result
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise TimeoutError(f"the game did not answer ACTION {rid} ({actions[0]['method']}) within {timeout:.0f}s")

    async def start_readings(self) -> ActionResult:
        result = await self.dispatch([{"method": "start_readings", "params": []}])
        self.readings_started = result.ok
        return result

    async def stop_readings(self) -> ActionResult:
        result = await self.dispatch([{"method": "stop_readings", "params": []}])
        if result.ok:
            self.readings_started = False
        return result

    async def act_and_look(self, actions: list[dict[str, Any]]) -> tuple[ActionResult, bool]:
        """Run actions, then wait for the reading they produced. Returns (result, fresh reading arrived)."""
        before = self.pulse.readings
        result = await self.dispatch(actions)
        if result.frame is not None:
            self.last_action_frame = result.frame
        arrived = await self._await_reading(before, READING_WAIT_SECONDS, result.frame)
        return result, arrived

    async def _await_reading(self, after: int, timeout: float, frame: int | None) -> bool:
        def arrived() -> bool:
            if self.pulse.readings <= after:
                return False
            if frame is None:
                return True
            latest = self.pulse.frame
            return latest is None or latest > frame

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not arrived():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._reading_arrived.clear()
            try:
                await asyncio.wait_for(self._reading_arrived.wait(), remaining)
            except asyncio.TimeoutError:
                return False
        return True

    # ── views ─────────────────────────────────────────────────────────────

    def view(self, whole: bool = False) -> str:
        if not self.pulse.seen:
            return "(no reading yet — is the game running with readings started?)"
        rendered = self.pulse.render(since=0 if whole else None, advance=True)
        return rendered or "(nothing changed since the last view)"

    def status(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "build_id": self.build_id,
            "connected_for_s": round(time.time() - self.connected_at, 1),
            "readings_started": self.readings_started,
            "readings": self.pulse.readings,
            "scene": self.pulse.scene,
            "frame": self.pulse.frame,
            "last_pulse_ago_s": None if self._last_pulse_at is None else round(time.time() - self._last_pulse_at, 1),
            "pulse_errors": self._pulse_errors,
            "device": self.device,
        }
