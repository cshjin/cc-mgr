"""FastAPI app: read-only viewer over Claude Code local project data."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import store

app = FastAPI(title="cc_mgr", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/api/projects")
def api_projects():
    return store.list_projects()


@app.get("/api/projects/{project}/sessions")
def api_sessions(project: str):
    return [asdict(s) for s in store.list_sessions(project)]


@app.get("/api/projects/{project}/memory")
def api_memory(project: str):
    return store.get_memory(project)


@app.get("/api/projects/{project}/sessions/{session_id}")
def api_conversation(
    project: str, session_id: str, offset: int = 0, limit: int = 40
):
    result = store.get_conversation(project, session_id, offset=offset, limit=limit)
    if result["total"] == 0:
        raise HTTPException(status_code=404, detail="session not found or empty")
    result["session_id"] = session_id
    return result


@app.get("/api/sessions/{session_id}/tasks")
def api_tasks(session_id: str):
    return store.get_tasks(session_id)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
