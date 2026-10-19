// Turns zca-js objects into kim-rag events {uid, source, type, ts, payload}.
//
// Pure functions only, so they can be tested without a Zalo login. The privacy rules live here too:
// direct chats are kept, groups are kept only when Kim allowed them, blocked threads leave no trace,
// and every piece of text is scrubbed of keys, passwords and card numbers before it leaves the process.
"use strict";

const crypto = require("node:crypto");
const { scrub } = require("../chrome/scrub.js");

const SOURCE = "zalo";
const MAX_TEXT = 20000;
const MAX_QUOTE = 300;

// Zalo message types, mapped to a short kind and the label used in place of non-text content.
const KINDS = {
  "webchat": ["text", null],
  "chat.photo": ["photo", "photo"],
  "chat.sticker": ["sticker", "sticker"],
  "chat.voice": ["voice", "voice message"],
  "chat.video.msg": ["video", "video"],
  "share.file": ["file", "file"],
  "chat.gif": ["gif", "gif"],
  "chat.doodle": ["drawing", "drawing"],
  "chat.location.new": ["location", "location"],
  "chat.recommended": ["link", "link"],
  "chat.link": ["link", "link"],
  "chat.todo": ["todo", "task"],
};

const threadKind = (type) => (type === 1 ? "group" : "user");   // zca-js ThreadType: User = 0, Group = 1

function isoTs(ts) {
  const ms = Number(ts);
  return new Date(Number.isFinite(ms) && ms > 0 ? ms : Date.now()).toISOString();
}

function clip(text, limit) {
  return text.length <= limit ? text : `${text.slice(0, limit).trimEnd()} … (${text.length - limit} more chars)`;
}

function hash(text) {
  return crypto.createHash("sha256").update(text).digest("hex").slice(0, 16);
}

function str(value) {
  return typeof value === "string" ? value.trim() : "";
}

// Text of a message: the message itself for plain chat, a bracketed label plus any caption or title for
// everything else. Attachment bytes are never fetched.
function messageText(msgType, content) {
  const [kind, label] = KINDS[msgType] || ["other", msgType || "message"];
  if (typeof content === "string") {
    return { kind, text: label ? `[${label}] ${content}`.trim() : content };
  }
  const c = content && typeof content === "object" ? content : {};
  const title = str(c.title);
  const description = str(c.description);
  const href = str(c.href);
  let text;
  if (kind === "file") text = `[file: ${title || "?"}]`;
  else if (kind === "link") text = [`[link${title ? `: ${title}` : ""}]`, description, href].filter(Boolean).join(" ");
  else text = [`[${label}]`, title, description].filter(Boolean).join(" ");
  return { kind, text };
}

// Where a thread stands under Kim's rules: "keep" (full text), "count" (a group Kim did not allow: only
// that a message happened, to decide later which groups are worth keeping) or "drop" (blocked: nothing).
function policy(config, kind, threadId, names = {}) {
  const name = names[threadId];
  const listed = (list) => (list || []).some((x) => String(x) === threadId || (name && x === name));
  if (listed(config.blocked)) return "drop";
  if (kind === "user") return config.users === false ? "drop" : "keep";
  return listed(config.groups) ? "keep" : "count";
}

// One message (UserMessage or GroupMessage from zca-js) to one event, or null when it must not be kept.
function fromMessage(message, ctx) {
  const { config, ownId, names = {} } = ctx;
  const data = message.data || {};
  const kind = threadKind(message.type);
  const threadId = String(message.threadId);
  const rule = policy(config, kind, threadId, names);
  if (rule === "drop") return null;

  const msgId = String(data.msgId || data.realMsgId || data.cliMsgId);
  const base = { uid: `zalo:msg:${kind}:${threadId}:${msgId}`, source: SOURCE, ts: isoTs(data.ts) };
  if (data.content && typeof data.content === "object" && "deleteMsg" in data.content) {
    return fromUndo({ data, threadId, isSelf: message.isSelf, isGroup: kind === "group" }, ctx);
  }
  const { kind: msgKind, text } = messageText(data.msgType, data.content);
  const mentions = (data.mentions || []).map((m) => String(m.uid));
  // Only a mention of Kim by name counts as talking to Kim; @all (uid -1) is kept apart.
  const mentionsMe = mentions.includes(String(ownId));
  const mentionsAll = mentions.includes("-1");

  if (rule === "count") {
    // Only the fact that a message happened. No sender, no words.
    return { ...base, type: "message", payload: {
      thread_id: threadId, thread_kind: kind, msg_id: msgId, skipped: true, is_self: !!message.isSelf,
      msg_type: msgKind, chars: text.length, mentions_me: mentionsMe, mentions_all: mentionsAll,
    } };
  }

  const payload = {
    thread_id: threadId, thread_kind: kind, msg_id: msgId, cli_msg_id: str(String(data.cliMsgId || "")) || null,
    from_id: String(data.uidFrom || ""), from_name: str(data.dName) || null, is_self: !!message.isSelf,
    msg_type: msgKind, text: clip(scrub(text), MAX_TEXT),
  };
  if (data.msgType && !KINDS[data.msgType]) payload.raw_type = data.msgType;
  if (data.quote) {
    payload.quote = {
      from_id: String(data.quote.ownerId || ""), from_name: str(data.quote.fromD) || null,
      msg_id: String(data.quote.globalMsgId || ""), text: clip(scrub(str(data.quote.msg)), MAX_QUOTE),
    };
  }
  if (mentions.length) {
    payload.mentions = mentions;
    payload.mentions_me = mentionsMe;
    payload.mentions_all = mentionsAll;
  }
  return { ...base, type: "message", payload };
}

// A message taken back by its sender. The server drops the original if it has not been grouped yet.
function fromUndo(undo, ctx) {
  const { config, names = {} } = ctx;
  const data = undo.data || {};
  const kind = undo.isGroup ? "group" : "user";
  const threadId = String(undo.threadId);
  if (policy(config, kind, threadId, names) === "drop") return null;
  const target = String((data.content && data.content.globalMsgId) || "");
  return {
    uid: `zalo:undo:${kind}:${threadId}:${target}`, source: SOURCE, type: "undo", ts: isoTs(data.ts),
    payload: { thread_id: threadId, thread_kind: kind, msg_id: target, from_id: String(data.uidFrom || ""),
               is_self: !!undo.isSelf },
  };
}

// A name for a person or a group. The uid changes only when the name changes, so the daily refresh adds
// nothing to the server unless something was renamed.
function fromContact(kind, id, name, extra = {}) {
  const clean = str(name);
  if (!id || !clean) return null;
  return {
    uid: `zalo:${kind}:${id}:${hash(clean)}`, source: SOURCE, type: "contact", ts: new Date().toISOString(),
    payload: { thread_id: String(id), thread_kind: kind, name: clean, ...extra },
  };
}

// The sensor's own state: connected, closed (with the reason), needs login, or a heartbeat.
function status(state, extra = {}) {
  return {
    uid: `zalo:status:${crypto.randomUUID()}`, source: SOURCE, type: "status", ts: new Date().toISOString(),
    payload: { state, ...extra },
  };
}

// Larger of two Zalo message ids (numeric strings longer than a JS number can hold).
function laterId(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  try { return BigInt(a) >= BigInt(b) ? a : b; } catch { return a; }
}

module.exports = { SOURCE, KINDS, messageText, policy, fromMessage, fromUndo, fromContact, status, laterId, isoTs };
