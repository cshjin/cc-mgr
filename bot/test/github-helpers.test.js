import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findBotPr, ensureLabel, removeLabel, deleteBranch, listComments, addComment, createPr, addLabels } from '../github-helpers.js';

const mkErr = (status) => {
  const err = new Error('nope');
  err.status = status;
  return err;
};

test('findBotPr matches the branch prefix for the issue', async () => {
  const octokit = {
    paginate: async (fn, args) => (await fn(args)).data,
    rest: {
      pulls: {
        list: async () => ({
          data: [
            { head: { ref: 'bot/issue-1-fix-typo' } },
            { head: { ref: 'bot/issue-2-other' } },
            { head: { ref: 'bot/issue-10-other' } },
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
  const octokit = {
    paginate: async (fn, args) => (await fn(args)).data,
    rest: { pulls: { list: async () => ({ data: [] }) } },
  };
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
