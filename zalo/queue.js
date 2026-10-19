// Events wait in a JSON-lines file until the kim-rag server takes them, so nothing is lost while the
// server is down or the Mac is offline. Same rules as the Chrome extension: send in batches to
// POST /events; a 400 or 422 means the server will never take that batch, so it is dropped instead of
// blocking the queue forever.
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const BATCH = 100;
const MAX_QUEUE = 50000;

class Queue {
  constructor(file, { server, token = "", fetchImpl = fetch, log = () => {} }) {
    this.file = file;
    this.server = server.replace(/\/+$/, "");
    this.token = token;
    this.fetch = fetchImpl;
    this.log = log;
    this.flushing = false;
    this.lastError = null;
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  }

  // Everything below runs synchronously, so a flush that is waiting on the network never interleaves
  // with an append.
  read() {
    let raw;
    try { raw = fs.readFileSync(this.file, "utf8"); } catch (err) { if (err.code === "ENOENT") return []; throw err; }
    const out = [];
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try { out.push(JSON.parse(line)); } catch { this.log("queue: skipped a broken line"); }
    }
    return out;
  }

  write(events) {
    const tmp = `${this.file}.tmp`;
    fs.writeFileSync(tmp, events.map((e) => JSON.stringify(e)).join("\n") + (events.length ? "\n" : ""), { mode: 0o600 });
    fs.renameSync(tmp, this.file);
  }

  push(events) {
    const list = events.filter(Boolean);
    if (!list.length) return;
    fs.appendFileSync(this.file, list.map((e) => JSON.stringify(e)).join("\n") + "\n", { mode: 0o600 });
    const all = this.read();
    if (all.length > MAX_QUEUE) {
      this.write(all.slice(-MAX_QUEUE));
      this.log(`queue: over ${MAX_QUEUE} events, dropped the oldest ${all.length - MAX_QUEUE}`);
    }
  }

  size() {
    return this.read().length;
  }

  // Sends until the queue is empty or the server stops answering. Returns how many events were sent.
  async flush() {
    if (this.flushing) return 0;
    this.flushing = true;
    let sent = 0;
    try {
      for (;;) {
        const batch = this.read().slice(0, BATCH);
        if (!batch.length) break;
        const headers = { "Content-Type": "application/json" };
        if (this.token) headers.Authorization = `Bearer ${this.token}`;
        let resp;
        try {
          resp = await this.fetch(`${this.server}/events`, { method: "POST", headers, body: JSON.stringify(batch) });
        } catch (err) {
          this.lastError = "server not running";
          break;
        }
        if (!resp.ok && resp.status !== 400 && resp.status !== 422) {
          this.lastError = `HTTP ${resp.status}`;
          break;
        }
        if (!resp.ok) this.log(`queue: server refused a batch (${resp.status}): ${await resp.text()}`);
        const done = new Set(batch.map((e) => e.uid));
        this.write(this.read().filter((e) => !done.has(e.uid)));
        sent += batch.length;
        this.lastError = null;
      }
    } finally {
      this.flushing = false;
    }
    return sent;
  }
}

module.exports = { Queue, BATCH, MAX_QUEUE };
