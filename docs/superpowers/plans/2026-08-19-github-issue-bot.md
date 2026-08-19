# GitHub Issue Bot (agentic auto-fix) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `bot/`, a self-hosted Probot GitHub App inside the cc-mgr repo that fixes `bot:fix`-labeled issues via headless Claude Code and delivers the fix as a PR.

**Architecture:** Probot v14 (ESM, Node ≥ 20.18.1) webhook server + in-memory single-worker queue; the worker fresh-clones the repo, runs `claude -p` headless, verifies the diff, commits/pushes a branch, opens a PR, and comments on the issue. DRY_RUN mode turns external side effects into log lines so everything except the GitHub App glue is testable with fakes and a local git repo.

**Tech Stack:** Node.js (≥ 20.18.1, ESM, built-in `node:test` — no test framework), Probot ^14.3.2, `claude` CLI (already configured on the host via third-party gateway env vars), git.

Spec: `docs/superpowers/specs/2026-08-19-github-issue-bot-design.md`. Verified during planning: claude CLI v2.1.x `-p "<prompt>" --output-format json` prints one JSON doc on stdout with `result` = final text and `is_error` on run failure; `--permission-mode bypassPermissions` is current; there is **no** wall-clock timeout flag (runner enforces its own SIGTERM timer); gateway auth env vars are `ANTHROPIC_*`. Probot v14: `app.auth(installationId)` returns the installation Octokit, `await app.auth()` (no args) is app-level (JWT); there is **no** `app.octokit` or `getInstallationOctokit`.

**Deviations from the spec (all improvements, agreed in planning):**
- `WORKDIR_ROOT` default is `../data/bot-worktrees` (repo-root `data/`, already gitignored) — `bot/data/` would not be ignored.
- The spec's "default verify commands" (`node --check frontend/app.js`, `py_compile` changed files) are implemented generically: every changed `.js` gets `node --check`, every changed `.py` gets `python -m py_compile`; `VERIFY_COMMANDS` holds extra free-form commands.
- Push token comes from `apps.createInstallationAccessToken` via the app-level octokit (guaranteed endpoint) instead of guessing `.auth()` shapes.
- Env-var names differ slightly from spec §7: the smee tunnel uses Probot's real variable `WEBHOOK_PROXY_URL` (spec called it `SMEE_URL`), and the private key uses `PRIVATE_KEY_PATH` (probot accepts it; a PEM path is systemd-EnvironmentFile-friendlier than inline contents). Added beyond spec §7: `CLAUDE_PERMISSION_MODE`, `CLAUDE_ENV_EXTRA`, `BOT_GIT_NAME`, `BOT_GIT_EMAIL`, `REPOS`, `BRANCH_PREFIX`, `DONE_LABEL`, `FAILED_LABEL`.

---

## Task 1: Scaffold `bot/`

**Files:**
- Create: `bot/package.json`
- Modify: `.gitignore` (append two lines)

- [ ] **Step 1.1: Check the Node version**

Run: `node --version`
Expected: `v20.18.1` or newer (or any v22+). If older, stop and tell the user — Probot v14 requires `^20.18.1 || >=22`.

- [ ] **Step 1.2: Create `bot/package.json`**

```json
{
  "name": "cc-mgr-bot",
  "version": "0.1.0",
  "private": true,
  "description": "GitHub bot: fixes labeled issues via headless Claude Code, delivers as PR",
  "type": "module",
  "engines": {
    "node": "^20.18.1 || >=22"
  },
  "scripts": {
    "start": "probot run ./index.js",
    "check": "node --check index.js && node --check config.js && node --check queue.js && node --check git.js && node --check github-helpers.js && node --check runner.js",
    "test": "node --test"
  },
  "dependencies": {
    "probot": "^14.3.2"
  }
}
```

Note: the `test` script is bare `node --test` (NOT `node --test test/`) — on Node ≥21 positional args to `--test` are glob patterns, and the directory form fails on Node 22+; bare `node --test` discovers `test/` correctly across the whole supported range.

- [ ] **Step 1.3: Append `bot/` ignores to the repo root `.gitignore`**

Append these two lines to `/home/hjin/shared/coding/cc-mgr/.gitignore` (after the existing `data/` line):

```
# GitHub issue bot
bot/node_modules/
```

- [ ] **Step 1.4: Install dependencies**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && npm install`
Expected: exit 0, `node_modules/` created (note: `bot/package-lock.json` will be created — commit it).

- [ ] **Step 1.5: Smoke-test Probot loads**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && npm ls probot`
Expected: `probot@14.3.2`. (Optional: `npx probot --version` — note it prints `0.0.0-dev` because of an upstream probot CLI packaging bug; that is NOT a failure. The library version is what matters.)

- [ ] **Step 1.6: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/package.json bot/package-lock.json .gitignore
git commit -m "feat(bot): scaffold probot app (v14, ESM)"
```

---

## Task 2: `config.js` — env config with defaults

**Files:**
- Create: `bot/test/config.test.js`
- Create: `bot/config.js`

- [ ] **Step 2.1: Write the failing test**

Create `bot/test/config.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { load, list } from '../config.js';

test('defaults', () => {
  const c = load({});
  assert.equal(c.triggerLabel, 'bot:fix');
  assert.equal(c.doneLabel, 'bot:done');
  assert.equal(c.failedLabel, 'bot:failed');
  assert.deepEqual(c.allowedAuthors, []);
  assert.equal(c.agentTimeoutMin, 30);
  assert.equal(c.branchPrefix, 'bot');
  assert.equal(c.permissionMode, 'bypassPermissions');
  assert.equal(c.claudeCmd, 'claude');
  assert.deepEqual(c.claudeArgs, []);
  assert.deepEqual(c.verifyCommands, []);
  assert.deepEqual(c.repos, []);
  assert.equal(c.dryRun, false);
  assert.equal(c.workdirRoot, path.resolve(path.join('..', 'data', 'bot-worktrees')));
  assert.equal(c.gitName, 'cc-mgr-bot[bot]');
  assert.equal(c.gitEmail, 'cc-mgr-bot[bot]@users.noreply.github.com');
  assert.deepEqual(c.claudeEnvExtra, []);
});

test('overrides and list parsing', () => {
  const c = load({
    TRIGGER_LABEL: 'x:fix',
    ALLOWED_AUTHORS: 'alice, bob ,',
    AGENT_TIMEOUT_MIN: '5',
    DRY_RUN: '1',
    CLAUDE_ARGS: '--max-turns 10',
    CLAUDE_ENV_EXTRA: 'FOO , BAR',
    REPOS: 'cshjin/cc-mgr',
    VERIFY_COMMANDS: '\necho ok\n npm test \n',
    WORKDIR_ROOT: '/tmp/w',
  });
  assert.equal(c.triggerLabel, 'x:fix');
  assert.deepEqual(c.allowedAuthors, ['alice', 'bob']);
  assert.equal(c.agentTimeoutMin, 5);
  assert.equal(c.dryRun, true);
  assert.deepEqual(c.claudeArgs, ['--max-turns', '10']);
  assert.deepEqual(c.claudeEnvExtra, ['FOO', 'BAR']);
  assert.deepEqual(c.repos, ['cshjin/cc-mgr']);
  assert.deepEqual(c.verifyCommands, ['echo ok', 'npm test']);
  assert.equal(c.workdirRoot, path.resolve('/tmp/w'));
});

test('agentTimeoutMin falls back to 30 for invalid values', () => {
  assert.equal(load({ AGENT_TIMEOUT_MIN: 'abc' }).agentTimeoutMin, 30);
  assert.equal(load({ AGENT_TIMEOUT_MIN: '0' }).agentTimeoutMin, 30);
  assert.equal(load({ AGENT_TIMEOUT_MIN: '-5' }).agentTimeoutMin, 30);
  assert.equal(load({ AGENT_TIMEOUT_MIN: '15' }).agentTimeoutMin, 15);
});

test('list helper', () => {
  assert.deepEqual(list(undefined), []);
  assert.deepEqual(list('  a, b ,,c '), ['a', 'b', 'c']);
});
```

- [ ] **Step 2.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/config.test.js`
Expected: FAIL — `Cannot find module '../config.js'` (exit non-zero).

- [ ] **Step 2.3: Implement `bot/config.js`**

```js
// Bot configuration, loaded from the environment with defaults.
// One frozen object is exported; tests pass a custom env map.
import path from 'node:path';

export function list(value) {
  return (value || '').split(',').map((s) => s.trim()).filter(Boolean);
}

export function load(env = process.env) {
  return Object.freeze({
    triggerLabel: env.TRIGGER_LABEL || 'bot:fix',
    doneLabel: env.DONE_LABEL || 'bot:done',
    failedLabel: env.FAILED_LABEL || 'bot:failed',
    // Empty = only the repo owner may trigger the bot.
    allowedAuthors: list(env.ALLOWED_AUTHORS),
    workdirRoot: path.resolve(env.WORKDIR_ROOT || path.join('..', 'data', 'bot-worktrees')),
    claudeCmd: env.CLAUDE_CMD || 'claude',
    claudeArgs: (env.CLAUDE_ARGS || '').split(/\s+/).filter(Boolean),
    permissionMode: env.CLAUDE_PERMISSION_MODE || 'bypassPermissions',
    claudeEnvExtra: list(env.CLAUDE_ENV_EXTRA),
    agentTimeoutMin: (() => {
      const n = Number(env.AGENT_TIMEOUT_MIN || 30);
      return Number.isFinite(n) && n > 0 ? n : 30;
    })(),
    verifyCommands: (env.VERIFY_COMMANDS || '').split('\n').map((s) => s.trim()).filter(Boolean),
    branchPrefix: env.BRANCH_PREFIX || 'bot',
    gitName: env.BOT_GIT_NAME || 'cc-mgr-bot[bot]',
    gitEmail: env.BOT_GIT_EMAIL || 'cc-mgr-bot[bot]@users.noreply.github.com',
    repos: list(env.REPOS), // "owner/repo" list for startup reconciliation
    dryRun: env.DRY_RUN === '1',
  });
}

export const defaultConfig = load();
```

- [ ] **Step 2.4: Run the test — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/config.test.js`
Expected: PASS — summary shows `# pass 4`, `# fail 0`.

- [ ] **Step 2.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/config.js bot/test/config.test.js
git commit -m "feat(bot): env config loader with defaults"
```

---

## Task 3: `queue.js` — in-memory single-worker queue

**Files:**
- Create: `bot/test/queue.test.js`
- Create: `bot/queue.js`

- [ ] **Step 3.1: Write the failing test**

Create `bot/test/queue.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Queue } from '../queue.js';

test('runs jobs in order, one at a time', async () => {
  const order = [];
  let active = 0;
  let maxActive = 0;
  const q = new Queue(async (job) => {
    active += 1;
    maxActive = Math.max(maxActive, active);
    await new Promise((r) => setTimeout(r, 10));
    order.push(job);
    active -= 1;
  });
  assert.equal(q.enqueue('a', 'a'), true);
  assert.equal(q.enqueue('b', 'b'), true);
  assert.equal(q.enqueue('c', 'c'), true);
  await new Promise((r) => setTimeout(r, 60));
  assert.deepEqual(order, ['a', 'b', 'c']);
  assert.equal(maxActive, 1);
});

test('dedups by id while pending and running', async () => {
  const q = new Queue(async () => {});
  assert.equal(q.enqueue('x', 1), true);
  assert.equal(q.enqueue('x', 2), false); // duplicate while running
  assert.equal(q.enqueue('y', 3), true);
  await new Promise((r) => setTimeout(r, 10));
});

test('emits error when the worker throws, keeps processing', async () => {
  const errors = [];
  const done = [];
  const q = new Queue(async (job) => {
    if (job === 'boom') throw new Error('boom');
    done.push(job);
  });
  q.on('error', (id, err) => errors.push([id, err.message]));
  q.enqueue('boom', 'boom');
  q.enqueue('ok', 'ok');
  await new Promise((r) => setTimeout(r, 30));
  assert.deepEqual(errors, [['boom', 'boom']]);
  assert.deepEqual(done, ['ok']);
});
```

- [ ] **Step 3.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/queue.test.js`
Expected: FAIL — `Cannot find module '../queue.js'`.

- [ ] **Step 3.3: Implement `bot/queue.js`**

```js
// In-memory FIFO job queue with a fixed concurrency (1 by default).
// Jobs are keyed by id; enqueueing the same id while it is pending or
// running is a no-op and returns false.
import { EventEmitter } from 'node:events';

export class Queue extends EventEmitter {
  constructor(worker, concurrency = 1) {
    super();
    this.worker = worker; // async (job) => {}
    this.concurrency = concurrency;
    this.pending = [];
    this.running = new Set();
  }

  enqueue(id, job) {
    if (this.running.has(id) || this.pending.some((j) => j.id === id)) return false;
    this.pending.push({ id, job });
    this._pump();
    return true;
  }

  _pump() {
    while (this.running.size < this.concurrency && this.pending.length > 0) {
      const { id, job } = this.pending.shift();
      this.running.add(id);
      Promise.resolve()
        .then(() => this.worker(job))
        .catch((err) => {
          if (this.listenerCount('error') > 0) {
            try { this.emit('error', id, err); } catch { /* listener threw — keep pumping */ }
          } else {
            console.error(`queue job ${id} failed:`, err);
          }
        })
        .finally(() => {
          this.running.delete(id);
          this._pump();
        });
    }
  }
}
```

- [ ] **Step 3.4: Run the test — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/queue.test.js`
Expected: PASS — `# pass 3`, `# fail 0`.

- [ ] **Step 3.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/queue.js bot/test/queue.test.js
git commit -m "feat(bot): in-memory single-worker queue with dedup"
```

---

## Task 4: `git.js` — clone/branch/commit/push on the bot's own clone

**Files:**
- Create: `bot/test/git.test.js`
- Create: `bot/git.js`

- [ ] **Step 4.1: Write the failing test**

Create `bot/test/git.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { cloneShallow, checkoutNewBranch, commitAll, hasDiff, changedFiles, push, pushUrl } from '../git.js';

function sh(cwd, ...args) {
  return execFileSync('git', args, { cwd, stdio: 'pipe' });
}

function setup() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-git-'));
  const seed = path.join(tmp, 'seed');
  fs.mkdirSync(seed);
  sh(seed, 'init');
  sh(seed, 'config', 'user.email', 'test@example.com');
  sh(seed, 'config', 'user.name', 'test');
  fs.writeFileSync(path.join(seed, 'README.md'), 'hello\n');
  fs.writeFileSync(path.join(seed, 'a.txt'), 'aaa\n');
  sh(seed, 'add', '-A');
  sh(seed, 'commit', '-m', 'init');
  const origin = path.join(tmp, 'origin.git');
  sh(seed, 'clone', '--bare', seed, origin);
  return { tmp, origin };
}

test('clone, branch, commit, diff, changedFiles', async () => {
  const { tmp, origin } = setup();
  const cloneDir = path.join(tmp, 'clone');
  await cloneShallow(origin, cloneDir);
  assert.equal(fs.existsSync(path.join(cloneDir, 'README.md')), true);

  await checkoutNewBranch(cloneDir, 'bot/issue-1-fix');
  assert.equal(sh(cloneDir, 'branch', '--show-current').toString().trim(), 'bot/issue-1-fix');

  assert.equal(await hasDiff(cloneDir), false);
  fs.writeFileSync(path.join(cloneDir, 'README.md'), 'hello world\n');
  fs.writeFileSync(path.join(cloneDir, 'new.txt'), 'new\n');
  sh(cloneDir, 'mv', 'a.txt', 'b.txt');
  assert.equal(await hasDiff(cloneDir), true);
  assert.deepEqual((await changedFiles(cloneDir)).sort(), ['README.md', 'b.txt', 'new.txt']);

  await commitAll(cloneDir, 'fix(issue-1): test', 'bot[bot]', 'bot@example.com');
  assert.equal(await hasDiff(cloneDir), false);
  assert.equal(
    sh(cloneDir, 'log', '-1', '--format=%an <%ae> %s').toString().trim(),
    'bot[bot] <bot@example.com> fix(issue-1): test'
  );
});

test('push to a local bare remote', async () => {
  const { tmp, origin } = setup();
  const cloneDir = path.join(tmp, 'clone');
  await cloneShallow(origin, cloneDir);
  await checkoutNewBranch(cloneDir, 'bot/issue-2-x');
  fs.writeFileSync(path.join(cloneDir, 'change.txt'), 'x\n');
  await commitAll(cloneDir, 'fix', 'bot[bot]', 'bot@example.com');
  const remote2 = path.join(tmp, 'remote2.git');
  sh(tmp, 'clone', '--bare', origin, remote2);
  await push(cloneDir, remote2, 'unused-token', 'bot/issue-2-x');
  const refs = sh(tmp, '--git-dir', remote2, 'for-each-ref', '--format=%(refname)').toString();
  assert.ok(refs.includes('refs/heads/bot/issue-2-x'));
});

test('pushUrl embeds token only for https remotes', () => {
  assert.equal(
    pushUrl('https://github.com/a/b.git', 'tok'),
    'https://x-access-token:tok@github.com/a/b.git'
  );
  assert.equal(pushUrl('/tmp/x.git', 'tok'), '/tmp/x.git');
});

test('push failure does not leak the token', async () => {
  const { tmp, origin } = setup();
  const cloneDir = path.join(tmp, 'clone');
  await cloneShallow(origin, cloneDir);
  await checkoutNewBranch(cloneDir, 'bot/issue-3-x');
  fs.writeFileSync(path.join(cloneDir, 'change.txt'), 'x\n');
  await commitAll(cloneDir, 'fix', 'bot[bot]', 'bot@example.com');
  // Unreachable https remote: push must reject, and neither the error
  // message nor stderr may contain the token.
  await assert.rejects(
    () => push(cloneDir, 'https://127.0.0.1:1/never.git', 'ghs_supersecrettoken', 'bot/issue-3-x'),
    (err) => {
      const text = `${err.message || ''} ${err.stderr || ''}`;
      assert.ok(!text.includes('ghs_supersecrettoken'));
      return true;
    }
  );
});

test('changedFiles handles unicode, quoted, and renamed filenames', async () => {
  const { tmp, origin } = setup();
  const cloneDir = path.join(tmp, 'clone');
  await cloneShallow(origin, cloneDir);
  fs.writeFileSync(path.join(cloneDir, 'café.txt'), 'x\n');
  fs.writeFileSync(path.join(cloneDir, 'b"q.txt'), 'x\n');
  sh(cloneDir, 'mv', 'a.txt', 'sp ace.txt');
  const files = await changedFiles(cloneDir);
  assert.ok(files.includes('café.txt'));
  assert.ok(files.includes('b"q.txt'));
  assert.ok(files.includes('sp ace.txt'));
});
```

- [ ] **Step 4.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/git.test.js`
Expected: FAIL — `Cannot find module '../git.js'`.

- [ ] **Step 4.3: Implement `bot/git.js`**

```js
// Git operations on the bot's own fresh clone. The clone carries no
// credentials; push() receives a one-shot token from the caller and embeds
// it only in that single push URL — it is never written to disk.
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const execFileAsync = promisify(execFile);
const MAX_BUFFER = 16 * 1024 * 1024;

function run(cwd, args) {
  return execFileAsync('git', args, { cwd, maxBuffer: MAX_BUFFER });
}

export async function cloneShallow(url, dir) {
  return run(undefined, ['clone', '--depth', '1', url, dir]);
}

export async function checkoutNewBranch(cwd, branch) {
  return run(cwd, ['checkout', '-b', branch]);
}

export async function commitAll(cwd, message, authorName, authorEmail) {
  await run(cwd, ['add', '-A']);
  return run(cwd, [
    '-c', `user.name=${authorName}`,
    '-c', `user.email=${authorEmail}`,
    'commit', '-m', message,
  ]);
}

export async function hasDiff(cwd) {
  const { stdout } = await run(cwd, ['status', '--porcelain']);
  return stdout.trim().length > 0;
}

// Changed files including untracked ones. Uses --porcelain -z (NUL-separated
// records, no C-style quoting) so non-ASCII/quoted filenames survive intact;
// rename records are "R  <dest>\0<src>\0" — the source path is skipped.
export async function changedFiles(cwd) {
  const { stdout } = await run(cwd, ['status', '--porcelain', '-z']);
  const files = [];
  const fields = stdout.split('\0');
  for (let i = 0; i < fields.length; i++) {
    const f = fields[i];
    if (f.length < 3 || f[2] !== ' ') continue; // not a record header
    files.push(f.slice(3));
    if (f[0] === 'R' || f[0] === 'C') i++; // skip the rename/copy source path
  }
  return files;
}

export function pushUrl(remoteUrl, token) {
  return remoteUrl.startsWith('https://')
    ? remoteUrl.replace('https://', `https://x-access-token:${token}@`)
    : remoteUrl;
}

// Pushes using the one-shot token embedded only in the push URL. On failure
// rethrows an error built from stderr only — execFile's default error
// message echoes the full command line, which would leak the token into logs.
export async function push(cwd, remoteUrl, token, branch) {
  try {
    return await run(cwd, ['push', pushUrl(remoteUrl, token), branch]);
  } catch (err) {
    const clean = new Error(String(err.stderr).slice(-2000) || 'git push failed');
    throw clean;
  }
}
```

- [ ] **Step 4.4: Run the test — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/git.test.js`
Expected: PASS — `# pass 5`, `# fail 0`.

- [ ] **Step 4.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/git.js bot/test/git.test.js
git commit -m "feat(bot): git helpers (clone/branch/commit/push with one-shot token)"
```

---

## Task 5: `github-helpers.js` — octokit wrappers

**Files:**
- Create: `bot/test/github-helpers.test.js`
- Create: `bot/github-helpers.js`

- [ ] **Step 5.1: Write the failing test**

Create `bot/test/github-helpers.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findBotPr, ensureLabel, removeLabel, deleteBranch, listComments, addComment, createPr, addLabels } from '../github-helpers.js';

const mkErr = (status) => {
  const err = new Error('nope');
  err.status = status;
  return err;
};

const paginate = async (fn, args) => (await fn(args)).data;

test('findBotPr matches the branch prefix for the issue', async () => {
  const octokit = {
    paginate,
    rest: {
      pulls: {
        list: async () => ({
          data: [
            { head: { ref: 'bot/issue-1-fix-typo' } },
            { head: { ref: 'bot/issue-10-other' } },
            { head: { ref: 'bot/issue-2-other' } },
            { head: { ref: 'someone/pr' } },
          ],
        }),
      },
    },
  };
  const pr = await findBotPr(octokit, { owner: 'o', repo: 'r', issueNumber: 1, branchPrefix: 'bot' });
  assert.equal(pr.head.ref, 'bot/issue-1-fix-typo');
});

test('findBotPr returns null when nothing matches', async () => {
  const octokit = { paginate, rest: { pulls: { list: async () => ({ data: [] }) } } };
  assert.equal(await findBotPr(octokit, { owner: 'o', repo: 'r', issueNumber: 3, branchPrefix: 'bot' }), null);
});

test('ensureLabel creates the label when missing (404)', async () => {
  const created = [];
  const octokit = {
    rest: {
      issues: {
        getLabel: async () => { throw mkErr(404); },
        createLabel: async (args) => { created.push(args); },
      },
    },
  };
  await ensureLabel(octokit, { owner: 'o', repo: 'r', name: 'bot:done' });
  assert.equal(created.length, 1);
  assert.equal(created[0].name, 'bot:done');
});

test('ensureLabel passes through unexpected errors', async () => {
  const octokit = { rest: { issues: { getLabel: async () => { throw mkErr(500); } } } };
  await assert.rejects(() => ensureLabel(octokit, { owner: 'o', repo: 'r', name: 'x' }), /nope/);
});

test('removeLabel and deleteBranch swallow the expected missing errors', async () => {
  await removeLabel(
    { rest: { issues: { removeLabel: async () => { throw mkErr(404); } } } },
    { owner: 'o', repo: 'r', issueNumber: 1, name: 'x' }
  );
  await deleteBranch(
    { rest: { git: { deleteRef: async () => { throw mkErr(422); } } } },
    { owner: 'o', repo: 'r', branch: 'b' }
  );
  await assert.rejects(
    () => removeLabel(
      { rest: { issues: { removeLabel: async () => { throw mkErr(500); } } } },
      { owner: 'o', repo: 'r', issueNumber: 1, name: 'x' }
    ),
    /nope/
  );
});

test('listComments maps to {user, body}, null user becomes ghost', async () => {
  const octokit = {
    rest: {
      issues: {
        listComments: async () => ({
          data: [
            { user: { login: 'alice' }, body: 'hi' },
            { user: { login: 'bob' }, body: null },
            { user: null, body: 'deleted account' },
          ],
        }),
      },
    },
  };
  assert.deepEqual(await listComments(octokit, { owner: 'o', repo: 'r', issueNumber: 1 }), [
    { user: 'alice', body: 'hi' },
    { user: 'bob', body: '' },
    { user: 'ghost', body: 'deleted account' },
  ]);
});

test('pass-through helpers send the exact API payloads', async () => {
  const calls = [];
  const octokit = {
    rest: {
      issues: {
        createComment: async (args) => { calls.push(['createComment', args]); },
        addLabels: async (args) => { calls.push(['addLabels', args]); },
      },
      pulls: {
        create: async (args) => {
          calls.push(['create', args]);
          return { data: { html_url: 'https://example/pr' } };
        },
      },
    },
  };
  await addComment(octokit, { owner: 'o', repo: 'r', issueNumber: 1, body: 'hi' });
  await createPr(octokit, { owner: 'o', repo: 'r', title: 't', head: 'h', base: 'main', body: 'b' });
  await addLabels(octokit, { owner: 'o', repo: 'r', issueNumber: 1, labels: ['bot:done'] });
  assert.deepEqual(calls[0], ['createComment', { owner: 'o', repo: 'r', issue_number: 1, body: 'hi' }]);
  assert.deepEqual(calls[1], ['create', { owner: 'o', repo: 'r', title: 't', head: 'h', base: 'main', body: 'b' }]);
  assert.deepEqual(calls[2], ['addLabels', { owner: 'o', repo: 'r', issue_number: 1, labels: ['bot:done'] }]);
});
```

- [ ] **Step 5.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/github-helpers.test.js`
Expected: FAIL — `Cannot find module '../github-helpers.js'`.

- [ ] **Step 5.3: Implement `bot/github-helpers.js`**

```js
// Thin octokit wrappers for everything the bot does on GitHub, kept free of
// Probot so the runner and handlers stay testable with fakes.

// The open PR by the bot for this issue, identified by branch-name
// convention (branch = `${branchPrefix}/issue-<n>-<slug>`). Uses paginate so
// repos with >100 open PRs cannot hide the match on a later page.
export async function findBotPr(octokit, { owner, repo, issueNumber, branchPrefix }) {
  const data = await octokit.paginate(octokit.rest.pulls.list, { owner, repo, state: 'open', per_page: 100 });
  const prefix = `${branchPrefix}/issue-${issueNumber}-`;
  return data.find((pr) => pr.head.ref.startsWith(prefix)) || null;
}

export async function listComments(octokit, { owner, repo, issueNumber }) {
  const { data } = await octokit.rest.issues.listComments({ owner, repo, issue_number: issueNumber, per_page: 100 });
  return data.map((c) => ({ user: c.user?.login || 'ghost', body: c.body || '' }));
}

export async function addComment(octokit, { owner, repo, issueNumber, body }) {
  return octokit.rest.issues.createComment({ owner, repo, issue_number: issueNumber, body });
}

export async function createPr(octokit, { owner, repo, title, head, base, body }) {
  const { data } = await octokit.rest.pulls.create({ owner, repo, title, head, base, body });
  return data;
}

// Creates the label if it does not exist yet.
export async function ensureLabel(octokit, { owner, repo, name }) {
  try {
    await octokit.rest.issues.getLabel({ owner, repo, name });
  } catch (err) {
    if (err.status !== 404) throw err;
    await octokit.rest.issues.createLabel({ owner, repo, name, color: 'bfd4f2' });
  }
}

export async function addLabels(octokit, { owner, repo, issueNumber, labels }) {
  return octokit.rest.issues.addLabels({ owner, repo, issue_number: issueNumber, labels });
}

export async function removeLabel(octokit, { owner, repo, issueNumber, name }) {
  try {
    await octokit.rest.issues.removeLabel({ owner, repo, issue_number: issueNumber, name });
  } catch (err) {
    if (err.status !== 404) throw err; // label already gone is fine
  }
}

export async function deleteBranch(octokit, { owner, repo, branch }) {
  try {
    await octokit.rest.git.deleteRef({ owner, repo, ref: `heads/${branch}` });
  } catch (err) {
    if (err.status !== 422) throw err; // 422 = branch does not exist
  }
}
```

- [ ] **Step 5.4: Run the test — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/github-helpers.test.js`
Expected: PASS — `# pass 7`, `# fail 0`.

- [ ] **Step 5.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/github-helpers.js bot/test/github-helpers.test.js
git commit -m "feat(bot): octokit wrappers (dedup, comments, PR, labels, branches)"
```

---

## Task 6: `runner.js` part 1 — pure helpers (slug, prompt, parse, env)

**Files:**
- Create: `bot/test/runner.test.js` (part 1 content; Task 7 appends the integration tests)
- Create: `bot/runner.js` (part 1 content; Task 7 appends the rest)

- [ ] **Step 6.1: Write the failing tests for the pure helpers**

Create `bot/test/runner.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { slugify, buildPrompt, parseResult, claudeEnv } from '../runner.js';
import { load } from '../config.js';

test('slugify', () => {
  assert.equal(slugify('Fix the README typo!'), 'fix-the-readme-typo');
  assert.equal(slugify(''), 'fix');
  assert.equal(slugify('!!!'), 'fix');
  assert.equal(slugify('a'.repeat(100)), 'a'.repeat(40));
});

test('buildPrompt includes issue content and constraints', () => {
  const prompt = buildPrompt({ issueNumber: 7, title: 'T', body: 'B', author: 'alice', comments: [{ user: 'bob', body: 'C1' }] });
  assert.ok(prompt.includes('ISSUE #7: T'));
  assert.ok(prompt.includes('Author: @alice'));
  assert.ok(prompt.includes('B'));
  assert.ok(prompt.includes('@bob: C1'));
  assert.ok(prompt.includes('Do not run git commands'));
  const noComments = buildPrompt({ issueNumber: 1, title: 'T', body: '', author: 'a', comments: [] });
  assert.ok(noComments.includes('(none)'));
  assert.ok(noComments.includes('(no body)'));
});

test('parseResult handles json, error flag and raw text', () => {
  assert.deepEqual(
    parseResult('{"type":"result","result":"did the thing"}'),
    { summary: 'did the thing', isError: false }
  );
  assert.deepEqual(parseResult('{"result":"oops","is_error":true}'), { summary: 'oops', isError: true });
  assert.deepEqual(parseResult('not json at all'), { summary: 'not json at all', isError: false });
  assert.deepEqual(parseResult(''), { summary: '', isError: false });
});

test('claudeEnv passes ANTHROPIC_* and basics, drops the rest', () => {
  process.env.ANTHROPIC_BASE_URL = 'https://gw.example';
  process.env.ANTHROPIC_AUTH_TOKEN = 'sekret';
  process.env.WEBHOOK_SECRET = 'webhook-sekret';
  process.env.MY_GATEWAY_VAR = 'extra';
  try {
    const env = claudeEnv(load({ CLAUDE_ENV_EXTRA: 'MY_GATEWAY_VAR' }));
    assert.equal(env.ANTHROPIC_BASE_URL, 'https://gw.example');
    assert.equal(env.ANTHROPIC_AUTH_TOKEN, 'sekret');
    assert.equal(env.MY_GATEWAY_VAR, 'extra');
    assert.equal(env.PATH, process.env.PATH);
    assert.equal(env.WEBHOOK_SECRET, undefined);
  } finally {
    for (const k of ['ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'WEBHOOK_SECRET', 'MY_GATEWAY_VAR']) {
      delete process.env[k];
    }
  }
});
```

- [ ] **Step 6.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/runner.test.js`
Expected: FAIL — `Cannot find module '../runner.js'`.

- [ ] **Step 6.3: Implement `bot/runner.js` (part 1)**

```js
// The agentic job: clone → run headless claude → verify → commit → push →
// PR → issue comment. Every GitHub action goes through github-helpers, and
// DRY_RUN turns external side effects into log lines, so runJob is testable
// with fakes and a local git repo. (Task 7 appends verifyChanges/runJob.)

export function slugify(text, maxLen = 40) {
  const slug = String(text || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, maxLen)
    .replace(/-+$/g, '');
  return slug || 'fix';
}

export function buildPrompt({ issueNumber, title, body, author, comments }) {
  const commentText = comments.length
    ? comments.map((c) => `@${c.user}: ${c.body}`).join('\n\n---\n\n')
    : '(none)';
  return `You are an autonomous coding agent fixing a GitHub issue. The repository is checked out in your working directory.

ISSUE #${issueNumber}: ${title}
Author: @${author}

${body || '(no body)'}

EXISTING COMMENTS:
${commentText}

TASK: Fix the issue described above. Rules:
- Make the smallest change that resolves the issue. Do not refactor unrelated code.
- Follow the repository's own conventions; read CLAUDE.md and related docs if present.
- Do not run git commands, do not commit, do not push, do not create PRs — the orchestrator handles all git.
- Do not modify anything outside the repository directory.
- If the issue cannot be fixed, explain clearly why.
- End your reply with a summary of exactly what you changed and why.`;
}

// Parses `claude -p --output-format json` stdout into { summary, isError }.
// Falls back to raw text when stdout is not JSON.
export function parseResult(stdout) {
  const raw = (stdout || '').trim();
  if (!raw) return { summary: '', isError: false };
  try {
    const parsed = JSON.parse(raw);
    const text = typeof parsed.result === 'string'
      ? parsed.result.trim()
      : (typeof parsed.message === 'string' ? parsed.message.trim() : raw);
    return { summary: text, isError: parsed.is_error === true };
  } catch {
    return { summary: raw, isError: false };
  }
}

// Minimal environment for the claude child: only what the CLI needs. The
// bot's own GitHub credentials are never passed through.
export function claudeEnv(config) {
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    LANG: process.env.LANG || 'C.UTF-8',
  };
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith('ANTHROPIC_')) env[key] = value;
  }
  for (const name of config.claudeEnvExtra) {
    if (process.env[name] !== undefined) env[name] = process.env[name];
  }
  return env;
}
```

- [ ] **Step 6.4: Run the test — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/runner.test.js`
Expected: PASS — `# pass 4`, `# fail 0`.

- [ ] **Step 6.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/runner.js bot/test/runner.test.js
git commit -m "feat(bot): runner pure helpers (slug, prompt, json parse, child env)"
```

---

## Task 7: `runner.js` part 2 — runClaude, verify, runJob

**Files:**
- Modify: `bot/runner.js` (append the rest)
- Modify: `bot/test/runner.test.js` (append integration tests)

- [ ] **Step 7.1: Append the failing integration tests to `bot/test/runner.test.js`**

Replace the imports at the top of `bot/test/runner.test.js` (line 3) with:

```js
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { slugify, buildPrompt, parseResult, claudeEnv, verifyChanges, runClaude, runJob } from '../runner.js';
import { hasDiff } from '../git.js';
import { load } from '../config.js';
```

Then append these tests to the file:

```js
function sh(cwd, ...args) {
  return execFileSync('git', args, { cwd, stdio: 'pipe' });
}

// Seeds a local bare repo the runner can clone, returns { tmp, origin }.
function setupRepo() {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-runjob-'));
  const seed = path.join(tmp, 'seed');
  fs.mkdirSync(seed);
  sh(seed, 'init');
  sh(seed, 'config', 'user.email', 't@example.com');
  sh(seed, 'config', 'user.name', 't');
  fs.writeFileSync(path.join(seed, 'README.md'), 'hello\n');
  sh(seed, 'add', '-A');
  sh(seed, 'commit', '-m', 'init');
  const origin = path.join(tmp, 'origin.git');
  sh(seed, 'clone', '--bare', seed, origin);
  return { tmp, origin };
}

// Writes an executable fake `claude` script; runJob will exec it as the
// agent (its cwd is the fresh clone).
function fakeClaude(tmp, body) {
  const script = path.join(tmp, 'fake-claude.sh');
  fs.writeFileSync(script, `#!/bin/sh\n${body}\n`);
  fs.chmodSync(script, 0o755);
  return script;
}

function dryConfig(tmp, claudeCmd) {
  return load({ DRY_RUN: '1', WORKDIR_ROOT: path.join(tmp, 'work'), CLAUDE_CMD: claudeCmd, AGENT_TIMEOUT_MIN: '1' });
}

// Octokit fake covering everything runJob touches in dry-run mode.
function dryOctokit() {
  return {
    paginate: async (fn, args) => (await fn(args)).data,
    rest: {
      pulls: { list: async () => ({ data: [] }) },
      issues: { listComments: async () => ({ data: [] }) },
    },
  };
}

function job(origin) {
  return {
    owner: 'o', repo: 'r', issueNumber: 1, title: 'Typo in README',
    body: 'fix it', author: 'alice', cloneUrl: origin,
    defaultBranch: 'master', installationToken: 'tok',
  };
}

test('runJob end-to-end with a fake claude (dry run)', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, "echo fixed >> README.md\necho '{\"result\": \"Fixed the README typo.\"}'");
  const config = dryConfig(tmp, claude);
  const calls = [];
  const result = await runJob({ job: job(origin), config, octokit: dryOctokit(), log: (m) => calls.push(m) });

  assert.equal(result.status, 'done');
  assert.ok(result.summary.includes('Fixed the README typo'));
  assert.ok(calls.some((m) => m.includes('DRY_RUN: would open PR')));

  const workdir = path.join(tmp, 'work', fs.readdirSync(path.join(tmp, 'work'))[0]);
  assert.ok(fs.readFileSync(path.join(workdir, 'README.md'), 'utf8').includes('fixed'));
  assert.equal(sh(workdir, 'branch', '--show-current').toString().trim(), 'bot/issue-1-typo-in-readme');
  assert.equal(await hasDiff(workdir), false); // committed on the branch
});

test('runJob fails cleanly when the agent reports is_error', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, "echo '{\"result\": \"cannot fix\", \"is_error\": true}'");
  const config = dryConfig(tmp, claude);
  const result = await runJob({ job: job(origin), config, octokit: dryOctokit(), log: () => {} });
  assert.equal(result.status, 'failed');
  assert.ok(result.error.includes('Agent reported an error'));
});

test('runJob fails cleanly when the agent makes no changes', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, "echo '{\"result\": \"looks fine already\"}'");
  const config = dryConfig(tmp, claude);
  const result = await runJob({ job: job(origin), config, octokit: dryOctokit(), log: () => {} });
  assert.equal(result.status, 'failed');
  assert.ok(result.error.includes('No changes were made'));
});

test('runJob fails cleanly when claude exits non-zero', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, 'echo nope >&2\nexit 1');
  const config = dryConfig(tmp, claude);
  const result = await runJob({ job: job(origin), config, octokit: dryOctokit(), log: () => {} });
  assert.equal(result.status, 'failed');
  assert.ok(result.error.includes('Agent run failed'));
});

test('runJob skips when a bot PR already exists', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, 'exit 0');
  const config = dryConfig(tmp, claude);
  const octokit = dryOctokit();
  octokit.rest.pulls.list = async () => ({ data: [{ head: { ref: 'bot/issue-1-x' } }] });
  const result = await runJob({ job: job(origin), config, octokit, log: () => {} });
  assert.equal(result.status, 'skipped');
});

test('verifyChanges catches bad syntax and runs config commands', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-verify-'));
  fs.writeFileSync(path.join(dir, 'bad.js'), 'function ( {\n');
  fs.writeFileSync(path.join(dir, 'ok.js'), 'console.log(1);\n');
  const failures = await verifyChanges(load({ VERIFY_COMMANDS: 'exit 3' }), dir, ['bad.js', 'ok.js', 'notes.txt']);
  assert.equal(failures.length, 2);
  assert.ok(failures[0].includes('bad.js'));
  assert.ok(failures[1].includes('exit 3'));
});

test('runClaude times out and reports the failure', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-timeout-'));
  const slow = fakeClaude(tmp, 'sleep 5');
  const config = load({ CLAUDE_CMD: slow, AGENT_TIMEOUT_MIN: '0.0166', WORKDIR_ROOT: tmp });
  await assert.rejects(
    () => runClaude(config, 'p', tmp, path.join(tmp, 'claude.log')),
    /timed out/
  );
});
```

- [ ] **Step 7.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/runner.test.js`
Expected: FAIL — `verifyChanges`/`runClaude`/`runJob` are not exported (`SyntaxError: The requested module '../runner.js' does not provide an export named 'verifyChanges'`), or `TypeError` for the missing functions.

- [ ] **Step 7.3: Append the implementation to `bot/runner.js`**

Append to the end of `bot/runner.js`:

```js
import fs from 'node:fs';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import * as git from './git.js';
import * as gh from './github-helpers.js';

const execFileAsync = promisify(execFile);

// Runs `claude -p <prompt> --output-format json` headless. Enforces the
// timeout itself (the CLI has no wall-clock timeout flag). Resolves with
// stdout; stderr is written to logPath.
export function runClaude(config, prompt, cwd, logPath) {
  const args = ['-p', prompt, '--output-format', 'json', '--permission-mode', config.permissionMode, ...config.claudeArgs];
  return new Promise((resolve, reject) => {
    const child = execFile(config.claudeCmd, args, { cwd, env: claudeEnv(config) });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGTERM');
    }, config.agentTimeoutMin * 60 * 1000);
    const stdoutChunks = [];
    const stderrChunks = [];
    child.stdout.on('data', (chunk) => stdoutChunks.push(chunk));
    child.stderr.on('data', (chunk) => stderrChunks.push(chunk));
    child.on('error', (err) => {
      clearTimeout(timer);
      reject(err);
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      const stdout = Buffer.concat(stdoutChunks).toString('utf8');
      const stderr = Buffer.concat(stderrChunks).toString('utf8');
      fs.writeFileSync(logPath, stderr, 'utf8');
      if (code === 0) return resolve(stdout);
      const err = new Error(timedOut
        ? `claude timed out after ${config.agentTimeoutMin} min`
        : `claude exited with code ${code}`);
      err.stdout = stdout;
      err.stderr = stderr;
      err.timedOut = timedOut;
      reject(err);
    });
  });
}

async function runCheck(cwd, bin, args) {
  try {
    await execFileAsync(bin, args, { cwd });
  } catch (err) {
    err.stderr = String(err.stderr || err.message || '').slice(-2000);
    throw err;
  }
}

// Static checks per changed file type, plus free-form commands from config.
// Returns the list of failure strings (empty = all good).
export async function verifyChanges(config, cwd, files) {
  const failures = [];
  for (const file of files) {
    try {
      if (file.endsWith('.js')) await runCheck(cwd, 'node', ['--check', file]);
      else if (file.endsWith('.py')) await runCheck(cwd, 'python', ['-m', 'py_compile', file]);
    } catch (err) {
      failures.push(`${file}:\n${err.stderr}`);
    }
  }
  for (const cmd of config.verifyCommands) {
    try {
      await runCheck(cwd, '/bin/sh', ['-c', cmd]);
    } catch (err) {
      failures.push(`${cmd}:\n${err.stderr}`);
    }
  }
  return failures;
}

function readTail(filePath, lines) {
  try {
    return fs.readFileSync(filePath, 'utf8').split('\n').slice(-lines).join('\n');
  } catch {
    return '';
  }
}

async function fail(job, config, octokit, logPath, reason, log) {
  const tail = readTail(logPath, 30);
  const body = `❌ ${reason}${tail ? `\n\n<details><summary>Log tail</summary>\n\n\`\`\`\n${tail}\n\`\`\`\n</details>` : ''}`;
  if (config.dryRun) {
    log(`DRY_RUN: would comment on #${job.issueNumber}: ${body}`);
  } else {
    await gh.ensureLabel(octokit, { owner: job.owner, repo: job.repo, name: config.failedLabel });
    await gh.addLabels(octokit, { owner: job.owner, repo: job.repo, issueNumber: job.issueNumber, labels: [config.failedLabel] });
    await gh.addComment(octokit, { owner: job.owner, repo: job.repo, issueNumber: job.issueNumber, body });
  }
  return { status: 'failed', error: reason };
}

// The whole agentic job. job = { owner, repo, issueNumber, title, body,
// author, cloneUrl, defaultBranch, installationToken }.
export async function runJob({ job, config, octokit, log = () => {} }) {
  const { owner, repo, issueNumber, title, body, author, cloneUrl, defaultBranch, installationToken } = job;
  const branch = `${config.branchPrefix}/issue-${issueNumber}-${slugify(title)}`;

  // Authoritative dedup — an open bot PR for this issue means it is handled.
  if (await gh.findBotPr(octokit, { owner, repo, issueNumber, branchPrefix: config.branchPrefix })) {
    log(`#${issueNumber}: open PR already exists, skipping`);
    return { status: 'skipped' };
  }

  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const workdir = path.join(config.workdirRoot, `issue-${issueNumber}-${ts}`);
  const logPath = `${workdir}.log`;
  fs.mkdirSync(config.workdirRoot, { recursive: true });

  log(`#${issueNumber}: cloning ${cloneUrl}`);
  await git.cloneShallow(cloneUrl, workdir);
  await git.checkoutNewBranch(workdir, branch);

  const comments = await gh.listComments(octokit, { owner, repo, issueNumber });
  const prompt = buildPrompt({ issueNumber, title, body, author, comments });

  log(`#${issueNumber}: running ${config.claudeCmd}`);
  let stdout;
  try {
    stdout = await runClaude(config, prompt, workdir, logPath);
  } catch (err) {
    return fail(job, config, octokit, logPath, `Agent run failed: ${err.message}`, log);
  }
  const { summary, isError } = parseResult(stdout);
  if (isError) {
    return fail(job, config, octokit, logPath, `Agent reported an error:\n\n${summary || '(no details)'}`, log);
  }

  if (!(await git.hasDiff(workdir))) {
    return fail(job, config, octokit, logPath, `No changes were made.\n\nAgent summary:\n\n${summary || '(empty)'}`, log);
  }

  const files = await git.changedFiles(workdir);
  const verifyFailures = await verifyChanges(config, workdir, files);
  if (verifyFailures.length > 0) {
    return fail(job, config, octokit, logPath, `Verification failed:\n\n${verifyFailures.join('\n\n')}`, log);
  }

  const commitMessage = `fix(issue-${issueNumber}): ${title}`;
  await git.commitAll(workdir, commitMessage, config.gitName, config.gitEmail);

  if (config.dryRun) {
    log(`DRY_RUN: would push ${branch}`);
    log(`DRY_RUN: would open PR "Fix #${issueNumber}: ${title}"`);
    log(`DRY_RUN: would comment on #${issueNumber} with the summary`);
    return { status: 'done', summary, dryRun: true };
  }

  try {
    await git.push(workdir, cloneUrl, installationToken, branch);
  } catch (pushErr) {
    log(`#${issueNumber}: push failed (${pushErr.message}), deleting stale branch and retrying once`);
    await gh.deleteBranch(octokit, { owner, repo, branch });
    await git.push(workdir, cloneUrl, installationToken, branch);
  }

  const pr = await gh.createPr(octokit, {
    owner, repo,
    title: `Fix #${issueNumber}: ${title}`,
    head: branch, base: defaultBranch,
    body: `${summary || '(no summary)'}\n\nCloses #${issueNumber}`,
  });

  await gh.ensureLabel(octokit, { owner, repo, name: config.doneLabel });
  await gh.addLabels(octokit, { owner, repo, issueNumber, labels: [config.doneLabel] });
  await gh.removeLabel(octokit, { owner, repo, issueNumber, name: config.triggerLabel });
  await gh.addComment(octokit, { owner, repo, issueNumber, body: `Fixed in PR: ${pr.html_url}\n\n${summary || ''}` });

  log(`#${issueNumber}: PR ${pr.html_url}`);
  return { status: 'done', summary, prUrl: pr.html_url };
}
```

Note: ESM allows `import` statements only at the top of the file. **Move the six `import` lines above into the top of `bot/runner.js`** (after the existing first comment line), instead of appending them at the bottom — everything else appends fine. The final file must start with the comment, then the six imports, then the part-1 code.

- [ ] **Step 7.4: Run the tests — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/runner.test.js`
Expected: PASS — `# pass 11`, `# fail 0` (4 part-1 tests + 7 new). The timeout test takes ~1s.

- [ ] **Step 7.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/runner.js bot/test/runner.test.js
git commit -m "feat(bot): runner agentic job (claude subprocess, verify, commit, PR)"
```

---

## Task 8: `index.js` — Probot wiring, handlers, reconciliation

**Files:**
- Create: `bot/test/index.test.js`
- Create: `bot/index.js`

- [ ] **Step 8.1: Write the failing test**

Create `bot/test/index.test.js`:

```js
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHandlers, reconcile } from '../index.js';
import { load } from '../config.js';

function spyQueue() {
  const enqueued = [];
  return {
    enqueued,
    enqueue(id, job) {
      enqueued.push({ id, job });
      return true;
    },
  };
}

function fakeOctokit() {
  const comments = [];
  return {
    comments,
    rest: {
      apps: { createInstallationAccessToken: async () => ({ data: { token: 'tok' } }) },
      issues: { createComment: async (args) => { comments.push(args.body); } },
    },
  };
}

function payload(overrides = {}) {
  return {
    installation: { id: 1 },
    repository: {
      owner: { login: 'cshjin' },
      name: 'cc-mgr',
      clone_url: 'https://github.com/cshjin/cc-mgr.git',
      default_branch: 'main',
    },
    issue: {
      number: 1,
      title: 'typo',
      body: 'fix',
      user: { login: 'cshjin' },
      labels: [{ name: 'bot:fix' }],
    },
    ...overrides,
  };
}

async function fire(over = {}, env = {}) {
  const queue = spyQueue();
  const octokit = fakeOctokit();
  const logs = [];
  const handlers = createHandlers({ config: load(env), queue, log: (m) => logs.push(m) });
  await handlers.handleIssueEvent({ payload: payload(over), octokit, appOctokit: octokit });
  return { queue, octokit, logs };
}

test('enqueues labeled issues and posts a started comment', async () => {
  const { queue, octokit } = await fire();
  assert.equal(queue.enqueued.length, 1);
  assert.equal(queue.enqueued[0].id, 'cshjin/cc-mgr#1');
  assert.equal(octokit.comments.length, 1);
  assert.ok(octokit.comments[0].includes('Working on this'));
});

test('ignores PRs, missing label, and disallowed authors', async () => {
  const prIssue = { ...payload().issue, pull_request: {} };
  const unlabeled = { ...payload().issue, labels: [] };
  const stranger = { ...payload().issue, user: { login: 'mallory' } };
  for (const issue of [prIssue, unlabeled, stranger]) {
    const { queue, octokit } = await fire({ issue });
    assert.equal(queue.enqueued.length, 0);
    assert.equal(octokit.comments.length, 0);
  }
});

test('ALLOWED_AUTHORS can widen the allowlist', async () => {
  const { queue } = await fire(
    { issue: { ...payload().issue, user: { login: 'mallory' } } },
    { ALLOWED_AUTHORS: 'mallory' }
  );
  assert.equal(queue.enqueued.length, 1);
});

test('dry run enqueues but does not comment', async () => {
  const { queue, octokit } = await fire({}, { DRY_RUN: '1' });
  assert.equal(queue.enqueued.length, 1);
  assert.equal(octokit.comments.length, 0);
});

test('reconcile enqueues labeled issues without a bot PR', async () => {
  const queue = spyQueue();
  const logs = [];
  const appOctokit = {
    rest: {
      repos: { get: async () => ({ data: { clone_url: 'https://github.com/o/r.git', default_branch: 'main' } }) },
      apps: {
        getRepoInstallation: async () => ({ data: { id: 9 } }),
        createInstallationAccessToken: async () => ({ data: { token: 'tok' } }),
      },
    },
  };
  const octokit = {
    rest: {
      issues: {
        listForRepo: async () => ({
          data: [
            { number: 1, title: 't1', body: '', user: { login: 'o' } },
            { number: 2, title: 't2', body: '', user: { login: 'o' }, pull_request: {} },
          ],
        }),
      },
    },
  };
  await reconcile({
    config: load({ REPOS: 'o/r' }),
    queue,
    log: (m) => logs.push(m),
    appOctokit,
    getInstallationOctokit: async () => octokit,
  });
  assert.equal(queue.enqueued.length, 1);
  assert.equal(queue.enqueued[0].id, 'o/r#1');
});

test('reconcile logs and continues when a repo fails', async () => {
  const queue = spyQueue();
  const logs = [];
  const appOctokit = {
    rest: {
      repos: { get: async () => { throw new Error('boom'); } },
    },
  };
  await reconcile({
    config: load({ REPOS: 'o/r' }),
    queue,
    log: (m) => logs.push(m),
    appOctokit,
    getInstallationOctokit: async () => ({}),
  });
  assert.equal(queue.enqueued.length, 0);
  assert.ok(logs.some((m) => m.includes('reconcile: o/r: boom')));
});
```

- [ ] **Step 8.2: Run it — expect failure**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/index.test.js`
Expected: FAIL — `Cannot find module '../index.js'`.

- [ ] **Step 8.3: Implement `bot/index.js`**

```js
// Probot app entry: webhook → checks → enqueue. The agentic work lives in
// runner.js; the handlers are factored out so they are testable without a
// running Probot server.
import { Queue } from './queue.js';
import { runJob } from './runner.js';
import * as gh from './github-helpers.js';
import { defaultConfig as config } from './config.js';

export function createHandlers({ config, queue, log = () => {} }) {
  async function handleIssueEvent({ payload, octokit, appOctokit }) {
    const issue = payload.issue;
    if (issue.pull_request) return; // PRs fire issue events too
    const labels = (issue.labels || []).map((l) => l.name);
    if (!labels.includes(config.triggerLabel)) return;

    const allowed = config.allowedAuthors.length > 0
      ? config.allowedAuthors
      : [payload.repository.owner.login];
    if (!allowed.includes(issue.user.login)) {
      log(`ignoring #${issue.number}: author @${issue.user.login} not in ALLOWED_AUTHORS`);
      return;
    }

    // One-shot push token from the app-level octokit; the agent never sees it.
    const { data } = await appOctokit.rest.apps.createInstallationAccessToken({
      installation_id: payload.installation.id,
    });
    const job = {
      owner: payload.repository.owner.login,
      repo: payload.repository.name,
      issueNumber: issue.number,
      title: issue.title,
      body: issue.body || '',
      author: issue.user.login,
      cloneUrl: payload.repository.clone_url,
      defaultBranch: payload.repository.default_branch,
      installationToken: data.token,
    };

    const id = `${job.owner}/${job.repo}#${job.issueNumber}`;
    if (!queue.enqueue(id, () => runJob({ job, config, octokit, log }))) return;

    log(`enqueued ${id}`);
    if (!config.dryRun) {
      await gh.addComment(octokit, { ...job, body: "👷 Working on this — I'll open a PR with the fix shortly." });
    }
  }

  return { handleIssueEvent };
}

// Startup reconciliation: re-enqueue trigger-labeled issues left over from a
// crash (only repos listed in REPOS). The runner's own dedup skips anything
// that already has an open bot PR.
export async function reconcile({ config, queue, log = () => {}, appOctokit, getInstallationOctokit }) {
  for (const repoSpec of config.repos) {
    const [owner, repo] = repoSpec.split('/');
    try {
      const { data: repoData } = await appOctokit.rest.repos.get({ owner, repo });
      const { data: installation } = await appOctokit.rest.apps.getRepoInstallation({ owner, repo });
      const octokit = await getInstallationOctokit(installation.id);
      const { data: issues } = await octokit.rest.issues.listForRepo({
        owner, repo, state: 'open', labels: config.triggerLabel, per_page: 100,
      });
      for (const issue of issues) {
        if (issue.pull_request) continue;
        const { data } = await appOctokit.rest.apps.createInstallationAccessToken({ installation_id: installation.id });
        const job = {
          owner, repo, issueNumber: issue.number, title: issue.title, body: issue.body || '',
          author: issue.user.login, cloneUrl: repoData.clone_url, defaultBranch: repoData.default_branch,
          installationToken: data.token,
        };
        const id = `${owner}/${repo}#${issue.number}`;
        if (!queue.enqueue(id, () => runJob({ job, config, octokit, log }))) continue;
        log(`reconcile: enqueued ${id}`);
      }
    } catch (err) {
      log(`reconcile: ${repoSpec}: ${err.message}`);
    }
  }
}

export default (app) => {
  const queue = new Queue(async (job) => job());
  queue.on('error', (id, err) => app.log.error(`job ${id}: ${err.stack || err}`));
  const handlers = createHandlers({ config, queue, log: (m) => app.log.info(m) });

  app.on(['issues.opened', 'issues.labeled'], async (context) => {
    const appOctokit = await app.auth(); // app-level (JWT) octokit
    await handlers.handleIssueEvent({
      payload: context.payload,
      octokit: context.octokit, // installation-bound
      appOctokit,
    });
  });

  // Fire-and-forget startup reconciliation, deferred until the server is up.
  setTimeout(() => {
    (async () => {
      const appOctokit = await app.auth();
      await reconcile({
        config,
        queue,
        log: (m) => app.log.info(m),
        appOctokit,
        getInstallationOctokit: (id) => app.auth(id),
      });
    })().catch((err) => app.log.error(`reconcile: ${err.stack || err}`));
  }, 10 * 1000);
};
```

- [ ] **Step 8.4: Run the tests — expect pass**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && node --test test/index.test.js`
Expected: PASS — `# pass 6`, `# fail 0`.

- [ ] **Step 8.5: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/index.js bot/test/index.test.js
git commit -m "feat(bot): probot entry (event handlers, trigger checks, reconciliation)"
```

---

## Task 9: Deployment docs — README, env example, systemd units

**Files:**
- Create: `bot/.env.example`
- Create: `bot/README.md`
- Create: `bot/cc-mgr-bot.service.example`
- Create: `bot/cc-mgr-bot-smee.service.example`

- [ ] **Step 9.1: Create `bot/.env.example`**

```bash
# ---- GitHub App (Settings → Developer settings → GitHub Apps) ----
APP_ID=
PRIVATE_KEY_PATH=/home/hjin/.config/cc-mgr-bot/cc-mgr-bot.pem
WEBHOOK_SECRET=
# smee.io channel URL (also set as the App's webhook URL + /api/github/webhooks)
WEBHOOK_PROXY_URL=https://smee.io/XXXX

# ---- Behavior ----
TRIGGER_LABEL=bot:fix
# Comma-separated logins allowed to trigger the bot. Empty = repo owner only.
ALLOWED_AUTHORS=
# Repos for startup reconciliation ("owner/repo", comma-separated).
REPOS=cshjin/cc-mgr

# ---- Agent (headless Claude Code) ----
# CLAUDE_CMD must be an absolute path if `claude` is not on PATH.
CLAUDE_CMD=claude
# Extra args appended after `-p --output-format json --permission-mode <mode>`.
# Whitespace-separated tokens only — no quoted values (passed to execFile
# directly, never a shell).
CLAUDE_ARGS=
CLAUDE_PERMISSION_MODE=bypassPermissions
# Comma-separated extra env var names to pass to the claude child. All
# ANTHROPIC_* vars (e.g. ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN for a
# third-party gateway) are passed through automatically.
CLAUDE_ENV_EXTRA=
# Invalid values (non-numeric, zero, negative) fall back to 30.
AGENT_TIMEOUT_MIN=30

# ---- Verification & git ----
# Extra shell commands to run in the clone before committing. NOTE: systemd
# EnvironmentFile values are single-line — join commands with ';', e.g.
# VERIFY_COMMANDS=make lint; make test
# Every changed .js gets `node --check`, every changed .py gets py_compile,
# automatically.
VERIFY_COMMANDS=
BOT_GIT_NAME=cc-mgr-bot[bot]
BOT_GIT_EMAIL=cc-mgr-bot[bot]@users.noreply.github.com

# ---- Safety switch ----
DRY_RUN=0
```

- [ ] **Step 9.2: Create `bot/cc-mgr-bot.service.example`**

```ini
[Unit]
Description=cc-mgr issue bot (Probot + Claude Code)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/hjin/shared/coding/cc-mgr/bot
EnvironmentFile=/home/hjin/.config/cc-mgr-bot/env
ExecStart=/usr/bin/npm start
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

- [ ] **Step 9.3: Create `bot/cc-mgr-bot-smee.service.example`**

```ini
[Unit]
Description=cc-mgr issue bot smee webhook tunnel
After=network-online.target

[Service]
Type=simple
# Replace YOUR_CHANNEL_ID with the smee channel ID from https://smee.io/new
ExecStart=/usr/bin/npx smee -u https://smee.io/YOUR_CHANNEL_ID -t http://localhost:3000/api/github/webhooks
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

- [ ] **Step 9.4: Create `bot/README.md`**

```markdown
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
4. If the diff is empty, verification fails, or claude errors, the issue gets
   a `bot:failed` comment + label (with a log tail).
5. Otherwise: commit → push branch (one-shot installation token, never on
   disk) → open PR `Fix #<n>: <title>` with `Closes #<n>` → comment with the
   summary + PR link → label `bot:done`.

Duplicate triggers are deduped (open-PR lookup by branch name); a restart
re-enqueues labeled issues via startup reconciliation.

## Prerequisites

- Node.js ≥ 20.18.1 (or ≥ 22)
- git
- A working headless `claude -p` on this machine (third-party gateway is
  fine; its env vars must be `ANTHROPIC_*`-named or listed in
  `CLAUDE_ENV_EXTRA`)

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
# edit ~/.config/cc-mgr-bot/env: APP_ID, WEBHOOK_SECRET, WEBHOOK_PROXY_URL, REPOS
```

### 3. Install and test

```bash
npm install
npm test
DRY_RUN=1 npm start   # optional: sanity-run before wiring up systemd
```

### 4. Run as systemd user services

```bash
cp cc-mgr-bot.service.example ~/.config/systemd/user/cc-mgr-bot.service
cp cc-mgr-bot-smee.service.example ~/.config/systemd/user/cc-mgr-bot-smee.service
# edit the smee unit: put your channel URL in ExecStart
systemctl --user daemon-reload
systemctl --user enable --now cc-mgr-bot cc-mgr-bot-smee
loginctl enable-linger   # keep user services running without a login session
journalctl --user -u cc-mgr-bot -f   # logs
```

### 5. First end-to-end check

Open a harmless issue (e.g. a README typo) on cc-mgr, add the `bot:fix`
label, and watch for the "👷" comment → PR. Review and merge; merging closes
the issue via `Closes #<n>`.

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
```

- [ ] **Step 9.5: Add a bot pointer to the root CLAUDE.md**

Append to `/home/hjin/shared/coding/cc-mgr/CLAUDE.md` (after the last section):

```markdown
## GitHub issue bot (`bot/`)

A separate Node app in this repo: a self-hosted Probot GitHub App that fixes
`bot:fix`-labeled issues via headless Claude Code and delivers a PR (see
`bot/README.md`). Requires Node ≥ 20.18.1 — not the Python conda env.
Develop/test with `cd bot && npm test` (Node's built-in test runner, no
framework) and `npm run check`; the runtime config is environment variables
(`bot/.env.example`).
```

- [ ] **Step 9.6: Syntax-check everything and commit**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && npm run check && npm test`
Expected: check exits 0 (all `node --check` lines pass), tests pass.

```bash
cd /home/hjin/shared/coding/cc-mgr
git add bot/.env.example bot/README.md bot/cc-mgr-bot.service.example bot/cc-mgr-bot-smee.service.example CLAUDE.md
git commit -m "docs(bot): README, env example, systemd units, CLAUDE.md pointer"
```

---

## Task 10: Final checks + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 10.1: Run the full check and test suites**

Run: `cd /home/hjin/shared/coding/cc-mgr/bot && npm run check && npm test`
Expected: `node --check` on all six files exits 0; `# pass` total = 36 (4 config + 3 queue + 5 git + 7 helpers + 11 runner + 6 index), `# fail 0`.

- [ ] **Step 10.2: Add the CHANGELOG entry**

At the top of `CHANGELOG.md` (above `## v0.3.0`), insert:

```markdown
## v0.4.0 — 2026-08-19

- GitHub issue bot (`bot/`): label-triggered agentic auto-fix via headless
  Claude Code, delivered as a PR for review (Probot + smee, self-hosted,
  config-driven, DRY_RUN mode).
```

- [ ] **Step 10.3: Commit**

```bash
cd /home/hjin/shared/coding/cc-mgr
git add CHANGELOG.md
git commit -m "docs: changelog for v0.4.0 (issue bot)"
```

---

## Task 11: Manual E2E (user-assisted — real GitHub App + smee)

This task needs the user (GitHub App creation + repo install) and a real
harmless issue. Do NOT skip verification of the push-token and `app.auth()`
paths — this task is their test.

- [ ] **Step 11.1: Create the smee channel and GitHub App**

Follow `bot/README.md` steps 1–2 exactly (smee channel → App with Issues/Pull requests/Contents write + Issues events → private key → install on cc-mgr).

- [ ] **Step 11.2: Run the bot in the foreground once, in DRY_RUN**

```bash
cd /home/hjin/shared/coding/cc-mgr/bot
set -a; . ~/.config/cc-mgr-bot/env; set +a
DRY_RUN=1 npm start
```

In another terminal: `npx smee -u <your-channel-url> -t http://localhost:3000/api/github/webhooks`

Expected: bot logs "Listening on http://localhost:3000".

- [ ] **Step 11.3: Fire a real issue and watch the dry run**

Create a real issue on cc-mgr titled `bot test: rename Quick start heading` with body "In README.md, please change the heading 'Quick start' to 'Quickstart'." Add the `bot:fix` label and wait for the webhook (smee → Probot).

Expected (dry run — no GitHub writes): **no** "👷" comment appears; the bot log shows `enqueued cshjin/cc-mgr#<n>` followed by `DRY_RUN: would push bot/issue-<n>-...` / `DRY_RUN: would open PR ...`. Verify the worktree in `data/bot-worktrees/issue-<n>-*/` contains the README change committed on the `bot/issue-<n>-...` branch.

- [ ] **Step 11.4: Real run (no DRY_RUN)**

Stop the bot, restart without `DRY_RUN=1`. Add the label again to the same issue (remove `bot:failed`/`bot:done` first if present).

Expected: real "👷" comment, then a PR `Fix #<n>: ...` from the bot, an issue comment with the PR link, label `bot:done`. **This step verifies the installation-token push path and `app.auth()`.**

- [ ] **Step 11.5: Review, merge, confirm the issue closes**

Merge the PR from the GitHub UI. Expected: issue auto-closes via `Closes #<n>`. Delete the `bot/issue-<n>-*` branch after merge (GitHub offers this on the PR page).

- [ ] **Step 11.6: Install the systemd units (optional — do it if the bot should run 24/7)**

Follow `bot/README.md` step 4. Confirm `systemctl --user status cc-mgr-bot cc-mgr-bot-smee` shows both active.

- [ ] **Step 11.7: Commit nothing (runtime artifacts only)**

`data/` and `bot/node_modules/` are gitignored. Confirm with `git status` that the tree is clean (apart from anything the user changed).
