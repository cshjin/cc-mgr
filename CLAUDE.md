# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc_mgr` is a local, portable web UI for browsing coding agents' own on-disk data —
Claude Code (`~/.claude/projects/`, `~/.claude/tasks/`, per-project `memory/`),
Antigravity CLI (agy), OpenCode, Codex, GitHub Copilot CLI, and the retired Gemini
CLI (legacy). It reads that data for viewing, and writes only in narrow, explicit
cases (task status, root-doc/memory edits, session/project export and delete). No
account or network access.

## Commands

Use the following conda env
```
conda activate $SCRATCH/envs/gridai
```

```bash
pip install -r requirements.txt                     # FastAPI + uvicorn
python cc-mgr.py run                                 # serve on localhost:8765 — binds both 127.0.0.1 and ::1 (Ctrl+C to stop)
python cc-mgr.py run --background                    # detached; stop with `python cc-mgr.py stop`
python cc-mgr.py run --read-only                     # every mutating endpoint returns 403
python cc-mgr.py stop                                # stop the server this script started on this host
python cc-mgr.py status                              # running here? (also lists other hosts' pidfiles)
CLAUDE_HOME=/path/to/.claude python cc-mgr.py run    # point at a different data root
python -m pytest                                     # test suite (pip install -r requirements-dev.txt)
```

`cc-mgr.py` is the single entry point (`run`/`stop`/`status`). Every start prints
`http://localhost:PORT/?token=…`: the API requires that token (exchanged for an
HttpOnly cookie on first visit) because loopback is shared by every user on a
multi-user host. `$CC_MGR_TOKEN` pins it; `--no-token` disables it (warns).
State lives in `$CC_MGR_DATA_DIR` (default `data/`, kept 0700): a **per-host**
pidfile `cc_mgr.<host>.pid` (0600, holds the token) and log `cc_mgr.<host>.log`
(0600; neither the banner nor uvicorn's access log ever records the token) — the
repo often sits on a filesystem every node mounts, and a PID only means something
on its own host. Startup also tightens state/exports an older version left
world-readable. `stop` signals a PID only if its command line is a Python
interpreter running `…/cc-mgr.py run …` (parsed like the interpreter does, so
`python -c …`/`-m …` or an editor on the file don't count) and its start time
matches the one recorded in the pidfile — via `/proc` on Linux, `ps` on
macOS/BSD, PowerShell/CIM on Windows; if identity can't be established it
refuses. It then kills the process group (Windows: `taskkill /T`, POSIX:
`killpg`). Note `_pid_alive` uses `tasklist` on Windows — never `os.kill(pid, 0)`,
which would terminate the process there.

There is **no build step** (vanilla JS frontend). Tests are pytest under `tests/`
with synthetic per-agent homes (`tests/conftest.py`). An autouse fixture points
every agent root (and `HOME`, `XDG_DATA_HOME`, the search index, exports) at an
empty sandbox and refuses to run real agent CLIs — keep it that way: a test must
never read or write real user data. Pure frontend helpers are tested under node
(`tests/test_frontend_logic.py`); `node --check frontend/app.js` is also a test.
`tests/test_frontend_dom.py` drives the real page in jsdom (the project-delete
dialog); it runs only when jsdom is reachable via `NODE_PATH`, else it is skipped.

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
  helpers (`SessionSummary`, jsonl iter, context windows, git info, path-guarded doc
  read/write, id validators, `connect_sqlite_ro`, data/export dirs, `new_trash_dir`,
  `pid_alive`, `render_markdown`). Adapters: `claude.py` (delegates to `store.py`,
  full features), `agy.py` (Antigravity CLI: memory/artifacts), `opencode.py` (v1 + v2
  schemas, archive/delete via OpenCode), `codex.py` (archive/delete via the codex
  CLI), `copilot.py` (delete by editing its store), and `gemini.py` (legacy; delete
  moves files). Every adapter exports and deletes. `get_adapter(agent_id)` returns
  the adapter (defaults to claude; supports "antigravity" alias for "agy");
  `all_adapters()` / `list_agents()` enumerate (`list_agents` adds a cheap
  `has_data` probe per adapter).
  **Only the transcript parser truly differs per agent** — paths, doc filename, and
  capability flags are data on each adapter (`Capabilities.delete_note` explains
  what a delete does, for the dialogs). Agents that group sessions by working
  directory name projects with `common.project_names`: the mangled path, unless
  another directory mangles alike (`repo-x` vs `repo/x`) or the mangled path itself
  ends like a generated name; then the mangled path plus `~` and 12 hex digits of
  the path's sha256 (longer where even those clash). Plain names never end in
  `~<12+ hex>` and generated ones always do, so a name never stands for two
  directories and a project delete can't reach into another one. Names depend on
  which directories exist: when a collision appears or goes, a name can move to
  another directory — so a project delete is bound to the directory the user
  confirmed (`expect_cwd`, below). The base class provides a generic Markdown
  export (`get_conversation` → `render_markdown`) and a generic project delete
  (`delete_project`: checks every listed session is under `expect_cwd`
  (`ProjectChanged` otherwise), exports everything first, then deletes every listed
  session through `delete_session` — parents first so cascades are skipped, an
  optional `guard(session_id)` right before each, per-session failures in
  `errors`), plus `project_delete_preview`; Claude and Gemini override both to
  remove the whole project folder (their names are folders, so they can't move).
  Adapters without a feature leave the base method, which raises
  `UnsupportedCapability` (mapped to HTTP 404 in `app.py`). Each adapter's module
  docstring documents the real on-disk format it was verified against.
- **`backend/index_db.py`** — SQLite + FTS5 full-text index over every conversation
  turn (`turns`/`turns_fts`) **and** every project's memory files + root doc
  (`docs`/`docs_fts`), across **all agents**. A **rebuildable cache** at
  `$CC_MGR_DATA_DIR/cc_mgr.db` (default `data/`). `reindex()` loops the adapter
  registry, stamping each row with `agent`; incremental for sessions (skips
  unchanged mtime+size under an unchanged project name), fully refreshes docs each
  run, and **prunes** rows for
  sessions/projects an adapter no longer lists (an adapter that raises is reported
  and left untouched). Search results carry `agent` + a `source` field
  (`conversation` | `memory` | `claude_md` | `agent_doc`); conversation hits include
  `seq` (absolute turn index) so the UI can page to and highlight the exact turn.
  `search(...)` takes an `agent=` filter and falls back to `LIKE` when FTS finds
  nothing. Snippet highlights are `\x02`…`\x03` (never brackets). FTS tables use the
  `trigram` tokenizer (CJK needs substring matching) and are standalone (not
  external-content) so deletes are a plain `DELETE` — do not switch to `content=`
  (a past attempt corrupted the index). Bump `SCHEMA_VERSION` on any schema change;
  `init_db` drops and rebuilds on mismatch (`PRAGMA user_version`). Currently
  `SCHEMA_VERSION = 4` (trigram).
- **`backend/app.py`** — thin FastAPI layer: every route takes an `?agent=` query
  param, resolves the adapter via `get_adapter`, and calls it (defaults to claude).
  `GET /api/agents` returns capabilities; `GET|PUT /api/projects/{p}/doc` is the
  agent-agnostic root-doc endpoint (`/claude-md` kept as an alias).
  `GET /api/projects` adds `cwd_missing` (the recorded working directory is gone:
  renamed, moved, or another machine). `GET /api/projects/{p}/delete-preview` and
  `POST /api/projects/{p}/delete` (`export_first`, `include_memory`, `hard`, and the
  required `expect_cwd`: the directory the confirmation showed — 409 if the name
  means another directory now) delete a whole project and purge its sessions (and,
  when the project is gone, its docs) from the index. The writes into a project's
  directory are bound the same way: `PUT /api/projects/{p}/doc` requires
  `expect_cwd` (the doc GET's `cwd`) and `PUT /api/projects/{p}/memory` requires
  `expect_dir` (the memory GET's `dir`); the adapter compares them with the
  directory it is about to write (`common.check_cwd` → `ProjectChanged` → 409).
  Pydantic models guard request
  bodies; every route validates `project`/`session_id`/`task_id`
  (`is_safe_name`/`is_safe_id`). A middleware enforces the access token
  (`CC_MGR_TOKEN`), the loopback Host allowlist (`CC_MGR_ALLOWED_HOSTS`), read-only
  mode (`CC_MGR_READ_ONLY`), and adds CSP/hardening headers.
- **`frontend/{index.html,style.css,app.js}`** — no-build, no-CDN vanilla JS. `app.js`
  is a single module with a global `state` object (incl. `state.agent` + cached
  `state.caps`); the single `api()` helper appends `agent=state.agent` to every
  request, so the agent dropdown next to the brand re-scopes the whole UI. Nav
  buttons (Tasks/Memory/doc label) are gated by the active agent's `capabilities`.
  Rendering is string-template + `innerHTML` + event wiring — every interpolated
  value goes through `esc()` (which also escapes quotes for attributes). Subagent
  sessions (`parent_id`) fold under their parent (`sessionTree`). Three-pane layout
  (projects / sessions / detail) with draggable splitters and a collapsible sidebar;
  widths + active agent persist in `localStorage`. Delete dialogs bind the agent,
  project (and its `cwd`, sent as `expect_cwd`) and session when they open; the doc
  and memory editors bind the project they were loaded for and send the directory
  they showed; a project view drops a response that arrives after the user moved
  on. When the project list reloads, the open project is followed by its directory
  if its name changed (`followProject`), and let go of if its directory is gone —
  even when its old name now means another directory.

## On-disk data model (what `store.py` reads — verified, not assumed)

- `~/.claude/projects/<mangled-cwd>/` — one folder per working directory. The folder
  name mangles the path (separators → `-`) and is **lossy**, so never reconstruct the
  real path from it. Instead recover the true `cwd` by peeking the head of the
  newest session's transcript (`_peek_session_meta`), which carries `cwd`/`gitBranch`.
- `<session-uuid>.jsonl` — transcript. Record `type`s: `user`, `assistant`,
  `last-prompt`, `mode`, `permission-mode`, `attachment`, `file-history-snapshot`,
  `ai-title` (`aiTitle`) / `custom-title` (`customTitle`) — the session title —,
  `system`, `agent-name`, … Content blocks: `text`, `thinking`, `tool_use`,
  `tool_result`, `document`/`image` (shown as placeholders, never the base64). A
  `user` record whose blocks are all `tool_result` is classified as a `tool` turn,
  not a prompt. Transcripts without user/assistant records aren't listed.
  Summaries are cached by (path, mtime, size) — transcripts reach hundreds of MB.
- `assistant.message.usage` → context size = `input + cache_read + cache_creation +
  output` of the **last** assistant turn.
- `~/.claude/projects/<mangled>/memory/` — `MEMORY.md` + `*.md`. **Project-level and
  shared by all sessions in that folder.** Session deletion must never touch it; a
  project delete only removes it when asked (`include_memory`). An empty `memory/`
  dir doesn't count as memory (`has_memory` means it holds files).
- Project rows count only transcripts with at least one user/assistant record
  (`_has_messages`: reads up to the first one, cached by mtime/size), so
  `session_count` matches the session list.
- `~/.claude/tasks/<session-uuid>/N.json` — tasks `{id, subject, status, blocks,
  blockedBy, owner, metadata}`, keyed by session, separate from the projects tree.

### Other agents — verified against real data (details in each adapter's docstring)

- **Antigravity CLI (`agy`)** (`$AGY_HOME` / `$ANTIGRAVITY_HOME` or `~/.gemini/antigravity-cli`):
  sessions under `brain/<conversation-id>/.system_generated/logs/transcript.jsonl`;
  steps `USER_INPUT`, `PLANNER_RESPONSE` (with `input_tokens`/`cache_read_tokens`/
  `output_tokens`), tool steps, `SYSTEM_MESSAGE`, `CHECKPOINT` (compaction summary),
  `ERROR_MESSAGE`; unknown step types are shown labelled, never dropped (one
  `_step_turn` table). Artifacts under `brain/<conversation-id>/*.md` surfaced in
  Memory tab. Session metadata from `conversation_summaries.db` (title, preview,
  workspace_uris, parent_conversation_id) and `history.jsonl` (workspace mapping
  fallback). No per-session model is recorded, so none is shown. Soft delete saves
  the summaries row as JSON in the trash and moves annotations too. Doc:
  `GEMINI.md` / `AGENTS.md`.
- **OpenCode (`opencode`)** (`$OPENCODE_DB`, `$XDG_DATA_HOME/opencode/opencode.db`,
  `$OPENCODE_BASE/share/opencode/opencode.db`, `~/.local/share/opencode/opencode.db`,
  else the `db` line of `opencode debug paths`): live SQLite DB — read it with
  `connect_sqlite_ro` (`mode=ro`, never `immutable=1`, which misses the WAL and
  returns "malformed" pages while OpenCode writes). **v2** (≥ 2.0): `session_v2` +
  `session_message` (`type` user/assistant/compaction/synthetic/system/shell/…,
  ordered by `seq`, parts inlined in `data.content`; tool parts are
  `{name, state{input, content[] | error}}`). **v1** (legacy): `session`, `message`,
  `part`, `todo`. v2 migrated every v1 session; `todo` stays readable for old
  sessions (v2 has no todo tool). Archive/delete go through OpenCode itself
  (`opencode api session.update`, `opencode session delete`) and only when the
  running OpenCode service on this host is writing exactly this DB — the unique
  `.db` it has open read-write with its `-wal` open too (`opencode api
  server.info` → pid → `/proc/<pid>/fd` + `fdinfo`). `opencode debug paths` can't
  prove that (it inherits `OPENCODE_DB`). `opencode session delete` also removes a
  session's descendants, so a permanent *project* delete first walks that tree:
  archived subagents in the directory are previewed and exported, and descendants
  under other directories make it refuse (archiving is still possible). It then
  only touches what was approved (the listed sessions + those archived subagents):
  right before each `opencode session delete` (or archive) the session — and for a
  delete its whole subtree — is looked up again, and anything that isn't approved
  or isn't in this directory any more (a new subagent, a session moved away) makes
  that session refused and kept. Doc: `AGENTS.md`.
- **Codex** (`$CODEX_HOME` or `~/.codex`): sessions under
  `sessions/YYYY/MM/DD/rollout-*.jsonl`, grouped into projects by mangled cwd. **Each
  line is a `{timestamp,type,payload}` envelope.** cwd from `session_meta.payload`,
  model from `turn_context.payload.model`; turns are `response_item` with
  `payload.type` ∈ `message` (role user/assistant/developer; content parts
  `input_text`/`output_text`) / `function_call` / `function_call_output`; context =
  `last_token_usage.total_tokens` (NOT the cumulative `total_token_usage`).
  Delete goes through the codex CLI with cc_mgr's `CODEX_HOME`: `codex archive <uuid>`
  (moves the rollout to `archived_sessions/`, flags `threads` in `state_*.sqlite`;
  undo with `codex unarchive`) / `codex delete --force <uuid>`. The uuid must be the
  same in the file name and `session_meta.payload.id`, and no other rollout (active
  or archived) may carry it; afterwards exactly that rollout must have left
  `sessions/`. Doc: `AGENTS.md`.
- **Copilot CLI** (`$COPILOT_HOME` or `~/.copilot`): `session-store.db` (`sessions`
  with cwd/summary/branch, `turns`, `assistant_usage_events`) plus
  `session-state/<id>/events.jsonl` (the full transcript: `user.message`,
  `assistant.message`, `tool.execution_complete`, `session.compaction_complete`,
  `session.usage_record`, …). events.jsonl wins for the conversation; the DB gives
  title/branch. The old `history/<mangled-cwd>/*.jsonl` guess is a fallback. The CLI
  can't delete sessions (`copilot sessions` only imports), so cc_mgr does: every
  table with a `session_id` column (FTS5 `search_index` included, its shadow tables
  not) plus the `sessions` row, and `session-state/<id>/`. The `BEGIN IMMEDIATE`
  transaction is taken first; holding it, the session is checked for activity again
  (whatever Copilot did while we waited for the lock is visible now) and the backup
  is read, so a soft delete saves exactly the rows it deletes (`db_rows.json`, with
  the folder, in `.cc_mgr_trash/`). On a failure it rolls back and moves the folder
  back with `common.restore_dir`, which never replaces anything, not even an empty
  directory (a plain `os.rename` would): one atomic `renameat2(RENAME_NOREPLACE)`
  where supported, else (Lustre — where the Copilot home lives on Perlmutter —
  answers EINVAL) a mkdir claim plus hard links entry by entry, which refuse names
  that appeared meanwhile. Whatever can't go back stays in the trash copy, which is
  then kept and reported. A session folder that is a symlink is refused (it could
  point at another session's folder). A `session-store.db` that resolves
  outside the Copilot home is refused. Refused too while
  `session-state/<id>/inuse.<pid>.lock` names a live copilot process, or the session
  (folder, events.jsonl or the store's `updated_at`) was active in the last two
  minutes — the home is often on a shared filesystem, so Copilot may run it on
  another node. Doc: `AGENTS.md`.
- **Gemini CLI — legacy** (`$GEMINI_HOME` or `~/.gemini`): the CLI is
  retired (agy replaces it). Projects under `tmp/<project>/`, true cwd from
  `tmp/<project>/.project_root` (fallback `history/<project>/.project_root`,
  non-lossy). Sessions: `tmp/<project>/chats/session-*.jsonl`. **The conversation is
  NOT in `$set.messages`** (that holds only the synthetic `<session_context>`
  prelude) — real turns are **bare top-level records** appended one per line
  (`{id,type,content,thoughts,tokens,toolCalls}`), `type` ∈ `user`/`gemini`,
  `content` polymorphic (str / `[{text}]` / `[{functionResponse}]`). Prelude-only
  sessions aren't listed or counted. Delete moves the files (no CLI left): a session
  is its chat file; a project is `tmp/<project>/` + `history/<project>/`. Doc:
  `GEMINI.md` (read-only here; edited from the agy view).

## Conventions and gotchas specific to this codebase

- **Context window is NOT recorded per session**: transcripts store only the
  resolved API model id (`claude-opus-4-8`), never the `[1m]` alias or a window
  size. The `[1m]` alias lives only in `settings.json` (the *current* default, not
  what a past session used). So the window is only *provable* when usage exceeded
  200k (then it must be the 1M tier) — and only for Claude models:
  `common.context_window(model, peak)` returns `{limit, known}` and never claims a
  tier for other or unknown models. The UI must show "window unknown" when `known`
  is False. Context = the **last** turn's live context, never a cumulative total.
- **Git info** is read directly from `<cwd>/.git/HEAD` (`common.read_git_info`), no
  `git` binary invoked; it handles the worktree case where `.git` is a file and
  keeps slashes in branch names.
- **Deletion is soft by default**: `delete_session` moves artifacts to
  `<claude_home>/.cc_mgr_trash/<ts>/` (reversible) unless `hard=True`. It only
  removes the transcript, the `<session-uuid>/` sidecar dir, and the tasks dir.
  Every agent's soft delete is reversible in its own way (trash folder, or the
  agent's archive for OpenCode/Codex); where the agent has a CLI command for it,
  go through that rather than editing its files. A project delete never touches
  the working directory (CLAUDE.md, AGENTS.md, `.agents/`).
- **Ids from the URL are untrusted**: routes validate them, and destructive code
  re-checks (`is_safe_id`, `is_safe_name`, `safe_join` — which resolves symlinks;
  anchor it at a trusted root such as `projects/` or the project cwd, never at a
  directory that could itself be a symlink) and only acts on sessions that exist
  (`memory` is reserved). Never join a raw id into a path or build a glob from it
  (`session_id='..'` once wiped `~/.claude/projects`, `'*'` every agy
  conversation, `'memory'` a project's memory).
- **cc_mgr's own files are private**: the search index (it copies every
  conversation), exports, pidfile and log are created owner-only.
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
