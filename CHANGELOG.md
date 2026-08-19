# Changelog

## v0.4.0 — 2026-08-19

- GitHub issue bot (`bot/`): label-triggered agentic auto-fix via headless
  Claude Code, delivered as a PR for review (Probot + smee, self-hosted,
  config-driven, DRY_RUN mode).

## v0.3.0 — 2026-06-18

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
