"""
Nagato Standalone WebUI Server

Provides a standalone HTTP/WebSocket server and React/Vite dashboard host
for nagato-mcp-tools. Requires the `[ui]` optional dependencies (fastapi, uvicorn, websockets).

Usage:
    nagato-ui [--host 127.0.0.1] [--port 8080] [--workspace .] [--session-id proj:my-app] [--open-browser]
    python -m nagato_mcp_tools.ui_server

Supports multi-project isolation via optional --session-id flag or NAGATO_SESSION_ID env var.
Formatted session IDs like "proj:my-app" are sanitized for filesystem use.
"""

import argparse
import asyncio
from importlib import resources
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Dict, Optional, Union
import webbrowser

try:
    from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel
    import uvicorn
except ImportError as err:
    missing_pkg = getattr(err, "name", "fastapi/uvicorn")
    print(f"Error: Missing optional UI dependency ({missing_pkg}).", file=sys.stderr)
    print("Please install nagato-mcp-tools with the [ui] extra:", file=sys.stderr)
    print("    pip install \"nagato-mcp-tools[ui]\"", file=sys.stderr)
    sys.exit(1)

from nagato_tools.facade import ToolFacade, get_tool_facade
from nagato_tools.ctx_mock import _resolve_session_id, _sanitize_session_id
from nagato_tools.telemetry import (
    AuditEvent,
    EventType,
    TelemetryMode,
    UndoStatus,
    add_listener,
    get_audit_file,
    publish_event,
    read_audit_events,
    remove_listener,
    set_workspace_root,
)
from nagato_tools.undo import get_standalone_undo_status

DEFAULT_SESSION_ID = "standalone"
STANDALONE_SESSION_ID = DEFAULT_SESSION_ID


def _resolve_request_session_id(session_id: Optional[str] = None) -> str:
    """Resolve session_id from query param, env var, or default."""
    if session_id:
        return _sanitize_session_id(session_id)
    return _sanitize_session_id(_resolve_session_id(None))


class StandaloneToolCallRequest(BaseModel):
    """Request to call a standalone tool directly."""
    arguments: Dict[str, Any] = {}


def resolve_webui_dist_dir() -> Optional[Path]:
    """
    Locate the built WebUI static assets directory.
    
    Checks in priority order:
    1. Package data via nagato_tools.webui_dist
    2. Package data via fsm.webui_dist
    3. Relative directory from package src
    4. Local dashboard/dist development build directory
    """
    # 1. Try nagato_tools.webui_dist package resource
    try:
        traversable = resources.files("nagato_tools.webui_dist")
        path = Path(str(traversable))
        if path.is_dir() and (path / "index.html").exists():
            return path
    except (ImportError, TypeError, ValueError, AttributeError):
        pass

    # 2. Try fsm.webui_dist package resource (if running inside repo)
    try:
        traversable = resources.files("fsm.webui_dist")
        path = Path(str(traversable))
        if path.is_dir() and (path / "index.html").exists():
            return path
    except (ImportError, TypeError, ValueError, AttributeError):
        pass

    # 3. Relative to this module
    candidate = Path(__file__).resolve().parent.parent / "nagato_tools" / "webui_dist"
    if candidate.is_dir() and (candidate / "index.html").exists():
        return candidate

    # 4. Local workspace dashboard/dist
    repo_candidate = Path.cwd() / "dashboard" / "dist"
    if repo_candidate.is_dir() and (repo_candidate / "index.html").exists():
        return repo_candidate

    return None



class InsightNodeResponse(BaseModel):
    id: str
    type: str
    title: str
    summary: str
    origin: str = "manual"
    origin_file: str = ""
    origin_symbol: str = ""
    status: str = "ACTIVE"
    last_synced_at: float = 0.0


class InsightEdgeResponse(BaseModel):
    id: int
    source_id: str
    target_id: str
    relation: str
    semantic_reason: str = ""
    linked_at_mtime: float = 0.0
    confidence: float = 1.0
    evidence_refs: list[str] = []
    created_by: str = "manual"
    tags: list[str] = []
    weight: float = 1.0


class InsightGraphResponse(BaseModel):
    session_id: str
    nodes: list[InsightNodeResponse] = []
    edges: list[InsightEdgeResponse] = []
    node_count: int = 0
    edge_count: int = 0


def create_app(workspace_root: Optional[Union[str, Path]] = None, session_id: Optional[str] = None) -> FastAPI:
    """Create and configure the Standalone WebUI FastAPI application."""
    ws_root = Path(workspace_root or Path.cwd()).resolve()
    resolved_session_id = _resolve_session_id(session_id)
    sanitized_session_id = _sanitize_session_id(resolved_session_id)
    set_workspace_root(ws_root)
    facade = ToolFacade(workspace_root=ws_root, session_id=sanitized_session_id)

    app = FastAPI(
        title="Nagato MCP Tools WebUI",
        description="Standalone WebUI & Telemetry Server for nagato-mcp-tools",
        version="0.1.1",
    )

    # Enable CORS (restricted to local loopback, no credential exposure)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Accept"],
    )

    @app.get("/health", tags=["system"])
    @app.get("/api/v1/health", tags=["system"])
    async def health():
        """Healthcheck endpoint."""
        return {
            "status": "healthy",
            "mode": "STANDALONE",
            "workspace_root": str(ws_root),
            "session_id": sanitized_session_id,
        }

    @app.get("/api/v1/standalone/tools", tags=["standalone"])
    async def list_standalone_tools():
        """List all available tools in standalone mode."""
        return {
            "tools": facade.get_registered_functions(),
            "count": len(facade.get_registered_functions()),
            "session_id": sanitized_session_id,
        }

    @app.post("/api/v1/standalone/tools/{tool_name}", tags=["standalone"])
    async def call_standalone_tool(tool_name: str, request: StandaloneToolCallRequest):
        """Call a standalone tool by name."""
        if tool_name not in facade.get_registered_functions():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tool '{tool_name}' not found. Available: {', '.join(sorted(facade.get_registered_functions()))}",
            )
        try:
            result = await facade.acall(tool_name, **request.arguments)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=str(e),
            )
        return {"tool": tool_name, "result": str(result), "session_id": sanitized_session_id}

    @app.get("/api/v1/standalone/audit", tags=["standalone"])
    async def get_standalone_audit(
        event_type: Optional[str] = None,
        limit: Optional[int] = 100,
        offset: int = 0,
        after_id: Optional[str] = None,
        session_id: Optional[str] = Query(None, description="Session ID (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Retrieve filtered, paginated audit events for standalone mode."""
        resolved_session_id = _resolve_request_session_id(session_id)
        ev_filter = None
        if event_type:
            try:
                ev_filter = EventType(event_type)
            except ValueError:
                pass

        events = await asyncio.to_thread(
            read_audit_events,
            session_id=resolved_session_id,
            event_type=ev_filter,
            limit=limit,
            offset=offset,
            after_id=after_id,
            workspace_root=ws_root,
        )
        return {
            "session_id": resolved_session_id,
            "count": len(events),
            "events": [ev.model_dump() for ev in events],
        }

    @app.get("/api/v1/standalone/tokens", tags=["standalone"])
    async def get_standalone_tokens(
        session_id: Optional[str] = Query(None, description="Session ID (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Retrieve token usage snapshots and totals for standalone mode."""
        resolved_session_id = _resolve_request_session_id(session_id)
        events = await asyncio.to_thread(
            read_audit_events,
            session_id=resolved_session_id,
            event_type=EventType.LLM_INTERACTION,
            workspace_root=ws_root,
        )
        total_prompt = sum(ev.payload.get("prompt_tokens", 0) for ev in events)
        total_comp = sum(ev.payload.get("completion_tokens", 0) for ev in events)
        return {
            "session_id": resolved_session_id,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_comp,
            "total_tokens": total_prompt + total_comp,
            "interactions": [ev.model_dump() for ev in events],
        }

    @app.get("/api/v1/standalone/undo", tags=["standalone"])
    async def get_standalone_undo(
        session_id: Optional[str] = Query(None, description="Session ID (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Retrieve undo/redo capacity and step history for standalone mode."""
        resolved_session_id = _resolve_request_session_id(session_id)
        session_facade = ToolFacade(workspace_root=ws_root, session_id=resolved_session_id)
        undo_status = await asyncio.to_thread(get_standalone_undo_status, session_facade.context)
        return undo_status.model_dump()

    @app.get("/api/v1/standalone/overview", tags=["standalone"])
    async def get_standalone_overview(
        session_id: Optional[str] = Query(None, description="Session ID (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Retrieve the aggregated dashboard state for standalone mode."""
        resolved_session_id = _resolve_request_session_id(session_id)
        session_facade = ToolFacade(workspace_root=ws_root, session_id=resolved_session_id)
        undo_status = await asyncio.to_thread(get_standalone_undo_status, session_facade.context)
        events = await asyncio.to_thread(
            read_audit_events,
            session_id=resolved_session_id,
            workspace_root=ws_root,
        )
        return {
            "session_id": resolved_session_id,
            "mode": "STANDALONE",
            "workspace_root": str(ws_root),
            "total_events": len(events),
            "undo_status": undo_status.model_dump(),
        }


    @app.get("/api/v1/standalone/insight/graph", response_model=InsightGraphResponse, tags=["standalone"])
    async def get_standalone_insight_graph(
        session_id: Optional[str] = Query(None, description="Session ID. Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Get the full Insight knowledge graph for standalone mode."""
        resolved_session_id = _resolve_request_session_id(session_id)
        
        def _build_response() -> InsightGraphResponse:
            try:
                from nagato_tools.insight_store import InsightStore
            except ImportError:
                return InsightGraphResponse(
                    session_id=resolved_session_id,
                    nodes=[],
                    edges=[],
                    node_count=0,
                    edge_count=0,
                )
            
            insight_store = InsightStore(
                workspace_root=ws_root,
                session_id=resolved_session_id,
            )
            try:
                nodes = insight_store.get_all_nodes(include_orphaned=True)
                edges = insight_store.conn.execute(
                    "SELECT id, source_id, target_id, relation, semantic_reason, linked_at_mtime, confidence, evidence_refs, created_by, tags, weight FROM memory_edges"
                ).fetchall()
                
                node_responses = [
                    InsightNodeResponse(
                        id=n.id,
                        type=n.type.value,
                        title=n.title,
                        summary=n.summary,
                        origin=n.origin,
                        origin_file=n.origin_file,
                        origin_symbol=n.origin_symbol,
                        status=n.status,
                        last_synced_at=n.last_synced_at,
                    )
                    for n in nodes
                ]
                
                edge_responses = []
                for row in edges:
                    evidence_raw = row["evidence_refs"] if "evidence_refs" in row.keys() and row["evidence_refs"] else []
                    tags_raw = row["tags"] if "tags" in row.keys() and row["tags"] else []
                    import json
                    if isinstance(evidence_raw, str):
                        try: evidence_refs = json.loads(evidence_raw)
                        except Exception: evidence_refs = []
                    else:
                        evidence_refs = list(evidence_raw)
                    if isinstance(tags_raw, str):
                        try: tags = json.loads(tags_raw)
                        except Exception: tags = []
                    else:
                        tags = list(tags_raw)

                    edge_responses.append(
                        InsightEdgeResponse(
                            id=row["id"],
                            source_id=row["source_id"],
                            target_id=row["target_id"],
                            relation=row["relation"],
                            semantic_reason=row["semantic_reason"] or "",
                            linked_at_mtime=row["linked_at_mtime"] if "linked_at_mtime" in row.keys() else 0.0,
                            confidence=row["confidence"] if "confidence" in row.keys() and row["confidence"] is not None else 1.0,
                            evidence_refs=evidence_refs,
                            created_by=row["created_by"] if "created_by" in row.keys() and row["created_by"] else "manual",
                            tags=tags,
                            weight=row["weight"] if "weight" in row.keys() and row["weight"] is not None else 1.0,
                        )
                    )
                
                return InsightGraphResponse(
                    session_id=resolved_session_id,
                    nodes=node_responses,
                    edges=edge_responses,
                    node_count=len(node_responses),
                    edge_count=len(edge_responses),
                )
            finally:
                insight_store.close()

        return await asyncio.to_thread(_build_response)

    @app.websocket("/api/v1/standalone/stream")
    async def stream_standalone_audit(
        websocket: WebSocket, 
        after_id: Optional[str] = None,
        session_id: Optional[str] = Query(None, description="Session ID (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID or 'standalone'."),
    ):
        """Live-stream audit events for standalone mode, with gap-free reconnect backfill."""
        resolved_session_id = _resolve_request_session_id(session_id)
        await websocket.accept()

        backfill = await asyncio.to_thread(
            read_audit_events,
            session_id=resolved_session_id,
            after_id=after_id,
            workspace_root=ws_root,
        )
        for event in backfill:
            await websocket.send_json(event.model_dump())

        live_queue: queue.Queue = queue.Queue()
        add_listener(resolved_session_id, live_queue.put_nowait)
        try:
            while True:
                event = await asyncio.to_thread(live_queue.get)
                if event is None:
                    break
                await websocket.send_json(event.model_dump())
        except WebSocketDisconnect:
            pass
        finally:
            remove_listener(resolved_session_id, live_queue.put_nowait)
            try:
                live_queue.put_nowait(None)
            except Exception:
                pass

    # Mount SPA static files & catch-all fallback
    dist_dir = resolve_webui_dist_dir()
    if dist_dir is not None and (dist_dir / "index.html").exists():
        assets_dir = dist_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa_assets")

        EXEMPT_PREFIXES = ("/api", "/docs", "/redoc", "/openapi.json")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa_or_fallback(full_path: str, request: Request):
            req_path = "/" + full_path.lstrip("/")
            for prefix in EXEMPT_PREFIXES:
                if req_path == prefix or req_path.startswith(prefix + "/"):
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

            candidate_file = dist_dir / full_path
            if full_path and candidate_file.is_file():
                return FileResponse(candidate_file)

            return FileResponse(dist_dir / "index.html")
    else:
        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def webui_not_built():
            return HTMLResponse(
                content="""<!DOCTYPE html>
<html>
<head><title>Nagato MCP Tools WebUI</title></head>
<body style="font-family: sans-serif; padding: 2rem; text-align: center;">
    <h2>Nagato MCP Tools WebUI is not built</h2>
    <p>The WebUI static assets were not found. To build and enable the WebUI:</p>
    <pre style="background: #f4f4f4; display: inline-block; padding: 1rem; border-radius: 4px; text-align: left;">
pip install "nagato-mcp-tools[ui]"
python sync_tools.py --build-ui
    </pre>
</body>
</html>""",
                status_code=status.HTTP_200_OK,
            )

    return app


def main():
    """CLI entry point for `nagato-ui`."""
    parser = argparse.ArgumentParser(
        description="Nagato Standalone WebUI Dashboard Server",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Target workspace root directory (default: current working directory)",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Session ID for multi-project isolation (e.g., 'proj:my-app', 'standalone'). Defaults to NAGATO_SESSION_ID env var or 'standalone'.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Automatically open WebUI in default web browser upon start",
    )

    args = parser.parse_args()

    workspace_path = Path(args.workspace).resolve()
    if not workspace_path.is_dir():
        print(f"Error: Workspace path '{workspace_path}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    app = create_app(workspace_root=workspace_path, session_id=args.session_id)

    if args.open_browser:
        def _open():
            time.sleep(1.0)
            url = f"http://{args.host}:{args.port}"
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()

    print(f"Starting Nagato Standalone WebUI on http://{args.host}:{args.port}")
    print(f"Workspace root: {workspace_path}")
    print(f"Session ID: {_sanitize_session_id(_resolve_session_id(args.session_id))}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
