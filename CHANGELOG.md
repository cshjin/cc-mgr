# Changelog

## v0.6.0 — 2026-11-13

Session and project management for every agent (#24–#26).

- Export and delete for Codex, Copilot CLI and Gemini CLI (#24); every agent now
  exports to Markdown and deletes sessions.
  - Codex goes through `codex archive` / `codex delete`.
  - Copilot has no delete command, so cc_mgr edits its store. The rows are backed
    up first, and sessions that Copilot has open are refused.
  - Gemini CLI (retired) moves files to a trash folder.
  - The delete dialog explains what each agent's soft delete does.
- Project delete (#25): the 🗑 on a project in the sidebar removes all its sessions.
  The confirmation shows counts and last activity. Everything is exported to Markdown
  first by default. Deletes are soft by default; a permanent delete requires typing
  the folder name. Claude memory is kept unless ticked.
- Project rows match the session list (#25): `session_count` skips transcripts with
  no messages, and `has_memory` means the memory dir holds files.
- Projects whose directory no longer exists (#26) — renamed, moved, or synced from
  another machine — get a "⚠ dir missing" badge and a sidebar filter (`cwd_missing`
  in `/api/projects`).

## v0.5.0 — 2026-11-13

Review fixes (#5–#21); see #22 for the full list.

Security
- Path params are validated: crafted session ids (`..`, `*`) could wipe all Claude
  transcripts or agy conversations through the delete endpoint (#5).
- Access token for the API (printed by `cc-mgr.py run`), Host allowlist against DNS
  rebinding, `--read-only` mode, CSP and hardening headers (#6).
- `esc()` escapes quotes — no attribute injection via directory/artifact names (#7).

OpenCode v2
- Reads the v2 schema (`session_v2` + `session_message`): sessions created since
  2026-10-06 were invisible (#9). Live DB read with `mode=ro` instead of
  `immutable=1` (stale data, "database disk image is malformed") (#8).
- Archive / delete go through OpenCode (`opencode api session.update`,
  `opencode session delete`); other v2 DBs are read-only; DB discovery uses
  `opencode debug paths` (#10).

Agents
- Antigravity (agy): CHECKPOINT / ERROR_MESSAGE / unknown steps are shown, real token
  counts, no borrowed model name, reversible soft delete (#13).
- Gemini CLI is retired: its adapter is legacy and read-only; agents with data lead
  the dropdown (#14).
- Copilot CLI: reads the real `session-store.db` + `session-state/*/events.jsonl` (#15).
- Claude: cached session summaries (a 200 MB project opened in ~24 s), session titles,
  document/image placeholders, empty sessions hidden (#17).
- Context sizes from the last turn (OpenCode showed 42.7M tokens); the 200k/1M tier is
  only claimed for Claude models (#11). Subagent sessions fold under their parent (#12).

Search, UI, launcher, tests
- Deleted sessions are pruned from the index; CJK search (trigram); highlight markers
  no longer collide with brackets (#16). Kanban drags the right card (#18).
- Per-host pidfile/log; `stop` never signals a process that isn't cc_mgr (#19).
- Tests are isolated from real agent data and CLIs (#20). Docs refreshed; app version
  0.5.0; `CC_MGR_DATA_DIR` / `CC_MGR_EXPORT_DIR` (#21).

## v0.4.1 — 2026-10-02

- OpenCode adapter (#4): sessions, tasks (todo table), AGENTS.md, export, archive/delete.

## v0.4.0 — 2026-09-09

- Antigravity CLI (`agy`) support: full browsing of projects and sessions under `~/.gemini/antigravity-cli/`.
- Management features for AGY: export sessions to Markdown, soft delete (to `.cc_mgr_trash/`) & hard delete, save session summaries to memory.
- AGY Artifacts & Memory viewer: surfaces generated reports and project-level markdown documents.
- Full-text search and indexing over AGY session turns, artifacts, and root docs (`GEMINI.md` / `AGENTS.md`).
- Display human-readable conversation titles from AGY's metadata database.

- Multi-agent support: browse and search Claude, Gemini, Codex, and Copilot from
  one UI via an agent dropdown (Gemini/Codex/Copilot are read + root-doc edit only).
- Per-agent full-text search index (`agent` column); search scoped to the active agent.
- Context-window tier stays known after `/compact` (derived from peak usage).

## v0.2.2 — 2026-06-18

- Cross-platform data-root resolution (`CLAUDE_CONFIG_DIR`, relative worktree gitdir).
- Bind both `127.0.0.1` and `::1` so `localhost` and `127.0.0.1` both work.

## v0.2.0 — 2026-06-17

- Single-file launcher `cc-mgr.py` (`run`/`stop`/`status`).
- Theme toggle (dark / light / system).
- Full-text search across conversations, memory, and CLAUDE.md, with jump-to-turn.
