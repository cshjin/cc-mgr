# Changelog

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
