// Run: cd zalo && npm test
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const N = require("./normalize");

const OWN = "100";
const cfg = (extra = {}) => ({ users: true, groups: [], blocked: [], ...extra });
const ctx = (extra = {}, names = {}) => ({ config: cfg(extra), ownId: OWN, names });

function userMsg(data, { isSelf = false, threadId = "200" } = {}) {
  return { type: 0, threadId, isSelf, data: { msgId: "7001", cliMsgId: "55", msgType: "webchat", uidFrom: "200",
    dName: "Anh Nam", ts: "1760500000000", content: "Thứ Sáu anh gửi báo giá nhé", ...data } };
}

function groupMsg(data, { isSelf = false, threadId = "900" } = {}) {
  return { type: 1, threadId, isSelf, data: { msgId: "8001", cliMsgId: "66", msgType: "webchat", uidFrom: "300",
    dName: "Chị Lan", ts: "1760500000000", content: "Họp lúc 3 giờ", ...data } };
}

test("a direct message keeps who, where, when and the words", () => {
  const e = N.fromMessage(userMsg({}), ctx());
  assert.strictEqual(e.source, "zalo");
  assert.strictEqual(e.type, "message");
  assert.strictEqual(e.uid, "zalo:msg:user:200:7001");
  assert.strictEqual(e.ts, new Date(1760500000000).toISOString());
  assert.deepStrictEqual(
    { thread: e.payload.thread_id, kind: e.payload.thread_kind, from: e.payload.from_name, text: e.payload.text,
      self: e.payload.is_self, type: e.payload.msg_type },
    { thread: "200", kind: "user", from: "Anh Nam", text: "Thứ Sáu anh gửi báo giá nhé", self: false, type: "text" });
});

test("Kim's own message is kept and marked", () => {
  const e = N.fromMessage(userMsg({ uidFrom: "0", dName: "Kim" }, { isSelf: true }), ctx());
  assert.strictEqual(e.payload.is_self, true);
});

test("the same message twice gives the same uid", () => {
  assert.strictEqual(N.fromMessage(userMsg({}), ctx()).uid, N.fromMessage(userMsg({ ts: "1760500009999" }), ctx()).uid);
});

test("secrets are masked before they leave", () => {
  const e = N.fromMessage(userMsg({ content: "pass là password=hunter2hunter2, thẻ 4111 1111 1111 1111" }), ctx());
  assert.ok(!e.payload.text.includes("hunter2"));
  assert.ok(!e.payload.text.includes("4111 1111"));
});

test("a group Kim did not allow is only counted: no sender, no words", () => {
  const e = N.fromMessage(groupMsg({ mentions: [{ uid: OWN, pos: 0, len: 3, type: 0 }] }), ctx());
  assert.strictEqual(e.payload.skipped, true);
  assert.strictEqual(e.payload.mentions_me, true);
  assert.strictEqual(e.payload.chars, "Họp lúc 3 giờ".length);
  for (const key of ["text", "from_id", "from_name", "quote"]) assert.ok(!(key in e.payload), key);
});

test("@all is not a mention of Kim", () => {
  const all = N.fromMessage(groupMsg({ mentions: [{ uid: "-1", pos: 0, len: 4, type: 1 }] }), ctx());
  assert.strictEqual(all.payload.mentions_me, false);
  assert.strictEqual(all.payload.mentions_all, true);
  const kept = N.fromMessage(groupMsg({ mentions: [{ uid: "-1", pos: 0, len: 4, type: 1 }] }), ctx({ groups: ["900"] }));
  assert.strictEqual(kept.payload.mentions_me, false);
  assert.strictEqual(kept.payload.mentions_all, true);
  const named = N.fromMessage(groupMsg({ mentions: [{ uid: OWN, pos: 0, len: 3, type: 0 }] }), ctx({ groups: ["900"] }));
  assert.strictEqual(named.payload.mentions_me, true);
  assert.strictEqual(named.payload.mentions_all, false);
});

test("an allowed group is kept in full, by id or by name", () => {
  assert.strictEqual(N.fromMessage(groupMsg({}), ctx({ groups: ["900"] })).payload.text, "Họp lúc 3 giờ");
  const byName = N.fromMessage(groupMsg({}), ctx({ groups: ["Kế toán"] }, { 900: "Kế toán" }));
  assert.strictEqual(byName.payload.from_name, "Chị Lan");
});

test("a blocked person or group leaves no trace", () => {
  assert.strictEqual(N.fromMessage(userMsg({}), ctx({ blocked: ["200"] })), null);
  assert.strictEqual(N.fromMessage(groupMsg({}), ctx({ groups: ["900"], blocked: ["900"] })), null);
  assert.strictEqual(N.fromMessage(userMsg({}), ctx({ blocked: ["Anh Nam"] }, { 200: "Anh Nam" })), null);
  assert.strictEqual(N.fromMessage(userMsg({}), ctx({ users: false })), null);
});

test("attachments become short labels, files keep their name, links keep title and address", () => {
  const photo = N.fromMessage(userMsg({ msgType: "chat.photo", content: { title: "bản vẽ tầng 2", href: "https://x/y.jpg" } }), ctx());
  assert.strictEqual(photo.payload.text, "[photo] bản vẽ tầng 2");
  const file = N.fromMessage(userMsg({ msgType: "share.file", content: { title: "bao-gia.pdf" } }), ctx());
  assert.strictEqual(file.payload.text, "[file: bao-gia.pdf]");
  const link = N.fromMessage(userMsg({ msgType: "chat.recommended", content: { title: "Bài báo", href: "https://vnexpress.net/a" } }), ctx());
  assert.strictEqual(link.payload.text, "[link: Bài báo] https://vnexpress.net/a");
  const sticker = N.fromMessage(userMsg({ msgType: "chat.sticker", content: { id: 1 } }), ctx());
  assert.strictEqual(sticker.payload.text, "[sticker]");
  const odd = N.fromMessage(userMsg({ msgType: "chat.ecard", content: {} }), ctx());
  assert.strictEqual(odd.payload.msg_type, "other");
  assert.strictEqual(odd.payload.raw_type, "chat.ecard");
});

test("a quoted message is kept short", () => {
  const e = N.fromMessage(userMsg({ quote: { ownerId: "100", fromD: "Kim", globalMsgId: 6999, msg: "x".repeat(1000) } }), ctx());
  assert.strictEqual(e.payload.quote.from_name, "Kim");
  assert.ok(e.payload.quote.text.length < 400);
});

test("an undo names the message taken back", () => {
  const undo = { threadId: "200", isSelf: false, isGroup: false,
                 data: { uidFrom: "200", ts: "1760500100000", content: { globalMsgId: 7001, deleteMsg: 1 } } };
  const e = N.fromUndo(undo, ctx());
  assert.strictEqual(e.type, "undo");
  assert.strictEqual(e.payload.msg_id, "7001");
  assert.strictEqual(e.uid, "zalo:undo:user:200:7001");
});

test("an undo inside the old-message replay is still an undo", () => {
  const e = N.fromMessage(userMsg({ content: { globalMsgId: 7001, deleteMsg: 1 } }), ctx());
  assert.strictEqual(e.type, "undo");
});

test("contact uid changes only when the name changes", () => {
  const a = N.fromContact("user", "200", "Anh Nam");
  assert.strictEqual(a.uid, N.fromContact("user", "200", " Anh Nam ").uid);
  assert.notStrictEqual(a.uid, N.fromContact("user", "200", "Nam Kế toán").uid);
  assert.strictEqual(N.fromContact("user", "200", ""), null);
});

test("message ids larger than a JS number compare correctly", () => {
  assert.strictEqual(N.laterId("9007199254740993", "9007199254740992"), "9007199254740993");
  assert.strictEqual(N.laterId(null, "5"), "5");
  assert.strictEqual(N.laterId("5", ""), "5");
});
