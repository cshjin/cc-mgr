"""FastAPI app: read-only viewer over coding-agent local project data."""
from __future__ import annotations

import hmac
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import index_db, store
from .agents import UnsupportedCapability, get_adapter, list_agents
from .agents.common import is_safe_id, is_safe_name

app = FastAPI(title="cc_mgr", version="0.3.0")

STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend"

# --- access control ---------------------------------------------------------
# Configured through env vars (set by cc-mgr.py) and read per request:
#   CC_MGR_TOKEN          if set, every /api/* request needs this token, sent as
#                         the cookie set by GET /?token=… or the X-CC-MGR-Token
#                         header. Unset = no auth (e.g. plain `uvicorn` in dev).
#   CC_MGR_ALLOWED_HOSTS  comma-separated Host names (no port); blocks DNS
#                         rebinding against a loopback-bound server.
#   CC_MGR_READ_ONLY=1    refuse every mutating /api/* call.
TOKEN_HEADER = "x-cc-mgr-token"
READ_ONLY_ALLOWED = {"/api/reindex"}  # writes only cc_mgr's own search cache
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def _token() -> str:
    return os.environ.get("CC_MGR_TOKEN", "")


def _token_cookie(request: Request) -> str:
    # Cookies ignore ports, so key the cookie by port: two servers on one host
    # (different ports, different tokens) must not overwrite each other.
    return f"cc_mgr_token_{request.url.port or 80}"


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _host_of(header: str) -> str:
    h = header.strip().lower()
    if h.startswith("["):  # [::1]:8765
        end = h.find("]")
        return h[1:end] if end != -1 else h
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def _allowed_hosts() -> set[str]:
    raw = os.environ.get("CC_MGR_ALLOWED_HOSTS", "")
    return {h.strip().lower().strip("[]") for h in raw.split(",") if h.strip()}


@app.middleware("http")
async def _guard(request: Request, call_next):
    allowed = _allowed_hosts()
    if allowed and _host_of(request.headers.get("host", "")) not in allowed:
        return PlainTextResponse("invalid Host header", status_code=400)
    path = request.url.path
    if path.startswith("/api/"):
        token = _token()
        if token:
            supplied = (request.cookies.get(_token_cookie(request))
                        or request.headers.get(TOKEN_HEADER) or "")
            if not _same(supplied, token):
                return JSONResponse(
                    {"detail": "unauthorized — open the URL printed by `cc-mgr.py run` "
                               "(it ends in ?token=…)"}, status_code=401)
        if (os.environ.get("CC_MGR_READ_ONLY") == "1"
                and request.method not in ("GET", "HEAD", "OPTIONS")
                and path not in READ_ONLY_ALLOWED):
            return JSONResponse({"detail": "cc_mgr is running in read-only mode"},
                                status_code=403)
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


def _adapter(agent: str | None):
    return get_adapter(agent)


def _project(project: str) -> str:
    """Reject anything that is not a single, non-dot path component."""
    if not is_safe_name(project):
        raise HTTPException(status_code=400, detail="invalid project name")
    return project


def _ident(value: str, what: str) -> str:
    """Session / task ids are plain tokens: no separators, dots-only or globs."""
    if not is_safe_id(value):
        raise HTTPException(status_code=400, detail=f"invalid {what}")
    return value


@app.get("/api/agents")
def api_agents():
    return list_agents()


@app.get("/api/projects")
def api_projects(agent: str | None = None):
    return _adapter(agent).list_projects()


@app.get("/api/projects/{project}/sessions")
def api_sessions(project: str, agent: str | None = None):
    _project(project)
    return [asdict(s) for s in _adapter(agent).list_sessions(project)]


@app.get("/api/projects/{project}/doc")
def api_get_doc(project: str, agent: str | None = None):
    _project(project)
    try:
        return _adapter(agent).get_doc(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class DocRequest(BaseModel):
    content: str


@app.put("/api/projects/{project}/doc")
def api_save_doc(project: str, req: DocRequest, agent: str | None = None):
    _project(project)
    try:
        path = _adapter(agent).save_doc(project, req.content)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": str(path)}


# --- back-compat aliases for the current Claude-only frontend ---
@app.get("/api/projects/{project}/claude-md")
def api_get_claude_md(project: str, agent: str | None = None):
    return api_get_doc(project, agent)


@app.put("/api/projects/{project}/claude-md")
def api_save_claude_md(project: str, req: DocRequest, agent: str | None = None):
    return api_save_doc(project, req, agent)


@app.get("/api/projects/{project}/memory")
def api_memory(project: str, agent: str | None = None):
    _project(project)
    try:
        return _adapter(agent).get_memory(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class MemoryFileRequest(BaseModel):
    name: str
    content: str


@app.put("/api/projects/{project}/memory")
def api_save_memory_file(project: str, req: MemoryFileRequest, agent: str | None = None):
    _project(project)
    try:
        path = _adapter(agent).save_memory_file(project, req.name, req.content)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"saved": str(path)}


@app.get("/api/projects/{project}/tasks")
def api_project_tasks(project: str, agent: str | None = None):
    _project(project)
    try:
        return _adapter(agent).project_tasks(project)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/api/projects/{project}/sessions/{session_id}")
def api_conversation(project: str, session_id: str, offset: int = 0,
                     limit: int = 40, agent: str | None = None):
    _project(project)
    _ident(session_id, "session id")
    result = _adapter(agent).get_conversation(project, session_id,
                                              offset=offset, limit=limit)
    if result["total"] == 0:
        raise HTTPException(status_code=404, detail="session not found or empty")
    result["session_id"] = session_id
    return result


@app.get("/api/sessions/{session_id}/tasks")
def api_tasks(session_id: str, agent: str | None = None):
    _ident(session_id, "session id")
    try:
        return _adapter(agent).get_tasks(session_id)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))


class TaskStatusRequest(BaseModel):
    status: str


@app.patch("/api/sessions/{session_id}/tasks/{task_id}")
def api_update_task(session_id: str, task_id: str, req: TaskStatusRequest,
                    agent: str | None = None):
    _ident(session_id, "session id")
    _ident(task_id, "task id")
    try:
        return _adapter(agent).update_task_status(session_id, task_id, req.status)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="task not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/projects/{project}/sessions/{session_id}/export",
         response_class=PlainTextResponse)
def api_export_inline(project: str, session_id: str, agent: str | None = None):
    _project(project)
    _ident(session_id, "session id")
    try:
        md = _adapter(agent).export_session_markdown(project, session_id)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not md.strip():
        raise HTTPException(status_code=404, detail="session not found or empty")
    return md


class DeleteRequest(BaseModel):
    export_first: bool = True
    save_memory: bool = False
    hard: bool = False


@app.post("/api/projects/{project}/sessions/{session_id}/delete")
def api_delete(project: str, session_id: str, req: DeleteRequest,
               agent: str | None = None):
    _project(project)
    _ident(session_id, "session id")
    ad = _adapter(agent)
    try:
        export_path = memory_path = None
        if req.export_first:
            export_path = str(ad.export_session_to_file(project, session_id))
        if req.save_memory:
            memory_path = str(ad.save_session_as_memory(project, session_id))
        result = ad.delete_session(project, session_id, hard=req.hard)
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:  # the agent's own CLI refused or failed
        raise HTTPException(status_code=502, detail=str(e))
    try:  # the search index is a cache: keep it from offering a deleted session
        index_db.remove_session(ad.capabilities.agent_id, session_id)
    except sqlite3.Error as e:
        result.setdefault("warnings", []).append(f"search index not updated: {e}")
    result["export"] = export_path
    result["memory"] = memory_path
    return result


@app.post("/api/projects/{project}/sessions/{session_id}/save-memory")
def api_save_memory(project: str, session_id: str, agent: str | None = None):
    _project(project)
    _ident(session_id, "session id")
    try:
        return {"memory": str(_adapter(agent).save_session_as_memory(project, session_id))}
    except UnsupportedCapability as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="session not found")


@app.get("/api/search")
def api_search(q: str, limit: int = 50, project: str | None = None,
               agent: str | None = None):
    if not q.strip():
        return {"query": q, "results": []}
    return {"query": q, "project": project, "agent": agent,
            "results": index_db.search(q, limit=limit, project=project, agent=agent)}


@app.post("/api/reindex")
def api_reindex(force: bool = False):
    return index_db.reindex(force=force)


@app.get("/api/index/stats")
def api_index_stats():
    return index_db.stats()


@app.get("/")
def index(request: Request, token: str | None = None):
    expected = _token()
    if expected and token is not None:
        if not _same(token, expected):
            return PlainTextResponse("invalid token", status_code=401)
        # Trade the URL token for an HttpOnly cookie and drop it from the URL.
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(_token_cookie(request), expected, httponly=True,
                        samesite="strict", path="/")
        return resp
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
