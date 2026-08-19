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
