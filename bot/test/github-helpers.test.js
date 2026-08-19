import { test } from 'node:test';
import assert from 'node:assert/strict';
import { findBotPr, ensureLabel, removeLabel, deleteBranch, listComments } from '../github-helpers.js';

const mkErr = (status) => {
  const err = new Error('nope');
  err.status = status;
  return err;
};

test('findBotPr matches the branch prefix for the issue', async () => {
  const octokit = {
    rest: {
      pulls: {
        list: async () => ({
          data: [
            { head: { ref: 'bot/issue-1-fix-typo' } },
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
  const octokit = { rest: { pulls: { list: async () => ({ data: [] }) } } };
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

test('listComments maps to {user, body}', async () => {
  const octokit = {
    rest: {
      issues: {
        listComments: async () => ({
          data: [
            { user: { login: 'alice' }, body: 'hi' },
            { user: { login: 'bob' }, body: null },
          ],
        }),
      },
    },
  };
  assert.deepEqual(await listComments(octokit, { owner: 'o', repo: 'r', issueNumber: 1 }), [
    { user: 'alice', body: 'hi' },
    { user: 'bob', body: '' },
  ]);
});
