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
