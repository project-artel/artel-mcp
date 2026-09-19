"""A fake SDK walks the gateway the way the real one does: register, upload evidence,
connect the socket, answer ACTION with ACTION_RESULT, push a PULSE. Then the tools read it."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import uvicorn
from starlette.testclient import TestClient

from artel_mcp.config import Settings
from artel_mcp.main import build

EVIDENCE = {
    "schema": 7,
    "capture": "player",
    "promises": ["selector-v1"],
    "build": {"unity": "2022.3.62f3", "sdk": "0.0.0", "digest": "abc"},
    "scenes": ["TitleScene", "Map_scene"],
    "types": {
        "Scenes.TitleSceneManager": [
            {
                "owner": "Scenes.TitleSceneManager",
                "entry": "LoadStoryScene()",
                "entryId": "Assembly-CSharp|Scenes.TitleSceneManager|LoadStoryScene|Void()",
                "source": "LoadStoryScene()",
                "methodId": "x",
                "recordKind": "candidate",
                "triggerKind": "unity-event",
                "confidence": "derived",
                "callPath": [],
                "condition": {"kind": "test", "left": "saveLoadController.LoadPlayData()", "operator": "!=", "right": "-1"},
                "inputs": [],
                "effects": [{"kind": "scene", "category": "observable", "target": "Map_scene"}],
                "calls": [],
            }
        ]
    },
    "unplaced": {},
    "objects": [
        {"path": "Canvas/continue", "selector": "Canvas/continue", "scene": "TitleScene", "active": True,
         "components": [{"type": "Scenes.TitleSceneManager"}, {"type": "UnityEngine.UI.Button"}], "visuals": [], "label": "이어하기"}
    ],
    "persistentObjects": [],
    "gaps": [],
}

PULSE = {
    "type": "PULSE", "id": 1, "schema": 3, "reading": 1, "frame": 120, "scene": "TitleScene", "whole": True,
    "active": [
        {"id": 7, "selector": "Canvas/continue", "path": "Canvas/continue", "rect": {"x": 100, "y": 200, "w": 50, "h": 20},
         "text": "이어하기", "offers": {"clicks": [{"method": "LoadStoryScene", "declaring": "TitleSceneManager"}]}, "members": []}
    ],
    "deactive": [], "gone": [], "changed": [], "statics": [],
}


@pytest.fixture
def stack(tmp_path: Path):
    mcp, gateway, store = build(Settings(home=tmp_path))
    return mcp, gateway, store


def test_rest_registration_and_evidence(stack):
    mcp, gateway, store = stack
    client = TestClient(gateway.app())

    assert client.get("/api/sdk/projects").json()["projects"][0]["id"] == "local"

    reg = client.post("/api/sdk/registrations", json={"projectId": "local", "sdkUuid": "u1", "gameVersion": "0.1", "instanceName": "mac"}).json()
    assert reg["instanceId"] == "1" and reg["gameBuildId"] == "1"

    body = json.dumps(EVIDENCE).encode()
    ticket = client.post("/api/sdk/game-builds/1/content-map/ticket", json={"contentLength": len(body)}).json()
    assert client.put(ticket["uploadUrl"].replace(gateway.settings.http_base, ""), content=body).status_code == 200

    caps = client.post("/api/sdk/game-builds/1/content-map/scene-captures/tickets",
                       json={"captures": [{"sceneName": "TitleScene", "contentType": "image/jpeg", "contentLength": 3, "width": 1, "height": 1}]}).json()
    assert client.put(caps["captures"][0]["uploadUrl"].replace(gateway.settings.http_base, ""), content=b"jpg").status_code == 200

    done = client.post("/api/sdk/game-builds/1/content-map",
                       json={"objectKey": ticket["objectKey"], "sceneCaptures": [{"sceneName": "TitleScene", "objectKey": caps["captures"][0]["objectKey"], "contentType": "image/jpeg", "width": 1, "height": 1}]}).json()
    assert done["alreadyRegistered"] is False and done["schemaVersion"] == 7

    again = client.post("/api/sdk/game-builds/1/content-map", json={"objectKey": ticket["objectKey"]}).json()
    assert again["alreadyRegistered"] is True

    doc = store.latest_evidence(1)
    assert doc["scenes"] == ["TitleScene", "Map_scene"]
    assert store.scene_capture(1, "TitleScene")["object_key"].endswith("TitleScene.jpg")


def test_socket_pulse_and_action(stack):
    mcp, gateway, store = stack
    client = TestClient(gateway.app())
    client.post("/api/sdk/registrations", json={"projectId": "local", "sdkUuid": "u1", "gameVersion": "0.1"})

    with client.websocket_connect("/ws/sdk?token=local&instanceId=1") as ws:
        session = gateway.current()
        assert session is not None and session.instance_id == 1

        ws.send_text(json.dumps(PULSE))

        async def drive():
            # The SDK answers our ACTION; run the dispatch and the fake answer together.
            task = asyncio.create_task(session.act_and_look([{"method": "button_click", "params": [7]}]))
            await asyncio.sleep(0.05)
            return task

        loop = asyncio.new_event_loop()
        task = loop.run_until_complete(drive())
        sent = json.loads(ws.receive_text())
        assert sent["type"] == "ACTION" and sent["actions"][0]["method"] == "button_click" and sent["actions"][0]["params"] == [7]
        ws.send_text(json.dumps({"type": "ACTION_RESULT", "id": 9, "requestId": sent["id"], "frame": 130,
                                 "results": [{"id": 1, "success": True, "action": "button_click", "returnValue": "ok"}]}))
        ws.send_text(json.dumps({**PULSE, "id": 2, "reading": 2, "frame": 140, "scene": "Map_scene", "whole": True,
                                 "active": [{"id": 8, "selector": "Canvas/TurnEndButton", "path": "Canvas/TurnEndButton", "rect": {"x": 0, "y": 0, "w": 10, "h": 10}, "text": "턴 종료", "offers": {"clicks": [{"method": "LoadStoryScene", "declaring": "TitleSceneManager"}]}, "members": []}], "gone": ["Canvas/continue"]}))
        # let the socket thread deliver, then finish the coroutine
        for _ in range(50):
            if task.done():
                break
            loop.run_until_complete(asyncio.sleep(0.05))
        result, arrived = loop.run_until_complete(task)
        assert result.ok and result.frame == 130
        assert arrived is True
        assert session.pulse.scene == "Map_scene"
        view = session.view(whole=True)
        assert "TurnEndButton" in view or "턴 종료" in view
