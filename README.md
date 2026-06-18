# cc_mgr

A lightweight, portable web UI for viewing your local Claude Code projects.

Reads (read-only) from `~/.claude/projects/`, `~/.claude/tasks/`, and per-project
`memory/` — no Claude account or network access required.

## Features

- **Projects list** sorted by recency or name (toggle), showing each project's
  latest activity time, aggregated task progress, memory/CLAUDE.md presence, and
  git branch (read directly from `.git/HEAD`). Sidebar collapses; all three panes
  are resizable via draggable splitters (widths persist in `localStorage`).
- **Project-level nav** — jump straight to a project's aggregated Tasks board
  (kanban across all its sessions), project Memory, or CLAUDE.md without opening a
  session.
- **CLAUDE.md & memory editing** — view, edit, and save the project's CLAUDE.md
  (in its working dir) and any memory file; absolute paths are shown.
- **Sessions by time** with last-prompt preview (toggleable) and a context-size bar.
- **Per-session badges**: turn counts, model, memory presence, open/total tasks.
- **Collapsible conversation viewer** — prompts expanded, responses/tool calls
  collapse-by-default; click any turn to expand. Thinking, tool_use, and
  tool_result blocks are shown distinctly. Large sessions load lazily (40 turns
  per page with a "Load more" button). The context bar adapts to the session's
  actual window (200k vs 1M) instead of assuming a fixed size.
- **Draggable kanban tasks tab** — drag cards between pending / in progress /
  completed; the new status is written back to the task JSON.
- **Memory tab** — renders `MEMORY.md` and all memory files for the project.
- **Session export + delete** — export any session to Markdown; delete with an
  export-first prompt. Delete is a soft-delete (moved to `.cc_mgr_trash/`,
  reversible) unless you opt into permanent removal.
- **Full-text search (local knowledge base)** — a SQLite + FTS5 index over every
  turn in every session **plus** every project's memory files and CLAUDE.md.
  Search all folders or scope to the current one. Each result is tagged by source
  (conversation / memory / CLAUDE.md); clicking a conversation hit opens the
  session and scrolls to/highlights the exact turn, while a memory/CLAUDE.md hit
  opens that editor. Click "↻ index" to (re)build incrementally.

## Quick start

```bash
pip install -r requirements.txt
python cc-mgr.py run             # serve http://127.0.0.1:8765 (Ctrl+C to stop)
```

One file manages the whole lifecycle (works on Windows and Linux):

```bash
python cc-mgr.py run                 # foreground; Ctrl+C to stop
python cc-mgr.py run --background    # detach, return to shell
python cc-mgr.py status              # is it running?
python cc-mgr.py stop                # stop a server started by this script
```

For a remote server, bind all interfaces and/or point at a different data root:

```bash
CLAUDE_HOME=/path/to/.claude python cc-mgr.py run --host 0.0.0.0 --port 8765
```

`--background` writes a pidfile (`data/cc_mgr.pid`) and logs to `data/cc_mgr.log`;
`stop` uses that pidfile to terminate the server (and any `--reload` workers).

## Stack

- Backend: FastAPI + Uvicorn; stdlib `json` for transcripts, stdlib `sqlite3`
  (FTS5) for the search index. The DB lives in `data/cc_mgr.db` and is a
  rebuildable cache — delete it anytime and click "↻ index".
- Frontend: vanilla HTML/CSS/JS, **no build step**, no CDN — fully self-contained.

## Roadmap

- [x] Lazy/paginated conversation loading for very large sessions.
- [x] Session delete with export-first prompt before removal.
- [x] Draggable kanban that writes back to task JSON.
- [x] SQLite index + full-text search → local knowledge base from conversations.
- [ ] "Save session as memory" option in the delete dialog.
- [ ] Auto-reindex on a timer / file-watch instead of manual button.
- [ ] Packaging (`pip install cc-mgr` / single-file launch).

## Data model notes

Each project folder under `~/.claude/projects/` is named after the (mangled)
working directory. It contains `<session-uuid>.jsonl` transcripts, an optional
`memory/` dir, and per-session sidecar dirs. Tasks live separately under
`~/.claude/tasks/<session-uuid>/N.json`. Context size is estimated from the last
assistant turn's `usage` (input + cache_read + cache_creation + output).
