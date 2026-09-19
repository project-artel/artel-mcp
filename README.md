# artel-mcp

Artel QA as an MCP server. The Unity game (with the Artel SDK) connects to this process on
the developer's machine; Claude Code drives the game through tools. Nothing leaves the
machine except what Claude Code itself sends to its model.

```
Unity game + Artel SDK  ◀── ws://127.0.0.1:4320/ws/sdk ──▶  artel-mcp  ◀── stdio MCP ──▶  Claude Code
                                                              └─ .artel/artel.db + store/
```

The SDK is unchanged: artel-mcp answers the same REST paths and the same `ACTION` /
`ACTION_RESULT` / `PULSE` socket protocol the hosted orchestration does, minus login.

## Status

Prototype. Verified end to end on the sample game (WordVenture, Unity 2022.3):
register → `scan_game` (7 scenes, 417 behaviour records, 7 scene captures) → `start_readings`
→ `observe` → `click_button` → scene change judged from the reading → `report_step` →
`finish_run`, plus `capture_screen` returning the game's JPEG.

## Setup

```bash
uv sync
claude mcp add artel -- "$PWD/.venv/bin/artel-mcp"     # in the game repo, or -s user
```

Then launch the game against it. Any token works; there is one project, `local`.

```bash
ARTEL_SDK_TOKEN=local ./Game.app/Contents/MacOS/Game \
  -artel-server 127.0.0.1:4320 -artel-secure false -artel-project local
```

`game_status()` prints this line. A development build spawns its own `ArtelManager` and reads
these arguments; a scene that already carries an `ArtelManager` keeps its serialized `Server`
instead (see `scripts/build-wordventure.sh` for how the sample build strips it).

## Tools

| group | tools |
|---|---|
| status | `game_status`, `scan_game`, `start_readings`, `stop_readings` |
| observe | `observe(whole)`, `inspect(selector)`, `capture_screen(target_id)` |
| act | `click_button`, `click_at`, `move_pointer`, `press_key`, `enter_text`, `drag_pointer`, `pause_game`, `resume_game`, `reset_game` |
| content map | `list_scenes`, `describe_scene`, `find_capability` |
| run | `start_run`, `report_step`, `finish_run`, `run_summary`, `list_runs` |

Action tools return what the game answered and the reading the action produced, waiting for
a reading captured after the action's frame (artel-agent-server's ARTEL-621 rule).

## Layout

```
artel_mcp/
  main.py      one process: uvicorn gateway + MCP stdio
  gateway.py   the REST + WebSocket surface the SDK expects
  game.py      per-connection session: pulse memory, ACTION round trips, act_and_look
  pulse.py     copied from artel-agent-server app/qa/pulse.py (pulse wire format → view)
  evidence.py  renders the uploaded evidence document (scenes, wired buttons, outcomes)
  store.py     SQLite: builds, evidence, captures, runs, steps, action log
  tools.py     the MCP tools
scripts/
  build-wordventure.sh   dev macOS build of artel-sdk/samples/WordVenture for local mode
  run-wordventure.sh     launches it against 127.0.0.1:4320
```

`.artel/` is created next to where the server runs (`ARTEL_HOME` overrides). Commit it to
share the content map and run history through the game repo, or ignore it.

## Not here yet

- test case generation rules and scenario save/feasibility check (lives in orchestration today)
- the dashboard (artel-home against a local API)
- knowledge search (embeddings); `find_capability` is plain text search over evidence
- screen (per-scene runtime state) tables; the reading itself carries that for now
