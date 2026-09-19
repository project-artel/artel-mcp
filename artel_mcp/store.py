"""SQLite behind the gateway.

The hosted orchestration keeps this in Postgres; here it is one file the game repo can commit.
Only what the prototype needs: builds the SDK registered, evidence documents it uploaded,
scene captures, and the runs/steps Claude reports while playing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS build (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    game_version TEXT NOT NULL,
    sdk_uuid TEXT,
    instance_name TEXT,
    created_at REAL NOT NULL,
    UNIQUE(project_id, game_version, sdk_uuid)
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER NOT NULL REFERENCES build(id),
    object_key TEXT NOT NULL UNIQUE,
    digest TEXT,
    schema_version INTEGER,
    byte_size INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scene_capture (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER NOT NULL REFERENCES build(id),
    scene_name TEXT NOT NULL,
    object_key TEXT,
    content_type TEXT,
    width INTEGER,
    height INTEGER,
    failure_code TEXT,
    created_at REAL NOT NULL,
    UNIQUE(build_id, scene_name)
);
CREATE TABLE IF NOT EXISTS qa_capture (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER,
    object_key TEXT NOT NULL UNIQUE,
    content_type TEXT,
    target_id INTEGER,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id INTEGER REFERENCES build(id),
    title TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS step (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES run(id),
    step INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    evidence TEXT,
    frame INTEGER,
    at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES run(id),
    step INTEGER,
    request_id INTEGER,
    actions TEXT NOT NULL,
    results TEXT,
    frame INTEGER,
    at REAL NOT NULL
);
"""


@dataclass
class Build:
    id: int
    project_id: str
    game_version: str
    sdk_uuid: str | None
    instance_name: str | None


class Store:
    def __init__(self, db_path: Path, store_dir: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store_dir.mkdir(parents=True, exist_ok=True)
        self.store_dir = store_dir
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ── builds ────────────────────────────────────────────────────────────

    def register_build(
        self, project_id: str, game_version: str, sdk_uuid: str | None, instance_name: str | None
    ) -> Build:
        row = self.conn.execute(
            "SELECT * FROM build WHERE project_id=? AND game_version=? AND sdk_uuid IS ?",
            (project_id, game_version, sdk_uuid),
        ).fetchone()
        if row is None:
            cur = self.conn.execute(
                "INSERT INTO build(project_id, game_version, sdk_uuid, instance_name, created_at) VALUES (?,?,?,?,?)",
                (project_id, game_version, sdk_uuid, instance_name, time.time()),
            )
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM build WHERE id=?", (cur.lastrowid,)).fetchone()
        return Build(row["id"], row["project_id"], row["game_version"], row["sdk_uuid"], row["instance_name"])

    def build(self, build_id: int) -> Build | None:
        row = self.conn.execute("SELECT * FROM build WHERE id=?", (build_id,)).fetchone()
        return None if row is None else Build(row["id"], row["project_id"], row["game_version"], row["sdk_uuid"], row["instance_name"])

    def latest_build(self) -> Build | None:
        row = self.conn.execute("SELECT * FROM build ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else Build(row["id"], row["project_id"], row["game_version"], row["sdk_uuid"], row["instance_name"])

    # ── blobs ─────────────────────────────────────────────────────────────

    def blob_path(self, object_key: str) -> Path:
        # object keys look like "evidence/3/abcd.json"; keep the shape on disk.
        safe = object_key.strip("/").replace("..", "_")
        path = self.store_dir / safe
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_blob(self, object_key: str, data: bytes) -> Path:
        path = self.blob_path(object_key)
        path.write_bytes(data)
        return path

    def read_blob(self, object_key: str) -> bytes | None:
        path = self.blob_path(object_key)
        return path.read_bytes() if path.exists() else None

    # ── evidence ──────────────────────────────────────────────────────────

    def register_evidence(
        self, build_id: int, object_key: str, digest: str | None, schema_version: int | None, byte_size: int
    ) -> tuple[int, bool]:
        """Returns (evidence id, already registered)."""
        existing = self.conn.execute(
            "SELECT id FROM evidence WHERE build_id=? AND digest IS ? AND digest IS NOT NULL", (build_id, digest)
        ).fetchone()
        if existing is not None:
            return existing["id"], True
        cur = self.conn.execute(
            "INSERT INTO evidence(build_id, object_key, digest, schema_version, byte_size, created_at) VALUES (?,?,?,?,?,?)",
            (build_id, object_key, digest, schema_version, byte_size, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid, False

    def latest_evidence(self, build_id: int | None = None) -> dict[str, Any] | None:
        if build_id is None:
            row = self.conn.execute("SELECT * FROM evidence ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM evidence WHERE build_id=? ORDER BY id DESC LIMIT 1", (build_id,)
            ).fetchone()
        if row is None:
            return None
        raw = self.read_blob(row["object_key"])
        if raw is None:
            return None
        doc = json.loads(raw)
        doc["_evidence_id"] = row["id"]
        doc["_build_id"] = row["build_id"]
        return doc

    def record_scene_captures(self, build_id: int, captures: list[dict[str, Any]]) -> int:
        n = 0
        for c in captures:
            self.conn.execute(
                "INSERT OR REPLACE INTO scene_capture(build_id, scene_name, object_key, content_type, width, height, failure_code, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    build_id,
                    c.get("sceneName"),
                    c.get("objectKey"),
                    c.get("contentType"),
                    c.get("width"),
                    c.get("height"),
                    c.get("failureCode"),
                    time.time(),
                ),
            )
            n += 1
        self.conn.commit()
        return n

    def scene_capture(self, build_id: int, scene_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM scene_capture WHERE build_id=? AND scene_name=?", (build_id, scene_name)
        ).fetchone()
        return None if row is None else dict(row)

    # ── qa captures ───────────────────────────────────────────────────────

    def new_qa_capture(self, instance_id: int | None, content_type: str, target_id: int | None) -> tuple[int, str]:
        at = time.time()
        cur = self.conn.execute(
            "INSERT INTO qa_capture(instance_id, object_key, content_type, target_id, created_at) VALUES (?,?,?,?,?)",
            (instance_id, f"pending-{at}", content_type, target_id, at),
        )
        cid = cur.lastrowid
        ext = "jpg" if "jpeg" in (content_type or "") else "png"
        key = f"captures/{cid}.{ext}"
        self.conn.execute("UPDATE qa_capture SET object_key=? WHERE id=?", (key, cid))
        self.conn.commit()
        return cid, key

    def qa_capture(self, capture_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM qa_capture WHERE id=?", (capture_id,)).fetchone()
        return None if row is None else dict(row)

    # ── runs / steps ──────────────────────────────────────────────────────

    def start_run(self, build_id: int | None, title: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO run(build_id, title, started_at) VALUES (?,?,?)", (build_id, title, time.time())
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, outcome: str) -> None:
        self.conn.execute("UPDATE run SET finished_at=?, outcome=? WHERE id=?", (time.time(), outcome, run_id))
        self.conn.commit()

    def active_run(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM run WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return None if row is None else dict(row)

    def add_step(self, run_id: int, step: int, verdict: str, note: str | None, evidence: str | None, frame: int | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO step(run_id, step, verdict, note, evidence, frame, at) VALUES (?,?,?,?,?,?,?)",
            (run_id, step, verdict, note, evidence, frame, time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def steps(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM step WHERE run_id=? ORDER BY id", (run_id,))]

    def log_action(
        self, run_id: int | None, step: int | None, request_id: int, actions: Any, results: Any, frame: int | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO action_log(run_id, step, request_id, actions, results, frame, at) VALUES (?,?,?,?,?,?,?)",
            (run_id, step, request_id, json.dumps(actions, ensure_ascii=False), json.dumps(results, ensure_ascii=False), frame, time.time()),
        )
        self.conn.commit()

    def actions(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM action_log WHERE run_id=? ORDER BY id", (run_id,))]

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))]
