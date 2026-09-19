"""The server the SDK thinks it is talking to.

Same paths and JSON the hosted orchestration exposes to the SDK, minus login: any token is
accepted, and the one project is auto-selected. Uploads that the hosted server hands to S3
via presigned URLs land here in `.artel/store/` instead — `uploadUrl` just points back at
this process.

    GET  /api/sdk/projects
    POST /api/sdk/registrations
    POST /api/sdk/game-builds/{id}/content-map/ticket
    POST /api/sdk/game-builds/{id}/content-map/scene-captures/tickets
    POST /api/sdk/game-builds/{id}/content-map
    POST /api/sdk/qa-captures/tickets
    PUT  /blob/{key}            GET /blob/{key}
    WS   /ws/sdk?token=&instanceId=
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from artel_mcp.config import Settings
from artel_mcp.game import GameSession
from artel_mcp.store import Store

log = logging.getLogger("artel_mcp.gateway")


def _expires(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


class Gateway:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.sessions: dict[int, GameSession] = {}
        self.instances: dict[int, dict[str, Any]] = {}
        self._next_instance = 1
        self.events: list[dict[str, Any]] = []

    # ── what tools ask ────────────────────────────────────────────────────

    def current(self) -> GameSession | None:
        if not self.sessions:
            return None
        return sorted(self.sessions.values(), key=lambda s: s.connected_at)[-1]

    def note(self, kind: str, **data: Any) -> None:
        self.events.append({"at": time.time(), "kind": kind, **data})
        del self.events[:-200]

    # ── REST the SDK calls ───────────────────────────────────────────────

    async def projects(self, request: Request) -> Response:
        return JSONResponse({"projects": [{"id": self.settings.project_id, "name": self.settings.project_name}]})

    async def registrations(self, request: Request) -> Response:
        body = await request.json()
        build = self.store.register_build(
            project_id=body.get("projectId") or self.settings.project_id,
            game_version=body.get("gameVersion") or "unknown",
            sdk_uuid=body.get("sdkUuid"),
            instance_name=body.get("instanceName"),
        )
        instance_id = self._next_instance
        self._next_instance += 1
        self.instances[instance_id] = {"build_id": build.id, "name": body.get("instanceName"), "registered_at": time.time()}
        self.note("registered", instance_id=instance_id, build_id=build.id, game_version=build.game_version)
        log.info("SDK registered instance %s for build %s (%s)", instance_id, build.id, build.game_version)
        return JSONResponse(
            {
                "instanceId": str(instance_id),
                "projectId": build.project_id,
                "instanceName": body.get("instanceName") or f"local-{instance_id}",
                "gameBuildId": str(build.id),
                "gameVersion": build.game_version,
            }
        )

    async def evidence_ticket(self, request: Request) -> Response:
        build_id = int(request.path_params["build_id"])
        body = await request.json()
        key = f"evidence/{build_id}/{int(time.time() * 1000)}.json"
        return JSONResponse(
            {
                "objectKey": key,
                "uploadUrl": f"{self.settings.http_base}/blob/{key}",
                "requiredHeaders": {"Content-Type": "application/json"},
                "uploadExpiresAt": _expires(),
                "contentLength": body.get("contentLength"),
            }
        )

    async def scene_capture_tickets(self, request: Request) -> Response:
        build_id = int(request.path_params["build_id"])
        body = await request.json()
        out = []
        for c in body.get("captures", []) or []:
            scene = c.get("sceneName") or "scene"
            ext = "jpg" if "jpeg" in (c.get("contentType") or "") else "png"
            key = f"scene-captures/{build_id}/{scene}.{ext}"
            out.append(
                {
                    "sceneName": scene,
                    "objectKey": key,
                    "uploadUrl": f"{self.settings.http_base}/blob/{key}",
                    "requiredHeaders": {"Content-Type": c.get("contentType") or "image/jpeg"},
                    "uploadExpiresAt": _expires(),
                }
            )
        return JSONResponse({"captures": out})

    async def register_evidence(self, request: Request) -> Response:
        build_id = int(request.path_params["build_id"])
        body = await request.json()
        key = body.get("objectKey")
        raw = self.store.read_blob(key) if key else None
        if raw is None:
            return JSONResponse({"code": "evidence-missing", "message": f"nothing uploaded at {key}"}, status_code=400)
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse({"code": "evidence-invalid", "message": "document is not JSON"}, status_code=400)
        digest = (doc.get("build") or {}).get("digest") or hashlib.sha256(raw).hexdigest()
        evidence_id, already = self.store.register_evidence(build_id, key, digest, doc.get("schema"), len(raw))
        captures = body.get("sceneCaptures") or []
        if captures:
            self.store.record_scene_captures(build_id, captures)
        self.note("evidence", build_id=build_id, evidence_id=evidence_id, scenes=len(doc.get("scenes", [])), already=already)
        log.info("evidence %s for build %s: %d scenes, %d bytes%s", evidence_id, build_id, len(doc.get("scenes", [])), len(raw), " (already registered)" if already else "")
        return JSONResponse(
            {
                "contentMapId": str(build_id),
                "documentId": str(evidence_id),
                "capture": doc.get("capture"),
                "schemaVersion": doc.get("schema"),
                "evidenceDigest": digest,
                "byteSize": len(raw),
                "alreadyRegistered": already,
            }
        )

    async def qa_capture_ticket(self, request: Request) -> Response:
        body = await request.json()
        instance_id = body.get("instanceId")
        cid, key = self.store.new_qa_capture(
            int(instance_id) if instance_id not in (None, "") else None,
            body.get("contentType") or "image/jpeg",
            body.get("targetId"),
        )
        return JSONResponse(
            {
                "captureId": str(cid),
                "uploadUrl": f"{self.settings.http_base}/blob/{key}",
                "requiredHeaders": {"Content-Type": body.get("contentType") or "image/jpeg"},
                "downloadUrl": f"{self.settings.http_base}/blob/{key}",
                "downloadExpiresAt": _expires(24 * 60),
            }
        )

    async def blob_put(self, request: Request) -> Response:
        key = request.path_params["key"]
        data = await request.body()
        self.store.write_blob(key, data)
        return PlainTextResponse("", status_code=200)

    async def blob_get(self, request: Request) -> Response:
        key = request.path_params["key"]
        data = self.store.read_blob(key)
        if data is None:
            return PlainTextResponse("not found", status_code=404)
        media = "application/json" if key.endswith(".json") else ("image/png" if key.endswith(".png") else "image/jpeg")
        return Response(data, media_type=media)

    # ── the socket ────────────────────────────────────────────────────────

    async def sdk_socket(self, ws: WebSocket) -> None:
        token = ws.query_params.get("token")
        raw_instance = ws.query_params.get("instanceId")
        if not token or raw_instance is None:
            await ws.close(code=4001, reason="Missing credentials")
            return
        try:
            instance_id = int(raw_instance)
        except ValueError:
            await ws.close(code=4001, reason="instanceId must be a number")
            return
        if instance_id in self.sessions:
            await ws.close(code=4002, reason="Instance already connected")
            return
        await ws.accept()
        build_id = (self.instances.get(instance_id) or {}).get("build_id") or (self.store.latest_build().id if self.store.latest_build() else None)

        async def send(text: str) -> None:
            await ws.send_text(text)

        session = GameSession(instance_id=instance_id, build_id=build_id, send=send)
        self.sessions[instance_id] = session
        self.note("connected", instance_id=instance_id, build_id=build_id)
        log.info("SDK connected: instance %s (build %s)", instance_id, build_id)
        try:
            while True:
                text = await ws.receive_text()
                session.on_message(text)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            log.exception("SDK socket failed")
        finally:
            self.sessions.pop(instance_id, None)
            self.note("disconnected", instance_id=instance_id)
            log.info("SDK disconnected: instance %s", instance_id)

    # ── app ───────────────────────────────────────────────────────────────

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/api/sdk/projects", self.projects, methods=["GET"]),
                Route("/api/sdk/registrations", self.registrations, methods=["POST"]),
                Route("/api/sdk/game-builds/{build_id}/content-map/ticket", self.evidence_ticket, methods=["POST"]),
                Route("/api/sdk/game-builds/{build_id}/content-map/scene-captures/tickets", self.scene_capture_tickets, methods=["POST"]),
                Route("/api/sdk/game-builds/{build_id}/content-map", self.register_evidence, methods=["POST"]),
                Route("/api/sdk/qa-captures/tickets", self.qa_capture_ticket, methods=["POST"]),
                Route("/blob/{key:path}", self.blob_put, methods=["PUT"]),
                Route("/blob/{key:path}", self.blob_get, methods=["GET"]),
                Route("/health", lambda r: JSONResponse({"ok": True, "sessions": list(self.sessions)}), methods=["GET"]),
                WebSocketRoute("/ws/sdk", self.sdk_socket),
            ]
        )
