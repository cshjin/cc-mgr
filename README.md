# cc_mgr

A lightweight, portable web UI for viewing your local Claude Code projects.

Reads (read-only) from `~/.claude/projects/`, `~/.claude/tasks/`, and per-project
`memory/` — no Claude account or network access required.

## Features (v0.1 — read-only viewer)

- **Projects list** sorted by recency.
- **Sessions by time** with last-prompt preview (toggleable) and a context-size bar.
- **Per-session badges**: turn counts, model, memory presence, open/total tasks.
- **Collapsible conversation viewer** — prompts expanded, responses/tool calls
  collapse-by-default; click any turn to expand. Thinking, tool_use, and
  tool_result blocks are shown distinctly.
- **Tasks tab** — kanban-style columns (pending / in progress / completed).
- **Memory tab** — renders `MEMORY.md` and all memory files for the project.

## Quick start

```bash
pip install -r requirements.txt
python run.py            # http://127.0.0.1:8765
```

For a remote server, bind all interfaces and/or point at a different data root:

```bash
CLAUDE_HOME=/path/to/.claude python run.py --host 0.0.0.0 --port 8765
```

## Stack

- Backend: FastAPI + Uvicorn (stdlib JSON parsing; no DB yet).
- Frontend: vanilla HTML/CSS/JS, **no build step**, no CDN — fully self-contained.

## Roadmap

- [ ] Session delete with export-or-memory prompt before removal.
- [ ] Lazy/paginated conversation loading for very large sessions.
- [ ] Draggable kanban that writes back to task JSON.
- [ ] SQLite index + full-text search → local knowledge base from conversations.
- [ ] Packaging (`pip install cc-mgr` / single-file launch).

## Data model notes

Each project folder under `~/.claude/projects/` is named after the (mangled)
working directory. It contains `<session-uuid>.jsonl` transcripts, an optional
`memory/` dir, and per-session sidecar dirs. Tasks live separately under
`~/.claude/tasks/<session-uuid>/N.json`. Context size is estimated from the last
assistant turn's `usage` (input + cache_read + cache_creation + output).
