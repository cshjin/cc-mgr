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
