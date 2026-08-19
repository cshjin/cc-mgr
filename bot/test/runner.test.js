import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { slugify, buildPrompt, parseResult, claudeEnv, verifyChanges, runClaude, runJob } from '../runner.js';
import { hasDiff, cloneShallow, checkoutNewBranch, commitAll, push } from '../git.js';
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

test('buildPrompt fences untrusted data and truncates', () => {
  const longBody = 'x'.repeat(20000);
  const longComment = 'y'.repeat(10000);
  const comments = Array.from({ length: 30 }, (_, i) => ({ user: `u${i}`, body: i === 29 ? longComment : 'c' }));
  const prompt = buildPrompt({ issueNumber: 1, title: 'T', body: longBody, author: 'a', comments });
  assert.ok(prompt.includes('<issue_data>'));
  assert.ok(prompt.includes('</issue_data>'));
  assert.ok(prompt.includes('(truncated)'));
  assert.ok(!prompt.includes('y'.repeat(9000))); // long comment clipped to 4000 chars
  assert.ok(!prompt.includes('u9:')); // only the newest 20 comments embedded
  assert.ok(prompt.includes('u10:'));
  assert.ok(prompt.length < 100000); // fits comfortably in one argv (128 KiB cap)
  const cjkBody = '汉'.repeat(4000); // 3 UTF-8 bytes each: 12 KB > 10 KB byte cap
  const cjkPrompt = buildPrompt({ issueNumber: 2, title: 'T', body: cjkBody, author: 'a', comments: [] });
  assert.ok(cjkPrompt.includes('(truncated)'));
  assert.ok(Buffer.byteLength(cjkPrompt, 'utf8') < 40000); // clipped by bytes, not chars
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

// Full fake covering the non-dry tail: records every write call. origin is
// the local bare repo standing in for the GitHub remote, so the simulated
// deleteRef API call must actually delete the ref there — otherwise runJob's
// retry push is rejected again (non-fast-forward) and never succeeds.
function fullOctokit(origin) {
  const calls = { deleteRef: [], createComment: [], addLabels: [], removeLabel: [], createLabel: [], createPr: [] };
  return {
    calls,
    paginate: async (fn, args) => (await fn(args)).data,
    rest: {
      pulls: {
        list: async () => ({ data: [] }),
        create: async (args) => {
          calls.createPr.push(args);
          return { data: { html_url: 'https://example.com/pr/1' } };
        },
      },
      issues: {
        listComments: async () => ({ data: [] }),
        getLabel: async () => { const e = new Error('missing'); e.status = 404; throw e; },
        createLabel: async (args) => { calls.createLabel.push(args); },
        addLabels: async (args) => { calls.addLabels.push(args); },
        removeLabel: async (args) => { calls.removeLabel.push(args); },
        createComment: async (args) => { calls.createComment.push(args.body); },
      },
      git: {
        deleteRef: async (args) => {
          calls.deleteRef.push(args.ref);
          // GitHub API ref form is "heads/<branch>"; git needs "refs/heads/...".
          sh(origin, 'update-ref', '-d', `refs/${args.ref}`);
        },
      },
    },
  };
}

test('runJob retries a conflicting push and runs the real tail (non-dry)', async () => {
  const { tmp, origin } = setupRepo();
  const claude = fakeClaude(tmp, "echo fixed >> README.md\necho '{\"result\": \"Fixed.\"}'");
  const config = load({ WORKDIR_ROOT: path.join(tmp, 'work'), CLAUDE_CMD: claude, AGENT_TIMEOUT_MIN: '1' }); // NOT dry

  // Pre-create a conflicting commit on the same branch in the origin, so the
  // first push is rejected (non-fast-forward) and the retry path is taken.
  const other = path.join(tmp, 'other');
  await cloneShallow(origin, other);
  await checkoutNewBranch(other, 'bot/issue-1-typo-in-readme');
  fs.writeFileSync(path.join(other, 'conflict.txt'), 'x\n');
  await commitAll(other, 'conflict', 't', 't@example.com');
  await push(other, origin, '', 'bot/issue-1-typo-in-readme');

  const octokit = fullOctokit(origin);
  const result = await runJob({ job: job(origin), config, octokit, log: () => {} });

  assert.equal(result.status, 'done');
  assert.equal(result.prUrl, 'https://example.com/pr/1');
  assert.deepEqual(octokit.calls.deleteRef, ['heads/bot/issue-1-typo-in-readme']); // stale branch deleted, retried
  assert.ok(octokit.calls.createComment.some((b) => b.includes('Fixed in PR: https://example.com/pr/1')));
  assert.deepEqual(octokit.calls.addLabels, [{ owner: 'o', repo: 'r', issue_number: 1, labels: ['bot:done'] }]);
  assert.deepEqual(octokit.calls.removeLabel, [{ owner: 'o', repo: 'r', issue_number: 1, name: 'bot:fix' }]); // trigger label removed
  const refs = sh(tmp, '--git-dir', origin, 'for-each-ref', '--format=%(refname)').toString();
  assert.ok(refs.includes('refs/heads/bot/issue-1-typo-in-readme'));
});

test('verifyChanges catches bad syntax, skips deleted files, runs config commands', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bot-verify-'));
  fs.writeFileSync(path.join(dir, 'bad.js'), 'function ( {\n');
  fs.writeFileSync(path.join(dir, 'ok.js'), 'console.log(1);\n');
  const failures = await verifyChanges(load({ VERIFY_COMMANDS: 'exit 3' }), dir, ['bad.js', 'ok.js', 'notes.txt', 'gone.js']);
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
