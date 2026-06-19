# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc_mgr` is a local, portable web UI for browsing Claude Code's own on-disk data
(`~/.claude/projects/`, `~/.claude/tasks/`, per-project `memory/`). It reads that
data read-only for viewing, and writes only in narrow, explicit cases (task status,
CLAUDE.md/memory edits, session export/delete). No Claude account or network access.

## Commands

Use the following conda env
```
conda activate $SCRATCH/envs/gridai
```

```bash
pip install -r requirements.txt                     # FastAPI + uvicorn
python cc-mgr.py run                                 # serve on localhost:8765 — binds both 127.0.0.1 and ::1 (Ctrl+C to stop)
python cc-mgr.py run --background                    # detached; stop with `python cc-mgr.py stop`
python cc-mgr.py run --host 0.0.0.0 --port 8765 --reload   # remote / dev autoreload
python cc-mgr.py stop                                # stop a server this script started
python cc-mgr.py status                              # running?  (reads data/cc_mgr.pid)
CLAUDE_HOME=/path/to/.claude python cc-mgr.py run    # point at a different data root
```

`cc-mgr.py` is the single entry point (`run`/`stop`/`status`). `run --background`
writes `data/cc_mgr.pid` and logs to `data/cc_mgr.log`; `stop` reads that pidfile
and kills the process group (Windows: `taskkill /T`, POSIX: `killpg`). Note
`_pid_alive` uses `tasklist` on Windows — never `os.kill(pid, 0)`, which would
terminate the process there.

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
- **`backend/agents/`** — the multi-agent layer (v0.3.0). `AgentAdapter` (ABC) +
  `Capabilities` + a registry in `__init__.py`; `common.py` holds agent-neutral
  helpers (`SessionSummary`, jsonl iter, context tiers, git info, path-guarded doc
  read/write). One adapter per agent: `claude.py` (delegates to `store.py`, full
  features), `gemini.py`, `codex.py`, `copilot.py`. `get_adapter(agent_id)` returns
  the adapter (defaults to claude); `all_adapters()` / `list_agents()` enumerate.
  **Only the transcript parser truly differs per agent** — paths, doc filename, and
  capability flags are data on each adapter. Non-Claude adapters implement only
  read + `get_doc`/`save_doc`; the base raises `UnsupportedCapability` for
  tasks/memory/export/delete (mapped to HTTP 404 in `app.py`). The real on-disk
  formats are documented in `docs/ISSUES-v0.3.0.md` — **Gemini and Codex are NOT
  Claude-shaped** (Gemini = bare top-level turn records + a `$set` prelude; Codex =
  `{type,payload}` envelopes with `response_item`/`message` + `function_call*`).
- **`backend/index_db.py`** — SQLite + FTS5 full-text index over every conversation
  turn (`turns`/`turns_fts`) **and** every project's memory files + root doc
  (`docs`/`docs_fts`), across **all agents**. A **rebuildable cache** at
  `data/cc_mgr.db`. `reindex()` loops the adapter registry, stamping each row with
  `agent`; incremental for sessions (skips unchanged mtime+size), fully refreshes
  docs each run. Search results carry `agent` + a `source` field (`conversation` |
  `memory` | `claude_md` | `agent_doc`); conversation hits include `seq` (absolute
  turn index) so the UI can page to and highlight the exact turn. `search(...)`
  takes an `agent=` filter. FTS tables are standalone (not external-content) so
  deletes are a plain `DELETE` — do not switch to `content=` (a past attempt
  corrupted the index). Bump `SCHEMA_VERSION` on any schema change; `init_db` drops
  and rebuilds on mismatch (`PRAGMA user_version`). Currently `SCHEMA_VERSION = 3`
  (the `agent` column).
- **`backend/app.py`** — thin FastAPI layer: every route takes an `?agent=` query
  param, resolves the adapter via `get_adapter`, and calls it (defaults to claude).
  `GET /api/agents` returns capabilities; `GET|PUT /api/projects/{p}/doc` is the
  agent-agnostic root-doc endpoint (`/claude-md` kept as an alias). Pydantic models
  guard request bodies.
- **`frontend/{index.html,style.css,app.js}`** — no-build, no-CDN vanilla JS. `app.js`
  is a single module with a global `state` object (incl. `state.agent` + cached
  `state.caps`); the single `api()` helper appends `agent=state.agent` to every
  request, so the agent dropdown next to the brand re-scopes the whole UI. Nav
  buttons (Tasks/Memory/doc label) are gated by the active agent's `capabilities`.
  Rendering is string-template + `innerHTML` + event wiring. Three-pane layout
  (projects / sessions / detail) with draggable splitters and a collapsible sidebar;
  widths + active agent persist in `localStorage`.

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

### Other agents (v0.3.0) — verified against real data, see `docs/ISSUES-v0.3.0.md`

- **Gemini** (`$GEMINI_HOME` or `~/.gemini`): projects under `tmp/<project>/`, true
  cwd from `tmp/<project>/.project_root` (fallback `history/<project>/.project_root`,
  non-lossy). Sessions: `tmp/<project>/chats/session-*.jsonl`. **The conversation is
  NOT in `$set.messages`** (that holds only the synthetic `<session_context>`
  prelude) — real turns are **bare top-level records** appended one per line
  (`{id,type,content,thoughts,tokens,toolCalls}`), `type` ∈ `user`/`gemini`,
  `content` polymorphic (str / `[{text}]` / `[{functionResponse}]`). Doc: `GEMINI.md`.
- **Codex** (`$CODEX_HOME` or `~/.codex`): sessions under
  `sessions/YYYY/MM/DD/rollout-*.jsonl`, grouped into projects by mangled cwd. **Each
  line is a `{timestamp,type,payload}` envelope.** cwd from `session_meta.payload`;
  turns are `response_item` with `payload.type` ∈ `message` (role user/assistant/
  developer; content parts `input_text`/`output_text`) / `function_call` /
  `function_call_output`; context = `last_token_usage.total_tokens` (NOT the
  cumulative `total_token_usage`). Doc: `AGENTS.md`.
- **Copilot** (`$COPILOT_HOME` or `~/.copilot`): no CLI history on this machine yet —
  adapter is empty-but-valid (reads `history/<mangled-cwd>/*.jsonl` if present).
  Format UNVERIFIED; revalidate if real data appears. Doc: `AGENTS.md`.

## Conventions and gotchas specific to this codebase

- **Context window is NOT recorded per session**: transcripts store only the
  resolved API model id (`claude-opus-4-8`), never the `[1m]` alias or a window
  size. The `[1m]` alias lives only in `settings.json` (the *current* default, not
  what a past session used). So the window is only *provable* when usage exceeded
  200k (then it must be the 1M tier). `store.context_limit_for` returns
  `{limit, known}`; `known` is False below 200k and the UI must show "window
  unknown" rather than claiming a tier. Never key the window off the model string.
- **Git info** is read directly from `<cwd>/.git/HEAD` (`store.read_git_info`), no
  `git` binary invoked; it also handles the worktree case where `.git` is a file.
- **Deletion is soft by default**: `delete_session` moves artifacts to
  `<claude_home>/.cc_mgr_trash/<ts>/` (reversible) unless `hard=True`. It only
  removes the transcript, the `<session-uuid>/` sidecar dir, and the tasks dir.
- **Mutating writes are path-guarded**: `save_memory_file` rejects path components /
  non-`.md` names; keep that guard intact for any new write endpoint.
- **Data root is `~/.claude` on every platform** (Windows, macOS, Linux — no
  `%APPDATA%`/XDG split; `Path.home()` resolves `~` on all three). Overrides, in
  precedence order: `CLAUDE_HOME` (cc_mgr's own seam for remote deployment /
  isolated tests) then `CLAUDE_CONFIG_DIR` (Claude Code's official override for
  relocated/multi-account config dirs). Both `expanduser()`. All resolution goes
  through `store.claude_home()`.
- The running server does **not** hot-reload unless `--reload` is passed; restart it
  to pick up backend changes, and hard-refresh the browser for frontend changes.
