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
  });
  assert.equal(c.triggerLabel, 'x:fix');
  assert.deepEqual(c.allowedAuthors, ['alice', 'bob']);
  assert.equal(c.agentTimeoutMin, 5);
  assert.equal(c.dryRun, true);
  assert.deepEqual(c.claudeArgs, ['--max-turns', '10']);
  assert.deepEqual(c.claudeEnvExtra, ['FOO', 'BAR']);
  assert.deepEqual(c.repos, ['cshjin/cc-mgr']);
  assert.deepEqual(c.verifyCommands, ['echo ok', 'npm test']);
});

test('list helper', () => {
  assert.deepEqual(list(undefined), []);
  assert.deepEqual(list('  a, b ,,c '), ['a', 'b', 'c']);
});
