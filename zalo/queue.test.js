// Run: cd zalo && npm test
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { Queue, BATCH } = require("./queue");

const tmpFile = () => path.join(fs.mkdtempSync(path.join(os.tmpdir(), "kimrag-zalo-")), "queue.jsonl");
const ev = (i) => ({ uid: `zalo:test:${i}`, source: "zalo", type: "message", ts: new Date().toISOString(), payload: { i } });

function fakeServer(statuses) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const status = statuses.length ? statuses.shift() : 200;
    if (status === "down") throw new Error("fetch failed");
    return { ok: status < 300, status, text: async () => "refused" };
  };
  return { calls, fetchImpl };
}

test("events survive until the server takes them, in batches", async () => {
  const file = tmpFile();
  const server = fakeServer(["down"]);
  const q = new Queue(file, { server: "http://127.0.0.1:8000/", token: "t", fetchImpl: server.fetchImpl });
  q.push(Array.from({ length: BATCH + 5 }, (_, i) => ev(i)));
  assert.strictEqual(await q.flush(), 0);
  assert.strictEqual(q.lastError, "server not running");
  assert.strictEqual(q.size(), BATCH + 5);

  assert.strictEqual(await q.flush(), BATCH + 5);
  assert.strictEqual(q.size(), 0);
  const [first] = server.calls.slice(1);
  assert.strictEqual(first.url, "http://127.0.0.1:8000/events");
  assert.strictEqual(first.init.headers.Authorization, "Bearer t");
  assert.strictEqual(first.body.length, BATCH);
});

test("a batch the server refuses is dropped, a server error keeps it", async () => {
  const file = tmpFile();
  const server = fakeServer([500]);
  const q = new Queue(file, { server: "http://x", fetchImpl: server.fetchImpl });
  q.push([ev(1)]);
  await q.flush();
  assert.strictEqual(q.size(), 1);
  assert.strictEqual(q.lastError, "HTTP 500");

  server.calls.length = 0;
  const refusing = fakeServer([422]);
  const q2 = new Queue(file, { server: "http://x", fetchImpl: refusing.fetchImpl });
  await q2.flush();
  assert.strictEqual(q2.size(), 0);
});

test("nulls from the privacy rules are never queued, and the file is private", () => {
  const file = tmpFile();
  const q = new Queue(file, { server: "http://x", fetchImpl: async () => ({ ok: true }) });
  q.push([null, ev(1), null]);
  assert.strictEqual(q.size(), 1);
  assert.strictEqual(fs.statSync(file).mode & 0o777, 0o600);
});
