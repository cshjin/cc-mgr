"""FastAPI app: read-only viewer over Claude Code local project data."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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


class TaskStatusRequest(BaseModel):
    status: str


@app.patch("/api/sessions/{session_id}/tasks/{task_id}")
def api_update_task(session_id: str, task_id: str, req: TaskStatusRequest):
    try:
        return store.update_task_status(session_id, task_id, req.status)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project}/sessions/{session_id}/export", response_class=PlainTextResponse)
def api_export_inline(project: str, session_id: str):
    """Return the session as Markdown text (for in-browser preview/download)."""
    md = store.export_session_markdown(project, session_id)
    if not md.strip():
        raise HTTPException(status_code=404, detail="session not found or empty")
    return md


class DeleteRequest(BaseModel):
    export_first: bool = True
    hard: bool = False


@app.post("/api/projects/{project}/sessions/{session_id}/delete")
def api_delete(project: str, session_id: str, req: DeleteRequest):
    jsonl = store.projects_dir() / project / f"{session_id}.jsonl"
    if not jsonl.is_file():
        raise HTTPException(status_code=404, detail="session not found")
    export_path = None
    if req.export_first:
        export_path = str(store.export_session_to_file(project, session_id))
    result = store.delete_session(project, session_id, hard=req.hard)
    result["export"] = export_path
    return result


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
