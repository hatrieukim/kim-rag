#!/usr/bin/env node
// Loads Zalo history from an official Zalo backup into kim-rag (product 10).
//
//   node import.js <..._zmessage.zdb> [--since YYYY-MM-DD] [--dry-run]
//
// The backup is the `.zdb` message file of a Zalo PC backup: one JSON object per line. Every row goes
// through the same rules as the live sensor (normalize.js): direct chats are kept, groups only when listed
// in zalo.json, blocked chats leave no trace, secrets are masked. Message uids are the sensor's, so a
// message both in the backup and already heard by the sensor is stored once.
//
// Left out on purpose: messages taken back or deleted by their sender (they withdrew them), bank account
// cards, and system lines such as "X left the group". Photo and file links are not kept, only labels.
"use strict";

const fs = require("node:fs");
const readline = require("node:readline");
const config = require("./config");
const { Queue } = require("./queue");
const N = require("./normalize");

// Backup message types (numbers) to the zca-js type names normalize.js understands.
const TYPES = {
  1: "webchat", 2: "chat.photo", 4: "chat.sticker", 6: "chat.recommended", 7: "chat.gif",
  17: "chat.location.new", 18: "chat.video.msg", 19: "share.file", 25: "chat.contact", 26: "chat.poll",
  52: "chat.card", [-1909]: "chat.card",
};
const DROP = new Set([20, -27, -4, 21]);   // taken back, deleted, system line, group header
const LABELS = { "chat.contact": "contact card", "chat.poll": "poll", "chat.card": "card" };

function parseJson(text) {
  try { return JSON.parse(text); } catch { return null; }
}

function minutes(seconds) {
  const s = Number(seconds) || 0;
  return s < 60 ? `${s} s` : `${Math.round(s / 60)} min`;
}

// What the message says, in the shape zca-js would have delivered it. Null when it must not be imported.
function content(row) {
  const m = row.message;
  const type = TYPES[row.msgType];
  if (row.msgType === 1 && m && typeof m === "object") return { type: "webchat", content: String(m.title || "") };
  if (!m || typeof m !== "object") return { type: type || `zalo.${row.msgType}`, content: m == null ? "" : String(m) };
  const action = String(m.action || "");
  if (action === "zinstant.bankcard") return null;
  if (action === "recommened.calltime" || action === "recommened.misscall") {
    const p = (typeof m.params === "string" ? parseJson(m.params) : m.params) || {};
    const label = action === "recommened.misscall" ? "missed call" : `call, ${minutes(p.duration)}`;
    return { type: "webchat", content: `[${label}]` };
  }
  if (LABELS[type]) {
    const text = [String(m.title || "").trim(), String(m.description || m.desc || "").trim()].filter(Boolean).join(" ");
    return { type: "webchat", content: `[${LABELS[type]}]${text ? " " + text : ""}` };
  }
  // Photos, files, links, videos: keep title and description; never the signed download links.
  return { type: type || `zalo.${row.msgType}`, content: { title: m.title, description: m.description || m.desc,
    href: type === "chat.recommended" ? m.href : undefined } };
}

// One backup row to one kim-rag event, or null.
function fromBackup(row, ctx) {
  if (DROP.has(row.msgType) || row.uidSenderDel) return null;
  const body = content(row);
  if (!body) return null;
  const to = String(row.toUid || "");
  const isGroup = to.startsWith("g");
  const self = String(row.fromUid) === "0";
  const message = {
    type: isGroup ? 1 : 0,
    threadId: isGroup ? to.slice(1) : to,
    isSelf: self,
    data: {
      msgId: String(row.msgId), cliMsgId: row.cliMsgId, msgType: body.type, content: body.content,
      uidFrom: self ? ctx.ownId : String(row.fromUid || ""), dName: row.dName || null, ts: String(row.sendDttm),
      quote: row.quote || undefined, mentions: Array.isArray(row.mentions) ? row.mentions : undefined,
    },
  };
  const event = N.fromMessage(message, ctx);
  if (event) event.payload.origin = "backup";
  return event;
}

// Rows of a backup file. A few messages hold a raw line break, which splits them over several lines: a line
// that does not parse is joined with the next ones until the whole message does. Returns {row} or {broken}.
async function* readRows(stream, maxJoin = 20) {
  let pending = null;
  let joined = 0;
  for await (const line of readline.createInterface({ input: stream, crlfDelay: Infinity })) {
    if (pending === null && !line.trim()) continue;
    const text = pending === null ? line : `${pending}\\n${line}`;   // the escaped form: JSON strings cannot hold a raw break
    const row = parseJson(text);
    if (row) { yield { row }; pending = null; joined = 0; continue; }
    if (pending !== null && line.startsWith("{") && parseJson(line)) {   // the next message: give up on the old one
      yield { broken: pending };
      yield { row: parseJson(line) };
      pending = null; joined = 0; continue;
    }
    pending = text;
    if (++joined > maxJoin) { yield { broken: pending }; pending = null; joined = 0; }
  }
  if (pending !== null) yield { broken: pending };
}

// Names of people heard in the backup, dated at their last message so a newer name from the sensor wins.
function contactsFrom(rows) {
  const last = new Map();
  for (const r of rows) {
    if (String(r.fromUid) === "0" || !r.dName || String(r.toUid || "").startsWith("g")) continue;
    const prev = last.get(String(r.fromUid));
    if (!prev || Number(r.sendDttm) > Number(prev.sendDttm)) last.set(String(r.fromUid), r);
  }
  return [...last.values()].map((r) => {
    const e = N.fromContact("user", String(r.fromUid), r.dName);
    if (e) { e.ts = N.isoTs(r.sendDttm); e.payload.origin = "backup"; }
    return e;
  }).filter(Boolean);
}

async function main() {
  const args = process.argv.slice(2);
  const file = args.find((a) => !a.startsWith("--"));
  const sinceArg = args[args.indexOf("--since") + 1];
  const dry = args.includes("--dry-run");
  if (!file) {
    console.error("usage: node import.js <..._zmessage.zdb> [--since YYYY-MM-DD] [--dry-run]");
    process.exit(2);
  }
  const since = args.includes("--since") ? Date.parse(`${sinceArg}T00:00:00+07:00`) : Date.now() - 90 * 86400e3;
  if (!Number.isFinite(since)) throw new Error(`--since must be YYYY-MM-DD, got ${sinceArg}`);

  const cfg = config.load();
  const state = config.readJson(config.PATHS.state, {});
  if (!state.ownId) throw new Error("the sensor has not logged in yet; run npm run login first");
  const ctx = { config: cfg, ownId: String(state.ownId), names: state.names || {} };

  const rows = [];
  let bad = 0;
  for await (const { row, broken } of readRows(fs.createReadStream(file))) {
    if (broken !== undefined) { bad += 1; continue; }
    if (Number(row.sendDttm) >= since) rows.push(row);
  }

  const counts = { rows: rows.length, kept: 0, counted: 0, left_out: 0, unreadable: bad };
  const events = [];
  for (const row of rows) {
    const e = fromBackup(row, ctx);
    if (!e) { counts.left_out += 1; continue; }
    if (e.payload.skipped) counts.counted += 1; else counts.kept += 1;
    events.push(e);
  }
  const contacts = contactsFrom(rows);
  console.log(JSON.stringify({ since: new Date(since).toISOString(), ...counts, contacts: contacts.length }));
  if (dry) return;

  const queue = new Queue(`${config.PRIVATE}/import-queue.jsonl`, { server: cfg.server, token: cfg.token,
                                                                     log: (m) => console.log(m) });
  queue.push(contacts.concat(events));
  const sent = await queue.flush();
  const left = queue.size();
  console.log(`sent ${sent} events, ${left} still queued${left ? ` (${queue.lastError}); run again to retry` : ""}`);
  if (left) process.exit(1);
}

if (require.main === module) {
  main().catch((err) => { console.error(err.stack || err.message || err); process.exit(1); });
}

module.exports = { fromBackup, contactsFrom, content, readRows, TYPES, DROP };
