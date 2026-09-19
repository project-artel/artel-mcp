"""Where artel-mcp keeps its files and which ports it opens.

Everything lives under one directory so a game repo can commit it (`.artel/`) or ignore it.
The SDK is pointed here with `-artel-server <host>:<port> -artel-secure false` and any
`ARTEL_SDK_TOKEN`; there is no login in local mode.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # 4320 for the SDK gateway (REST + /ws/sdk). The dashboard, when it exists, is 4321.
    host: str = "127.0.0.1"
    port: int = 4320
    home: Path = field(default_factory=lambda: Path(os.environ.get("ARTEL_HOME", ".artel")).resolve())
    # The one project the local gateway offers. The SDK auto-selects a single project.
    project_id: str = "local"
    project_name: str = "Local"

    @property
    def db_path(self) -> Path:
        return self.home / "artel.db"

    @property
    def store_dir(self) -> Path:
        return self.home / "store"

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)


def settings_from_env(**overrides) -> Settings:
    base = Settings()
    port = int(os.environ.get("ARTEL_MCP_PORT", base.port))
    host = os.environ.get("ARTEL_MCP_HOST", base.host)
    merged = {"host": host, "port": port, **overrides}
    return Settings(**merged)
