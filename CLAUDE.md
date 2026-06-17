# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc_mgr` is a local, portable web UI for browsing Claude Code's own on-disk data
(`~/.claude/projects/`, `~/.claude/tasks/`, per-project `memory/`). It reads that
data read-only for viewing, and writes only in narrow, explicit cases (task status,
CLAUDE.md/memory edits, session export/delete). No Claude account or network access.

## Commands

```bash
pip install -r requirements.txt          # FastAPI + uvicorn
python run.py                            # serve http://127.0.0.1:8765
python run.py --host 0.0.0.0 --port 8765 --reload   # remote / dev autoreload
CLAUDE_HOME=/path/to/.claude python run.py          # point at a different data root
```

There is **no build step** (vanilla JS frontend) and **no test framework** wired up.
Tests during development are done as throwaway scripts run against a temporary
`CLAUDE_HOME` (a temp dir with a synthetic `projects/`, `tasks/`, working dir + fake
`.git/HEAD`), then deleted — see git history for the pattern. To sanity-check the
frontend after edits: `node --check frontend/app.js`.

## Architecture

Three layers, all under `backend/` + `frontend/`:

- **`backend/store.py`** — the data-access layer and the single source of truth for
  the on-disk schema. Everything else depends on it. Read-only readers
  (`list_projects`, `list_sessions`, `get_conversation`, `get_memory`, `get_tasks`)
  plus the only mutating ops (`update_task_status`, `save_claude_md`,
  `save_memory_file`, `export_session_*`, `save_session_as_memory`,
  `delete_session`).
- **`backend/index_db.py`** — SQLite + FTS5 full-text index over every turn. This is
  a **rebuildable cache** at `data/cc_mgr.db`; it can always be regenerated from the
  JSONL transcripts. `reindex()` is incremental (skips sessions whose mtime+size are
  unchanged). The FTS table is standalone (not external-content) so per-session
  deletes are a plain `DELETE` — do not switch it to `content=` without rethinking
  the delete path (a past attempt corrupted the index).
- **`backend/app.py`** — thin FastAPI layer: routes call `store`/`index_db` and
  serve `frontend/` statically. Pydantic models guard request bodies.
- **`frontend/{index.html,style.css,app.js}`** — no-build, no-CDN vanilla JS. `app.js`
  is a single module with a global `state` object; rendering is string-template +
  `innerHTML` + event wiring. Three-pane layout (projects / sessions / detail) with
  draggable splitters and a collapsible sidebar; widths persist in `localStorage`.

## On-disk data model (what `store.py` reads — verified, not assumed)

- `~/.claude/projects/<mangled-cwd>/` — one folder per working directory. The folder
  name mangles the path (separators → `-`) and is **lossy**, so never reconstruct the
  real path from it. Instead recover the true `cwd` by peeking the head of the
  newest session's transcript (`_peek_session_meta`), which carries `cwd`/`gitBranch`.
- `<session-uuid>.jsonl` — transcript. Record `type`s: `user`, `assistant`,
  `last-prompt`, `mode`, `permission-mode`, `attachment`, `file-history-snapshot`.
  Content blocks: `text`, `thinking`, `tool_use`, `tool_result`. A `user` record
  whose blocks are all `tool_result` is classified as a `tool` turn, not a prompt.
- `assistant.message.usage` → context size = `input + cache_read + cache_creation +
  output` of the **last** assistant turn.
- `~/.claude/projects/<mangled>/memory/` — `MEMORY.md` + `*.md`. **Project-level and
  shared by all sessions in that folder.** Session deletion must never touch it.
- `~/.claude/tasks/<session-uuid>/N.json` — tasks `{id, subject, status, blocks,
  blockedBy, owner, metadata}`, keyed by session, separate from the projects tree.

## Conventions and gotchas specific to this codebase

- **Context window tiers**: model IDs (`claude-opus-4-7`, etc.) do **not** encode the
  1M-context (`[1m]`) variant — that's harness display only. The tier is inferred
  from observed token usage via `store.context_limit_for` (200k / 1M). Don't key
  context limits off the model string.
- **Git info** is read directly from `<cwd>/.git/HEAD` (`store.read_git_info`), no
  `git` binary invoked; it also handles the worktree case where `.git` is a file.
- **Deletion is soft by default**: `delete_session` moves artifacts to
  `<claude_home>/.cc_mgr_trash/<ts>/` (reversible) unless `hard=True`. It only
  removes the transcript, the `<session-uuid>/` sidecar dir, and the tasks dir.
- **Mutating writes are path-guarded**: `save_memory_file` rejects path components /
  non-`.md` names; keep that guard intact for any new write endpoint.
- **`CLAUDE_HOME`** overrides the data root everywhere (via `store.claude_home()`);
  this is the seam used for remote deployment and for isolated tests.
- The running server does **not** hot-reload unless `--reload` is passed; restart it
  to pick up backend changes, and hard-refresh the browser for frontend changes.
