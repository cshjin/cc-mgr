# v0.3.0 — Multi-agent support (Claude / Gemini / Codex / Copilot)

**Date:** 2026-06-18
**Status:** Approved design, pending implementation plan

## Goal

cc_mgr currently browses only Claude Code's on-disk data. v0.3.0 adds a dropdown
next to the "cc_mgr" brand to switch the active **coding agent** (Claude, Gemini,
Codex, Copilot). Each agent stores conversations similarly (JSONL transcripts) but
differs in directory layout, transcript schema, and root doc file (`CLAUDE.md` vs
`GEMINI.md` vs `AGENTS.md`). Conversations must be browsable **and searchable** per
agent.

## Decisions (settled during brainstorming)

- **Agents shipped:** Gemini, Codex, Copilot (plus existing Claude). Qwen explicitly
  out of scope for v0.3.0.
- **Codex/Copilot reality:** little or no local conversation data on the target
  machine right now. Their adapters ship but must render an empty (not broken) state
  until data exists.
- **Switch model:** the dropdown sets ONE active agent at a time. The whole 3-pane
  view and search are scoped to that agent (not a unified/merged view).
- **Non-Claude write scope:** browse + search + **edit the root doc file**
  (`GEMINI.md` / `AGENTS.md`), reusing Claude's path-guarded doc writer. No tasks,
  no memory, no export, no delete for non-Claude agents. Claude keeps all existing
  features.
- **Architecture:** Approach A — an `AgentAdapter` protocol + registry. Today's flat
  `store.py` becomes the `ClaudeAdapter`; each other agent is a smaller adapter;
  shared logic lives in `common.py`.

## On-disk facts (verified on the target machine, not assumed)

| Agent | Conversation storage | Format | Root doc | Notes |
|---|---|---|---|---|
| Claude | `~/.claude/projects/<mangled>/<uuid>.jsonl` | turn-per-line JSONL | `CLAUDE.md` | already supported; lossy folder name |
| Gemini | `~/.gemini/tmp/<project>/chats/session-*.jsonl` | **event-sourced** JSONL: line 1 = session meta, line 2+ = `{"$set":{"messages":[…]}}` | `GEMINI.md` | true cwd from `tmp/<project>/.project_root`, falling back to `history/<project>/.project_root` when the tmp one is empty |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (standard) | turn-per-line JSONL | `AGENTS.md` | **absent on this machine** — `~/.codex` currently has only `config.toml` + `tmp/`; adapter returns `[]` gracefully |
| Copilot | no CLI conversation dir present (`~/.copilot/ide` only) | n/a yet | `AGENTS.md` | adapter ships, returns `[]` until data exists |

Cross-cutting:
- Gemini's `.project_root` is a **non-lossy** reverse map (mangled name → real cwd),
  nicer than Claude's lossy scheme; use it directly.
- Root doc files (`AGENTS.md` / `GEMINI.md` / `CLAUDE.md`) live in the project's
  **true cwd** (the repo), not the agent home — same as Claude today.

## Architecture (Approach A)

### File layout

```
backend/
  agents/
    __init__.py    # AgentAdapter ABC + Capabilities dataclass + REGISTRY
    common.py      # shared: mangling, git info, jsonl iteration, context tiers,
                   #   path-guarded doc writer, soft-delete, SessionSummary
    claude.py      # ClaudeAdapter  (today's store.py logic; full features)
    gemini.py      # GeminiAdapter  (event-sourced transcript parser)
    codex.py       # CodexAdapter   (standard sessions reader; AGENTS.md)
    copilot.py     # CopilotAdapter (AGENTS.md; sessions when present)
  store.py         # thin shim re-exporting claude_home() etc. for back-compat
  index_db.py      # gains an `agent` column (schema bump 2 -> 3)
  app.py           # routes resolve adapter via ?agent=, call its methods
```

### Request flow

`GET /api/projects?agent=gemini` → `app.py` looks up `REGISTRY["gemini"]` → calls
`adapter.list_projects()`. Unknown/missing `agent` defaults to `claude`.
`GET /api/agents` returns every adapter's `capabilities` for the frontend to read
once at load.

**Key principle:** only the transcript parser is genuinely per-agent. Paths, doc
filename, and capability flags are *data* on each adapter. Shared behavior (mangled
names, git info, soft-delete, path-guarded writes) is written once in `common.py`.

## Adapter interface

```python
@dataclass(frozen=True)
class Capabilities:
    agent_id: str        # "claude" | "gemini" | "codex" | "copilot"
    label: str           # dropdown display
    doc_filename: str    # "CLAUDE.md" | "GEMINI.md" | "AGENTS.md"
    has_memory: bool     # Claude only
    has_tasks: bool      # Claude only
    can_edit_doc: bool   # all four
    can_delete: bool     # Claude only
    can_export: bool     # Claude only

class AgentAdapter(ABC):
    capabilities: Capabilities

    def home(self) -> Path: ...                       # env-overridable per agent

    # READ — every adapter implements
    @abstractmethod
    def list_projects(self) -> list[dict]: ...
    @abstractmethod
    def list_sessions(self, project: str) -> list[SessionSummary]: ...
    @abstractmethod
    def get_conversation(self, project, session_id, **paging) -> dict: ...
    @abstractmethod
    def iter_turns(self, project, session_id) -> Iterator[dict]: ...  # for indexing
    def get_doc(self, project: str) -> dict: ...      # reads capabilities.doc_filename

    # WRITE — gated by capabilities; base raises UnsupportedCapability otherwise
    def save_doc(self, project: str, content: str) -> Path: ...       # all four
    def get_memory(self, project): ...               # Claude overrides
    def get_tasks(self, session_id): ...             # Claude overrides
    def update_task_status(self, ...): ...           # Claude overrides
    def export_session_markdown(self, ...): ...      # Claude overrides
    def delete_session(self, ...): ...               # Claude overrides
```

Design points:
- **`SessionSummary` is shared and agent-neutral** (session_id, cwd, git branch,
  mtime, turn count, context tokens). Adapters fill what they can; missing fields
  (e.g. Gemini has no per-session token usage) default to unknown, reusing the
  existing "window unknown" UI pattern.
- **`get_conversation` returns a normalized turn shape** — `{role, blocks:[{type,
  text|…}], seq}` — regardless of source format, so the existing frontend renderer
  is unchanged. Gemini's `$set`/`messages[]` event log folds to the latest
  `messages[]`, each `{type, content:[{text}]}` mapped to a normalized turn.
- **`iter_turns`** is a lightweight generator used only by the indexer, yielding
  `(seq, role, text)`.
- **Unsupported calls** raise a typed `UnsupportedCapability` mapped to HTTP 404/405.
  The frontend won't call them (it hides the buttons via capabilities); the guard is
  defense in depth.

### Per-agent capabilities matrix

| | memory | tasks | edit-doc | delete | export |
|---|---|---|---|---|---|
| Claude | ✓ | ✓ | ✓ | ✓ | ✓ |
| Gemini | ✗ | ✗ | ✓ | ✗ | ✗ |
| Codex | ✗ | ✗ | ✓ | ✗ | ✗ |
| Copilot | ✗ | ✗ | ✓ | ✗ | ✗ |

## Search / index changes

Bump `SCHEMA_VERSION` 2 → 3; `init_db` auto-drops and rebuilds on mismatch (existing
pattern). The DB stays a single rebuildable cache at `data/cc_mgr.db` shared across
agents — simpler lifecycle, one reindex, scoping via the new column.

Add an `agent` column to all carriers:

```
sessions:  + agent TEXT     -- composite key (agent, session_id)
turns:     + agent TEXT     -- carried into turns_fts
docs:      + agent TEXT     -- PK (agent, project, source, ref)
turns_fts: + agent UNINDEXED
docs_fts:  + agent UNINDEXED
```

- **`reindex()` becomes adapter-driven:** loop over `REGISTRY`; for each adapter call
  `list_projects()` / `list_sessions()` / `iter_turns()` and stamp rows with
  `adapter.capabilities.agent_id`. Incremental skip (mtime+size match) unchanged, now
  per agent. `POST /api/reindex` reindexes **all** agents in one pass so the index is
  always complete regardless of the active agent.
- **Docs:** Claude contributes `memory` + `claude_md` rows as today; Gemini/Codex/
  Copilot contribute one `source='agent_doc'` row per project (their root doc).
  Agents with no projects contribute nothing.
- **`search(query, limit=50, project=None, agent=None)`** adds `WHERE agent = ?` in
  both FTS and LIKE paths. Result rows carry `agent` (for labeling/routing) plus the
  existing `source` and `seq` (conversation hits keep jump-to-turn). `agent_doc` hits
  navigate like `claude_md` (open the doc editor pane).
- **API:** `GET /api/search?q=…&agent=gemini` (default `claude`).

## Frontend dropdown + routing

Dropdown next to the brand, populated from `GET /api/agents` (not hardcoded):

```html
<div class="brand">cc_mgr</div>
<select id="agentSel" class="agentsel" title="Active coding agent">…</select>
```

- **State seam:** add `state.agent` (default `localStorage["cc_agent"]` → `"claude"`).
  The single fetch helper `api(path)` (app.js:51) appends `agent=<state.agent>` to
  every request automatically — this routes the *entire* app (projects, sessions,
  conversation paging, doc/memory/tasks, export, delete, search) without editing each
  call site.
- **On change:** set `state.agent`, persist to localStorage, apply that agent's
  capabilities, reset `activeProject`/`activeSession`, call `loadProjects()` to render
  fresh panes.
- **Capability-driven chrome:** `has_tasks=false` hides Tasks tab + counts;
  `has_memory=false` hides Memory tab; `can_delete/can_export=false` hide those
  buttons; `doc_filename` sets the doc tab/button label dynamically; `can_edit_doc`
  (all four) keeps the doc editor available. Example — Gemini shows: Projects ·
  Sessions · Conversation · GEMINI.md (editable); Tasks/Memory/Export/Delete hidden.
- **Search:** existing scope dropdown (all / project) unchanged; `runSearch` now
  includes `agent=<state.agent>` via the same `api()` seam. Empty results
  (Codex/Copilot) use the existing "no results / no sessions" hint.
- **Polish (optional):** page `<title>` and empty-pane hints swap "Claude" for the
  active agent label.

## Empty-but-valid behavior

Codex/Copilot (and any agent home that is absent or has no sessions) return `[]` from
`list_projects()` — never an error. The projects pane renders empty with a hint like
"No Codex sessions found under ~/.codex/sessions". This is intended, given the
decision to ship those adapters now.

## Out of scope (v0.3.0)

- Qwen adapter (format is near-identical to Claude; revisit later).
- Tasks / memory / export / delete for non-Claude agents.
- Unified cross-agent project view (chose single active agent instead).
- Writing/launching agents — cc_mgr remains a viewer.

## Compatibility / migration

- `store.py` keeps re-exporting `claude_home()` and friends so existing imports and
  any throwaway test scripts keep working while logic moves into `agents/claude.py`.
- Schema bump auto-rebuilds the index cache; no manual migration.
- Default `agent=claude` everywhere means existing URLs/behavior are unchanged for
  current users.
