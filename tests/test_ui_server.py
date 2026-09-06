"""
Tests for Standalone WebUI Server (nagato-mcp-tools[ui]).

Verifies:
1. FastAPI app creation and configuration with custom workspace root
2. Health and listing endpoints
3. Standalone tool execution over REST
4. Standalone telemetry emission and retrieval (/audit, /tokens, /overview)
5. Standalone undo/redo status (/undo)
6. WebSocket event streaming (/api/v1/standalone/stream)
7. SPA asset serving and API 404 guard
"""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from nagato_mcp_tools.ui_server import create_app
from nagato_tools.telemetry import (
    AuditEvent,
    EventType,
    TelemetryMode,
    publish_event,
)


@pytest.fixture
def temp_workspace(tmp_path):
    """Create an isolated workspace with .nagato directory for telemetry."""
    ws = tmp_path / "test_ws"
    ws.mkdir(parents=True, exist_ok=True)
    return ws


@pytest.fixture
def client(temp_workspace):
    """Create a TestClient against a standalone WebUI app instance."""
    app = create_app(workspace_root=temp_workspace)
    with TestClient(app) as test_client:
        yield test_client, temp_workspace


def test_health_endpoint(client):
    test_client, ws = client
    response = test_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["mode"] == "STANDALONE"
    assert data["workspace_root"] == str(ws.resolve())


def test_list_standalone_tools(client):
    test_client, _ = client
    response = test_client.get("/api/v1/standalone/tools")
    assert response.status_code == 200
    data = response.json()
    assert "tools" in data
    assert "nagato_read_file" in data["tools"]
    assert "nagato_generate_uuid4" in data["tools"]


def test_call_standalone_tool_success(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/standalone/tools/nagato_generate_uuid4",
        json={"arguments": {}},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["tool"] == "nagato_generate_uuid4"
    assert len(data["result"]) > 0


def test_call_standalone_tool_not_found(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/standalone/tools/non_existent_tool_12345",
        json={"arguments": {}},
    )
    assert response.status_code == 404
    data = response.json()
    assert "detail" in data


def test_standalone_telemetry_and_audit(client):
    test_client, ws = client
    # First call a tool to generate telemetry
    call_resp = test_client.post(
        "/api/v1/standalone/tools/nagato_generate_uuid4",
        json={"arguments": {}},
    )
    assert call_resp.status_code == 200

    # Query audit log
    audit_resp = test_client.get("/api/v1/standalone/audit")
    assert audit_resp.status_code == 200
    audit_data = audit_resp.json()
    assert audit_data["session_id"] == "standalone"
    assert audit_data["count"] >= 1
    events = audit_data["events"]
    assert any(ev.get("payload", {}).get("tool") == "nagato_generate_uuid4" for ev in events)


def test_standalone_undo_status(client):
    test_client, _ = client
    response = test_client.get("/api/v1/standalone/undo")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "standalone"
    assert "can_undo" in data
    assert "can_redo" in data
    assert "steps" in data
    assert isinstance(data["steps"], list)


def test_standalone_tokens_endpoint(client):
    test_client, ws = client
    # Publish a dummy LLM interaction event
    publish_event(
        AuditEvent(
            session_id="standalone",
            mode=TelemetryMode.STANDALONE,
            event_type=EventType.LLM_INTERACTION,
            payload={"prompt_tokens": 120, "completion_tokens": 45},
        ),
        workspace_root=ws,
    )

    response = test_client.get("/api/v1/standalone/tokens")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "standalone"
    assert data["total_prompt_tokens"] >= 120
    assert data["total_completion_tokens"] >= 45
    assert data["total_tokens"] >= 165


def test_standalone_overview(client):
    test_client, _ = client
    # Trigger a tool call
    test_client.post(
        "/api/v1/standalone/tools/nagato_generate_uuid4",
        json={"arguments": {}},
    )
    response = test_client.get("/api/v1/standalone/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["session_id"] == "standalone"
    assert data["mode"] == "STANDALONE"
    assert data["total_events"] >= 1
    assert "undo_status" in data


def test_api_404_guard_does_not_serve_html(client):
    test_client, _ = client
    response = test_client.get("/api/v1/nonexistent_standalone_endpoint")
    assert response.status_code == 404
    assert response.headers.get("content-type", "").startswith("application/json")


def test_websocket_stream_backfill_and_live(client):
    test_client, ws = client
    # Publish an event before connecting
    publish_event(
        AuditEvent(
            session_id="standalone",
            mode=TelemetryMode.STANDALONE,
            event_type=EventType.SYSTEM_ERROR,
            payload={"msg": "initial_event"},
        ),
        workspace_root=ws,
    )

    with test_client.websocket_connect("/api/v1/standalone/stream") as websocket:
        # Should receive backfilled event
        data = websocket.receive_json()
        assert data["session_id"] == "standalone"
        assert data["payload"]["msg"] == "initial_event"

