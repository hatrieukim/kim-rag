// Run: cd zalo && npm test
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const B = require("./backoff");

// One connection that lives `ms`, then the listener gives up with `code`.
function cycle(b, ms, code = 1006, t = { now: 0 }) {
  b.connected(t.now);
  t.now += ms;
  b.disconnected(t.now);
  return b.next(code);
}

test("a session refused again and again waits longer each time, then is suspected dead", () => {
  const b = new B.Backoff();
  const t = { now: 0 };
  const delays = [];
  let last;
  for (let i = 0; i < B.DEAD_AFTER; i++) {
    last = cycle(b, 10, 1006, t);
    delays.push(last.delay);
  }
  assert.deepStrictEqual(delays, [30e3, 60e3, 120e3, 240e3, 480e3]);
  assert.strictEqual(last.suspectDead, true);
});

test("the wait never goes past fifteen minutes", () => {
  const b = new B.Backoff();
  let last;
  for (let i = 0; i < 12; i++) last = cycle(b, 10);
  assert.strictEqual(last.delay, B.MAX_WAIT_MS);
});

test("a connection that stayed up resets the wait and the suspicion", () => {
  const b = new B.Backoff();
  for (let i = 0; i < 4; i++) cycle(b, 10);
  const after = cycle(b, B.STABLE_MS + 1);
  assert.strictEqual(after.delay, B.MIN_WAIT_MS);
  assert.strictEqual(after.suspectDead, false);
});

test("a session checked as fine is not suspected again at once", () => {
  const b = new B.Backoff();
  let last;
  for (let i = 0; i < B.DEAD_AFTER; i++) last = cycle(b, 10);
  assert.strictEqual(last.suspectDead, true);
  b.sessionOk();
  assert.strictEqual(cycle(b, 10).suspectDead, false);
});

test("being pushed out by another web session waits thirty minutes and is not a dead session", () => {
  const b = new B.Backoff();
  const r = cycle(b, 10, 3000);
  assert.deepStrictEqual(r, { delay: B.KICKED_WAIT_MS, kicked: true, suspectDead: false });
});

test("a close without a connection does not count as refused", () => {
  const b = new B.Backoff();
  b.disconnected(5);
  assert.strictEqual(b.refused, 0);
});
