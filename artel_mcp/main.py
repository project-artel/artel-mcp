"""Entry point: one process that is both the SDK's server and Claude Code's MCP server.

    artel-mcp                 # stdio MCP + gateway on 127.0.0.1:4320 (what `claude mcp add` runs)
    artel-mcp --gateway-only  # just the gateway, for poking with curl / a browser
    artel-mcp --port 5000 --home /path/to/.artel

Logs go to stderr; stdout is the MCP channel and must stay clean.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer as FastMCP

from artel_mcp import tools
from artel_mcp.config import Settings, settings_from_env
from artel_mcp.gateway import Gateway
from artel_mcp.store import Store

INSTRUCTIONS = """Artel drives a Unity game that has the Artel SDK installed.

Order of operations:
1. game_status() — is a game connected? If not, tell the user the launch line it prints.
2. scan_game() once per build (skip if game_status shows evidence already).
3. start_readings(), then observe() to see the screen.
4. list_scenes() / describe_scene() to learn what a scene offers before playing it.
5. start_run(title) → act with click_button / press_key / … → report_step() per scenario step → finish_run().

Judge steps by game state in the reading (scene name, watched values, controls that appeared), not by
how the screenshot looks. Use capture_screen() only when the reading cannot answer.
"""


def build(settings: Settings) -> tuple[FastMCP, Gateway, Store]:
    settings.ensure_dirs()
    store = Store(settings.db_path, settings.store_dir)
    gateway = Gateway(settings, store)
    mcp = FastMCP("artel", instructions=INSTRUCTIONS)
    tools.register(mcp, gateway, store)
    return mcp, gateway, store


async def serve(settings: Settings, gateway_only: bool) -> None:
    mcp, gateway, _ = build(settings)
    config = uvicorn.Config(gateway.app(), host=settings.host, port=settings.port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    gateway_task = asyncio.create_task(server.serve())
    logging.getLogger("artel_mcp").info("SDK gateway on %s (home %s)", settings.http_base, settings.home)
    try:
        if gateway_only:
            await gateway_task
        else:
            await mcp.run_stdio_async()
    finally:
        server.should_exit = True
        await gateway_task


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="artel-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--home", default=None, help="directory for artel.db and uploads (default ./.artel or $ARTEL_HOME)")
    parser.add_argument("--gateway-only", action="store_true", help="run the SDK gateway without the MCP stdio server")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    if args.home:
        from pathlib import Path

        overrides["home"] = Path(args.home).resolve()
    settings = settings_from_env(**overrides)
    asyncio.run(serve(settings, args.gateway_only))


if __name__ == "__main__":
    main()
