# cc_mgr

A lightweight, portable web UI for viewing your local **coding-agent** projects —
Claude Code, Antigravity CLI (agy), OpenCode, Codex, GitHub Copilot CLI, and the
retired Gemini CLI (legacy, read-only) — selectable from a dropdown next to the
title. Agents that have data on this machine are listed first.

Reads each agent's on-disk data — Claude's `~/.claude/projects/`, `~/.claude/tasks/`
and per-project `memory/`; Antigravity's `~/.gemini/antigravity-cli/brain/`;
OpenCode's SQLite database; Codex's `$CODEX_HOME/sessions/`; Copilot CLI's
`~/.copilot/session-store.db` + `session-state/`; Gemini CLI's `~/.gemini/tmp/` —
with no account or network access required. The active agent scopes the whole UI
and search. Claude Code, Antigravity (agy) and OpenCode support session browsing,
search, Markdown export, and soft/hard session deletion (Claude and agy also
memory/artifacts; OpenCode archives/deletes through OpenCode itself). Codex and
Copilot support browsing, search, and root doc editing; Gemini CLI is browse-only.

Each agent's data root can be relocated via an env var: `CLAUDE_HOME` /
`CLAUDE_CONFIG_DIR`, `AGY_HOME` / `ANTIGRAVITY_HOME`, `OPENCODE_DB` (else
`$XDG_DATA_HOME/opencode/opencode.db`, `$OPENCODE_BASE/share/opencode/opencode.db`,
`~/.local/share/opencode/opencode.db`, or `opencode debug paths`), `CODEX_HOME`,
`COPILOT_HOME`, `GEMINI_HOME`.

## Features

- **Projects list** sorted by recency or name (toggle), showing each project's
  latest activity time, aggregated task progress, memory/root-doc presence, and
  git branch (read directly from `.git/HEAD`). Sidebar collapses; all three panes
  are resizable via draggable splitters (widths persist in `localStorage`).
- **Project-level nav** — jump straight to a project's aggregated Tasks board
  (kanban across all its sessions), project Memory, or root doc (CLAUDE.md /
  AGENTS.md / GEMINI.md) without opening a session.
- **Root doc & memory editing** — view, edit, and save the project's root doc (in
  its working dir) and any memory file; absolute paths are shown.
- **Sessions by time** with titles (Claude Code's own session titles, OpenCode,
  agy, Copilot), last-prompt preview (toggleable) and a context-size bar.
  Subagent sessions (OpenCode, agy) fold under their parent; a toggle shows them.
- **Per-session badges**: turn counts, model, subagents, memory presence, open/total tasks.
- **Collapsible conversation viewer** — prompts expanded, responses/tool calls
  collapse-by-default; click any turn to expand. Thinking, tool_use, tool_result,
  and system notes (compaction summaries, errors) are shown distinctly. Large
  sessions load lazily (40 turns per page with a "Load more" button). The context
  bar shows the live context of the last turn; a 1M window is only claimed when a
  Claude session's usage proved it.
- **Draggable kanban tasks tab** — drag cards between pending / in progress /
  completed; the new status is written back to the task JSON.
- **Memory tab** — renders `MEMORY.md` and all memory files for the project.
- **Session export + delete** — export any session to Markdown; delete with an
  export-first prompt. Delete is a soft-delete (moved to `.cc_mgr_trash/`, or
  archived in OpenCode — reversible) unless you opt into permanent removal.
- **Full-text search (local knowledge base)** — a SQLite + FTS5 (trigram) index over
  every turn in every session **plus** every project's memory files and root doc,
  in any language (CJK included). Search all folders or scope to the current one.
  Each result is tagged by source (conversation / memory / doc); clicking a
  conversation hit opens the session and scrolls to/highlights the exact turn, while
  a memory/doc hit opens that editor. Click "↻ index" to (re)build incrementally;
  deleted sessions drop out of the index.

## Quick start

```bash
pip install -r requirements.txt
python cc-mgr.py run             # prints http://localhost:8765/?token=…  (Ctrl+C to stop)
```

Open the printed URL: it carries a random **access token**, which the page swaps
for a cookie. The API refuses requests without it — on a shared machine (e.g. an
HPC login node) loopback is reachable by every logged-in user. Set `CC_MGR_TOKEN`
for a stable token; `--no-token` turns the check off (not recommended).

One file manages the whole lifecycle (works on Windows and Linux):

```bash
python cc-mgr.py run                 # foreground; Ctrl+C to stop
python cc-mgr.py run --background    # detach, return to the shell (prints the URL)
python cc-mgr.py run --read-only     # browse only: every write/delete endpoint is disabled
python cc-mgr.py status              # is it running on this host? (prints the URL again)
python cc-mgr.py stop                # stop the server started on this host
```

For a remote machine, keep the default loopback bind and forward the port:
`ssh -L 8765:localhost:8765 remote`, then open the printed URL locally. Binding
all interfaces (`--host 0.0.0.0`) works but sends the token over plain HTTP.

```bash
CLAUDE_HOME=/path/to/.claude python cc-mgr.py run   # point at a different data root
```

`--background` writes a per-host pidfile (`data/cc_mgr.<host>.pid`, 0600 — it
holds the token) and logs to `data/cc_mgr.<host>.log` (0600; the token is never
logged); `stop` uses that pidfile and only signals the process if it really is
this host's cc_mgr server (command line and start time) — handy when
the repo sits on a filesystem shared by many nodes. `CC_MGR_DATA_DIR` relocates
`data/` (pidfiles, logs, search index) and `CC_MGR_EXPORT_DIR` the Markdown
exports (default `exports/`); both are kept owner-only since they hold
conversations.

## Stack

- Backend: FastAPI + Uvicorn; stdlib `json` for transcripts, stdlib `sqlite3`
  (FTS5) for the search index. The DB lives in `data/cc_mgr.db` and is a
  rebuildable cache — delete it anytime and click "↻ index". Other agents' live
  SQLite databases are only opened read-only (`mode=ro`).
- Frontend: vanilla HTML/CSS/JS, **no build step**, no CDN — fully self-contained.
- Tests: `pip install -r requirements-dev.txt && python -m pytest`. The suite runs
  on synthetic agent homes and never touches your real data or agent CLIs.

## Roadmap

- [x] Lazy/paginated conversation loading for very large sessions.
- [x] Session delete with export-first prompt before removal.
- [x] Draggable kanban that writes back to task JSON.
- [x] SQLite index + full-text search → local knowledge base from conversations.
- [x] "Save session as memory" option in the delete dialog.
- [ ] Auto-reindex on a timer / file-watch instead of manual button.
- [ ] Packaging (`pip install cc-mgr` / single-file launch).

## Data model notes

Each project folder under `~/.claude/projects/` is named after the (mangled)
working directory. It contains `<session-uuid>.jsonl` transcripts, an optional
`memory/` dir, and per-session sidecar dirs. Tasks live separately under
`~/.claude/tasks/<session-uuid>/N.json`. Context size is estimated from the last
assistant turn's `usage` (input + cache_read + cache_creation + output). The other
agents' formats are documented in each adapter's module docstring
(`backend/agents/*.py`) and in `CLAUDE.md`.
