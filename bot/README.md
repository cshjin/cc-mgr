# cc-mgr-bot

Self-hosted GitHub bot: fixes `bot:fix`-labeled issues using headless
Claude Code on this machine, delivers the fix as a PR, and comments on the
issue for the author to review. Built on [Probot](https://probot.github.io/)
v14. Demo repo: cc-mgr; the code is config-driven so other repos can be
added by installing the App on them.

See `docs/superpowers/specs/2026-08-19-github-issue-bot-design.md` for the
full design.

## How it works

1. An issue gets the `bot:fix` label (or is opened with it). Author must be
   in `ALLOWED_AUTHORS` (default: repo owner only).
2. The webhook handler enqueues the job and comments "👷 Working on this".
3. A single worker fresh-clones the repo into `data/bot-worktrees/`, checks
   out `bot/issue-<n>-<slug>`, and runs
   `claude -p "<prompt>" --output-format json --permission-mode bypassPermissions`
   headless (env: only `ANTHROPIC_*` + `CLAUDE_ENV_EXTRA` — never the bot's
   GitHub credentials).
4. If the diff is empty, verification fails, or claude errors, the trigger
   label is removed and the issue gets a `bot:failed` comment + label (with
   a log tail).
5. Otherwise: commit → push branch (one-shot installation token, never on
   disk) → open PR `Fix #<n>: <title>` with `Closes #<n>` → remove the
   trigger label, add `bot:done`, and comment with the summary + PR link.

Duplicate triggers are deduped (open-PR lookup by branch name); a restart
re-enqueues labeled issues via startup reconciliation.

## Prerequisites

- Node.js ≥ 20.18.1 (or ≥ 22)
- git
- A working headless `claude -p` on this machine (third-party gateway is
  fine; its env vars must be `ANTHROPIC_*`-named or listed in
  `CLAUDE_ENV_EXTRA`, AND present in the bot's process environment — i.e.
  in the env file, because systemd does not see interactive-shell exports)

## Setup

### 1. Create the GitHub App

GitHub → Settings → Developer settings → GitHub Apps → New GitHub App:

- **Webhook URL**: your smee channel URL + `/api/github/webhooks`
  (create one at <https://smee.io/new> first)
- **Webhook secret**: any random string — also goes in the env file
- **Permissions** (Repository): Issues → Read & write; Pull requests →
  Read & write; Contents → Read & write; Metadata → Read-only
- **Events**: Issues
- Generate and download a private key → save it as
  `~/.config/cc-mgr-bot/cc-mgr-bot.pem`
- Install the App on the `cshjin/cc-mgr` repo

### 2. Configure

```bash
mkdir -p ~/.config/cc-mgr-bot
cp .env.example ~/.config/cc-mgr-bot/env
# edit ~/.config/cc-mgr-bot/env: APP_ID, PRIVATE_KEY_PATH, WEBHOOK_SECRET, WEBHOOK_PROXY_URL, REPOS, ALLOWED_AUTHORS
```

### 3. Install and test

```bash
npm install
npm test
DRY_RUN=1 npm start   # optional: sanity-run before wiring up systemd
```

### 4. Run as a systemd user service

```bash
cp cc-mgr-bot.service.example ~/.config/systemd/user/cc-mgr-bot.service
# check the ExecStart npm path and Environment=PATH in the unit — they must
# match this machine
systemctl --user daemon-reload
systemctl --user enable --now cc-mgr-bot
systemctl --user status cc-mgr-bot   # confirm it is running
loginctl enable-linger   # keep user services running without a login session
journalctl --user -u cc-mgr-bot -f   # logs
```

The webhook tunnel needs no separate service: `WEBHOOK_PROXY_URL` in the
env file makes Probot run its own smee client.

### 5. First end-to-end check

Create the `bot:fix` label in the repo's label list first (the bot
auto-creates only `bot:done`/`bot:failed`). Open a harmless issue (e.g. a
README typo) on cc-mgr, add the `bot:fix` label, and watch for the "👷"
comment → PR. Review and merge; merging closes the issue via `Closes #<n>`.

## Security & hardening notes

- The agent runs with `bypassPermissions` and can read anything the bot's OS
  user can — including this repo's `data/` tree if `WORKDIR_ROOT` stays at
  its default (inside the checkout). For anything beyond the owner-only demo:
  run the bot as a dedicated OS user with a locked-down home, and point
  `WORKDIR_ROOT` outside the checkout (e.g. `/var/lib/cc-mgr-bot/worktrees`).
- The push token is minted lazily when the job actually runs (not at
  enqueue), so it is freshest when spent; a queue backlog longer than the
  token lifetime (~1h) would still 401 the push — rare with concurrency 1.
- Never run two bot instances against the same repo: the in-memory dedup is
  per-process, and two overlapping processes could race on the same branch
  (the stale-branch retry could delete the other's branch).
- Verification runs `python -m py_compile` (with a `python3` fallback) and
  `node --check`; the systemd unit's `Environment=PATH=` (or a `PATH=` line
  in the env file) must include both interpreters.

## Config reference

All settings come from the environment (see `.env.example`). The important
ones: `TRIGGER_LABEL`, `ALLOWED_AUTHORS`, `REPOS`, `CLAUDE_CMD`,
`CLAUDE_ARGS`, `CLAUDE_PERMISSION_MODE`, `AGENT_TIMEOUT_MIN`,
`VERIFY_COMMANDS`, `DRY_RUN`.

## Development

```bash
npm test                 # node:test — unit + local-repo integration tests
npm run check            # syntax-check every bot JS file
```

The queue/runner/git layers are testable without GitHub (fakes + local bare
repos). The Probot glue (`index.js` default export) is exercised by the real
end-to-end check in step 5.
