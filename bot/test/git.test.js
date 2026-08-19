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
