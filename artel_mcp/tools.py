"""The tools Claude Code gets.

Names and parameter shapes follow artel-agent-server's QA tools where one exists, so the
skill text written for that agent still reads correctly here. Every action tool returns
two things: what the game answered, and the view the action produced — the same shape
`act_and_look` gives the hosted agent.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer as FastMCP

from artel_mcp import evidence as ev
from artel_mcp.game import GameSession
from artel_mcp.gateway import Gateway
from artel_mcp.store import Store

NO_GAME = (
    "No game is connected. Start the Unity build (or press Play) with the SDK pointed at this "
    "server — see game_status() for the launch arguments — then call again."
)


def register(mcp: FastMCP, gateway: Gateway, store: Store) -> None:
    def session_or_raise() -> GameSession:
        s = gateway.current()
        if s is None:
            raise RuntimeError(NO_GAME)
        return s

    def doc_or_raise() -> dict[str, Any]:
        s = gateway.current()
        doc = store.latest_evidence(s.build_id if s else None) or store.latest_evidence()
        if doc is None:
            raise RuntimeError("No evidence document yet. Connect the game and call scan_game().")
        return doc

    async def act(session: GameSession, actions: list[dict[str, Any]], step: int | None) -> str:
        result, arrived = await session.act_and_look(actions)
        run = store.active_run()
        store.log_action(run["id"] if run else None, step, result.request_id, actions, result.results, result.frame)
        view = session.view()
        note = "" if arrived else "\n(no new reading within 1.5s — the screen may not have changed)"
        return f"{result.describe()}{note}\n\n{view}"

    # ── status / setup ────────────────────────────────────────────────────

    @mcp.tool()
    def game_status() -> str:
        """Is a game connected, what build, has it been scanned, are readings running.

        Also prints how to launch a Unity build against this server."""
        s = gateway.current()
        lines = []
        if s is None:
            lines.append("game: not connected")
        else:
            lines.append("game: connected " + json.dumps(s.status(), ensure_ascii=False))
        build = store.latest_build()
        lines.append("latest build: " + (f"#{build.id} v{build.game_version}" if build else "none registered"))
        doc = store.latest_evidence(s.build_id if s else None) or store.latest_evidence()
        lines.append("evidence: " + (json.dumps(ev.summary(doc), ensure_ascii=False) if doc else "none — call scan_game()"))
        run = store.active_run()
        lines.append("active run: " + (f"#{run['id']} {run.get('title') or ''}" if run else "none"))
        st = gateway.settings
        lines.append(
            "\nlaunch: ARTEL_SDK_TOKEN=local <game> -artel-server "
            f"{st.host}:{st.port} -artel-secure false -artel-project {st.project_id}"
        )
        lines.append("recent: " + "; ".join(f"{e['kind']}" for e in gateway.events[-5:]))
        return "\n".join(lines)

    @mcp.tool()
    async def scan_game() -> str:
        """Ask the SDK to walk every scene in the build and upload the evidence document
        (static analysis + scene contents + one capture per scene). Takes a few seconds;
        the game screen is covered while it runs. Run once per build, or after code changes."""
        s = session_or_raise()
        result = await s.dispatch([{"method": "scan_evidence", "params": []}], timeout=180)
        doc = store.latest_evidence(s.build_id)
        return result.describe() + ("\n\nevidence: " + json.dumps(ev.summary(doc), ensure_ascii=False) if doc else "")

    @mcp.tool()
    async def start_readings() -> str:
        """Turn on live state readings (the SDK sends changes ~1/s). Needed before observe()/actions."""
        s = session_or_raise()
        return (await s.start_readings()).describe()

    @mcp.tool()
    async def stop_readings() -> str:
        """Turn live state readings off."""
        s = session_or_raise()
        return (await s.stop_readings()).describe()

    # ── observation ───────────────────────────────────────────────────────

    @mcp.tool()
    def observe(whole: bool = False) -> str:
        """Current screen as the SDK reads it: scene, interactable controls with ids and
        coordinates, visible text, watched values. Returns only what changed since the last
        view unless `whole` is true."""
        s = session_or_raise()
        return s.view(whole=whole)

    @mcp.tool()
    def inspect(selector: str) -> str:
        """Everything the reading holds about one object, by its selector (e.g. Canvas/continue)."""
        s = session_or_raise()
        return s.pulse.inspect(selector)

    @mcp.tool()
    async def capture_screen(target_id: int | None = None) -> Image:
        """A screenshot from the game (JPEG). Pass `target_id` to crop around one object.
        Use sparingly — the reading is cheaper and usually enough."""
        s = session_or_raise()
        params: list[Any] = [] if target_id is None else [target_id]
        result = await s.dispatch([{"method": "capture_screen", "params": params}], timeout=30)
        if not result.ok:
            raise RuntimeError(result.describe())
        rv = (result.results[0].get("returnValue") or {}) if result.results else {}
        url = rv.get("url") or ""
        key = url.split("/blob/", 1)[1] if "/blob/" in url else None
        data = store.read_blob(key) if key else None
        if data is None:
            raise RuntimeError(f"capture reported {url!r} but nothing was uploaded")
        return Image(data=data, format="jpeg" if key.endswith(".jpg") else "png")

    # ── actions ───────────────────────────────────────────────────────────

    @mcp.tool()
    async def click_button(target_id: int, step: int | None = None) -> str:
        """Click a button by the id shown in observe(). Presses what the game wired to it."""
        return await act(session_or_raise(), [{"method": "button_click", "params": [target_id]}], step)

    @mcp.tool()
    async def click_at(x: float, y: float, button: int = 0, step: int | None = None) -> str:
        """Move the pointer to screen pixels (x, y) and click. button 0 left, 1 right, 2 middle.
        Prefer click_button when observe() gives an id."""
        s = session_or_raise()
        return await act(
            s,
            [
                {"method": "move_mouse", "params": [x, y]},
                {"method": "mouse_down", "params": [button]},
                {"method": "mouse_up", "params": [button]},
            ],
            step,
        )

    @mcp.tool()
    async def move_pointer(x: float, y: float, step: int | None = None) -> str:
        """Move the pointer without clicking (hover)."""
        return await act(session_or_raise(), [{"method": "move_mouse", "params": [x, y]}], step)

    @mcp.tool()
    async def press_key(key_code: str, duration_seconds: float = 0.1, step: int | None = None) -> str:
        """Press and release a key (Unity KeyCode name, e.g. Space, Return, A, LeftArrow)."""
        return await act(session_or_raise(), [{"method": "key_click", "params": [key_code, duration_seconds]}], step)

    @mcp.tool()
    async def enter_text(target_id: int, value: str, step: int | None = None) -> str:
        """Type into a text field by id."""
        return await act(session_or_raise(), [{"method": "enter_text", "params": [target_id, value]}], step)

    @mcp.tool()
    async def drag_pointer(from_x: float, from_y: float, to_x: float, to_y: float, button: int = 0, step: int | None = None) -> str:
        """Press at (from_x, from_y), move to (to_x, to_y), release."""
        return await act(
            session_or_raise(),
            [
                {"method": "move_mouse", "params": [from_x, from_y]},
                {"method": "mouse_down", "params": [button]},
                {"method": "move_mouse", "params": [to_x, to_y]},
                {"method": "mouse_up", "params": [button]},
            ],
            step,
        )

    @mcp.tool()
    async def pause_game() -> str:
        """Freeze game time (Time.timeScale = 0) so you can look without the state moving."""
        return await act(session_or_raise(), [{"method": "pause_time", "params": []}], None)

    @mcp.tool()
    async def resume_game() -> str:
        """Resume game time after pause_game()."""
        return await act(session_or_raise(), [{"method": "resume_time", "params": []}], None)

    @mcp.tool()
    async def reset_game(clear_player_prefs: bool = False) -> str:
        """Reload the startup scene. `clear_player_prefs` also wipes saved data."""
        params: list[Any] = [clear_player_prefs] if clear_player_prefs else []
        return await act(session_or_raise(), [{"method": "reset_game", "params": params}], None)

    # ── content map (evidence) ────────────────────────────────────────────

    @mcp.tool()
    def list_scenes() -> str:
        """Scenes in the build, from the evidence document, with object counts."""
        doc = doc_or_raise()
        lines = [f"build: unity {ev.summary(doc)['unity']}  capture={doc.get('capture')}"]
        for name in ev.scenes(doc):
            lines.append(f"- {name}: {len(ev.objects_in(doc, name))} objects")
        return "\n".join(lines)

    @mcp.tool()
    def describe_scene(scene: str) -> str:
        """What a scene holds and what its behaviours do (trigger → effects, when condition),
        from static analysis. Read this before playing a scene for the first time."""
        return ev.render_scene(doc_or_raise(), scene)

    @mcp.tool()
    def find_capability(query: str) -> str:
        """Search behaviour records by method name, type, effect target or condition text
        (e.g. 'LoadScene', 'continue', 'PlayerPrefs')."""
        return ev.find_capabilities(doc_or_raise(), query)

    # ── run / verdicts ────────────────────────────────────────────────────

    @mcp.tool()
    def start_run(title: str) -> str:
        """Open a QA run to attach step verdicts and actions to. One active run at a time."""
        active = store.active_run()
        if active:
            return f"run #{active['id']} is still open ({active.get('title')}); call finish_run first."
        s = gateway.current()
        run_id = store.start_run(s.build_id if s else None, title)
        return f"run #{run_id} started: {title}"

    @mcp.tool()
    def report_step(step: int, verdict: str, note: str = "", evidence: str = "") -> str:
        """Record a scenario step's verdict: pass | fail | blocked. `evidence` should name
        the state you judged by (scene name, a watched value, a control that appeared)."""
        run = store.active_run()
        if run is None:
            return "no active run — call start_run(title) first."
        verdict = verdict.lower().strip()
        if verdict not in ("pass", "fail", "blocked"):
            return "verdict must be pass, fail or blocked"
        s = gateway.current()
        frame = s.pulse.frame if s else None
        sid = store.add_step(run["id"], step, verdict, note or None, evidence or None, frame)
        tail = ""
        if verdict == "fail" and s is not None:
            tail = "\nstate at failure:\n" + (s.pulse.render(since=0, advance=False) or "(no reading)")[:2000]
        return f"run #{run['id']} step {step}: {verdict} (record {sid}){tail}"

    @mcp.tool()
    def finish_run(outcome: str = "") -> str:
        """Close the active run. `outcome` defaults to pass if every step passed, else fail."""
        run = store.active_run()
        if run is None:
            return "no active run."
        steps = store.steps(run["id"])
        if not outcome:
            outcome = "fail" if any(st["verdict"] != "pass" for st in steps) else ("pass" if steps else "empty")
        store.finish_run(run["id"], outcome)
        return run_summary(run["id"])

    @mcp.tool()
    def run_summary(run_id: int | None = None) -> str:
        """Steps and their verdicts for a run (default: the latest), with the actions taken."""
        if run_id is None:
            runs = store.runs(1)
            if not runs:
                return "no runs yet."
            run_id = runs[0]["id"]
        steps = store.steps(run_id)
        actions = store.actions(run_id)
        lines = [f"run #{run_id}: {len(steps)} steps, {len(actions)} action batches"]
        for st in steps:
            lines.append(f"- step {st['step']}: {st['verdict']}" + (f" — {st['note']}" if st["note"] else "") + (f"  [{st['evidence']}]" if st["evidence"] else ""))
        if actions:
            lines.append("actions:")
            for a in actions[-30:]:
                acts = json.loads(a["actions"])
                lines.append(f"  step {a['step']}: " + ", ".join(f"{x['method']}{x.get('params', [])}" for x in acts) + f"  (frame {a['frame']})")
        return "\n".join(lines)

    @mcp.tool()
    def list_runs(limit: int = 10) -> str:
        """Recent runs with outcome."""
        runs = store.runs(limit)
        if not runs:
            return "no runs yet."
        return "\n".join(f"- #{r['id']} {r.get('title') or ''}: {r.get('outcome') or 'open'}" for r in runs)
