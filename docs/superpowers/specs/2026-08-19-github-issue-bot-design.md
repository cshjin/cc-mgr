# GitHub Issue Bot (agentic auto-fix) — Design Spec

Date: 2026-08-19
Status: approved (design), pending implementation plan

## 1. Purpose

A self-hosted GitHub bot that watches issues on a repo, fixes them autonomously
using agentic coding (the `claude` CLI running headless on the host machine), and
delivers the fix as a pull request for the issue author to review.

- **Demo scope**: the cc-mgr repo only. The bot is config-driven so other repos
  can be added later by installing the GitHub App on them (revisit item).
- **Trigger**: an issue gets the configured trigger label (`bot:fix` default).
- **Delivery**: branch + PR (never direct pushes to `main`), with a summary
  comment on the issue linking to the PR.

## 2. Architecture

Two-process split, connected by an in-memory queue:

```
GitHub issue event ──→ smee.io tunnel ──→ Probot webhook server (Node)
                                          │ validate signature → check trigger
                                          │ → enqueue → respond 200 immediately
                                          ▼
                                   in-memory queue (single worker, concurrency 1)
                                          ▼
                                   Worker: agentic job
                                   fresh clone → claude -p (headless) → diff check
                                   → verify → commit → push branch → open PR
                                   → comment on issue
```

- The webhook handler must never block: it only validates and enqueues. The
  agentic job may take 5–30 minutes.
- Probot (Node.js) is the app/webhook layer; the heavy lifting is the `claude`
  CLI subprocess on the host. The bot's own code stays JS-only glue.

## 3. Components

All bot code lives in `bot/` at the cc-mgr repo root. Nothing inside `bot/`
hardcodes cc-mgr; every knob comes from env config (Section 7), so `bot/` can be
extracted to its own repo later (revisit item).

| File | Responsibility |
|---|---|
| `index.js` | Probot entry: app auth, event routing (`issues.opened`, `issues.labeled`), trigger checks, enqueue |
| `queue.js` | ~30-line in-memory queue, single worker (concurrency 1), dedup at enqueue time |
| `runner.js` | The agentic job: clone → run `claude -p` → read result → diff check → verify → commit/push → PR → issue comment |
| `git.js` | Worktree/clone management, branch naming, commit |
| `github-helpers.js` | Octokit wrappers: dedup lookup, comments, PR creation |
| `config.js` | Load env config with defaults; expose one frozen `botConfig` object |
| `.env.example`, `README.md` | App registration, smee, deployment instructions |

**No persisted state.** Dedup is done via the GitHub API: look for an open PR by
the bot whose head branch matches the `bot/issue-<n>-` prefix or whose body
contains `Closes #<n>`. On bot startup, a reconciliation pass re-enqueues any
open issue that has the trigger label but no bot PR — so jobs interrupted by a
bot restart are not lost.

## 4. Data flow (happy path)

1. User creates an issue, or adds the `bot:fix` label to an existing one.
2. Webhook arrives. Checks, in order: event type matches; trigger label present;
   issue author ∈ `allowedAuthors` (default: repo owner only); dedup passes (no
   existing bot PR for this issue). Enqueue, then comment `👷 starting work`.
3. Worker:
   - Fresh-clones the repo into `<workdirRoot>/issue-<n>-<random>/` (never the
     user's working copy). Checks out branch `bot/issue-<n>-<slug>` (slug from
     issue title).
   - Builds the agent prompt: issue title + body + existing comments, plus
     behavioral constraints: minimal diff, follow the repo's CLAUDE.md, do not
     touch git or commit (the runner owns git), explain in the final summary if
     the issue cannot be fixed.
   - Runs `<CLAUDE_CMD> -p "<prompt>" --output-format json [CLAUDE_ARGS]` headless,
     cwd = clone dir, with the third-party-gateway auth env inherited (Section 6).
     `--output-format json` is always appended by the runner (it is how the
     summary text is extracted); `CLAUDE_ARGS` is for anything extra.
   - Reads the JSON result's final text as the summary. Empty `git diff` → failure
     path.
   - Runs optional verify commands (default: `node --check frontend/app.js` and
     `python -m py_compile` on changed `.py` files).
   - Commits, pushes the branch, opens a PR (title `Fix #<n>: <title>`, body =
     agent summary + diff stat + `Closes #<n>`).
4. Comments on the issue: summary + PR link. Moves the label: `bot:fix` →
   `bot:done`.
5. The issue author reviews the PR; merging closes the issue via `Closes #<n>`.

## 5. Error handling

| Failure | Bot behavior |
|---|---|
| `claude` exits non-zero or times out | Comment on issue (with log tail), label `bot:failed`, job ends |
| Empty diff (agent concludes it can't fix) | Comment with the agent's reasoning, label `bot:failed`, no PR |
| Verify commands fail | Do not push; comment with failure output, label `bot:failed` |
| Push/PR creation fails (e.g. stale branch from a crashed run) | Delete stale branch, retry once; then `bot:failed` |
| Duplicate trigger (label added twice, or opened+labeled race) | Dedup at enqueue time, silently skipped |
| Bot restart mid-job | Startup reconciliation re-enqueues the issue |

Labels used: `bot:fix` (trigger), `bot:done`, `bot:failed`. The `bot:done` /
`bot:failed` labels are created by the bot if absent.

## 6. Security

1. **Identity isolation**: GitHub App (not a personal PAT). The bot acts as
   itself for PRs/comments. App private key lives only in the deployment env.
2. **Quota protection**: `allowedAuthors` config (default: repo owner's login
   only). A label from anyone else is silently ignored. README documents how to
   widen this.
3. **Clean child env**: the runner builds a minimal env for the `claude` child —
   PATH/HOME plus the auth vars the host's `claude` needs (the third-party
   gateway). The GitHub App private key, webhook secret, and smee channel are
   **not** passed. The clone is credential-free; the push is performed by the
   runner using a one-shot installation token in the push URL. The agent never
   sees any credential.
4. The agent works only in the fresh clone; the user's working copy and any other
   host state are out of reach (no path to them is given in the prompt, and the
   bot never exposes its own config dir).

## 7. Configuration surface (env)

| Env | Default | Purpose |
|---|---|---|
| `APP_ID`, `PRIVATE_KEY`, `WEBHOOK_SECRET` | — | Probot/GitHub App (standard) |
| `SMEE_URL` | — | smee.io channel |
| `TRIGGER_LABEL` | `bot:fix` | Label that triggers the bot |
| `ALLOWED_AUTHORS` | `repository.owner.login` from the webhook payload | Comma-separated logins; label events from others ignored |
| `WORKDIR_ROOT` | `./data/bot-worktrees` (gitignored) | Where fresh clones go |
| `CLAUDE_CMD` | `claude` | Path/prefix for the claude CLI |
| `CLAUDE_ARGS` | (empty) | Extra args appended after `-p` and `--output-format json` (which the runner always supplies) |
| `AGENT_TIMEOUT_MIN` | 30 | Kill the claude child after this long |
| `VERIFY_COMMANDS` | see Section 4 | Newline-separated shell commands |
| `DRY_RUN` | `0` | Run the agent for real, but only print commit/PR/comment actions |

## 8. Deployment

- Host: the Linux machine with a working headless `claude -p` (third-party
  gateway auth already configured).
- Process: systemd **user** unit `~/.config/systemd/user/cc-mgr-bot.service`
  running `npm start` in `bot/`, with an env file for all config.
- `bot/node_modules/` is added to the repo's `.gitignore` (the repo's `data/`
  entry already covers the default `WORKDIR_ROOT`).
- Webhook delivery: `smee-client` forwarding to `localhost:3000` (Probot dev
  standard).
- GitHub App: created via Probot's manifest flow, installed on cc-mgr only for
  the demo.

## 9. Testing

Repo culture: no test framework; throwaway scripts. Concretely:

- `node --check` on every `bot/*.js` (mirrors the existing frontend check).
- A local script exercising `queue.js` + `git.js` against a temp `git init
  --bare` repo — no GitHub involved.
- `DRY_RUN=1` as the main manual verification: agent runs for real, actions are
  printed instead of executed.
- E2E: one real, harmless issue on cc-mgr (e.g. a README typo), labeled `bot:fix`,
  watching the full chain to a PR.

## 10. Deferred (marked revisit — not in this implementation)

- Iterating on PR review comments (bot reads reviews and pushes follow-up fixes).
- Multi-repo queue fairness / per-repo settings.
- Extracting `bot/` into its own repo; re-registering the App against it.
