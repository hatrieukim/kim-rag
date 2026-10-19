// Run: cd zalo && npm test
"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { fromBackup, contactsFrom } = require("./import");

const OWN = "100";
const ctx = { config: { users: true, groups: ["900"], blocked: ["666"] }, ownId: OWN, names: {} };
const TS = "1760500000000";

// A row as it appears in a Zalo PC backup (`_zmessage.zdb`).
const row = (extra) => ({ msgId: "7001", cliMsgId: "55", toUid: "200", fromUid: "200", dName: "Anh Nam",
  sendDttm: TS, msgType: 1, message: "Thứ Sáu anh gửi báo giá nhé", mentions: [], quote: null, ...extra });

test("a direct message gets the same uid and fields as the sensor would give it", () => {
  const e = fromBackup(row({}), ctx);
  assert.strictEqual(e.uid, "zalo:msg:user:200:7001");
  assert.strictEqual(e.ts, new Date(Number(TS)).toISOString());
  assert.strictEqual(e.payload.text, "Thứ Sáu anh gửi báo giá nhé");
  assert.strictEqual(e.payload.from_name, "Anh Nam");
  assert.strictEqual(e.payload.origin, "backup");
});

test("Kim's own message: sender 0 in the backup, Kim's id in kim-rag", () => {
  const e = fromBackup(row({ fromUid: "0", dName: "Kim" }), ctx);
  assert.strictEqual(e.payload.is_self, true);
  assert.strictEqual(e.payload.from_id, OWN);
  assert.strictEqual(e.payload.thread_id, "200");
});

test("group ids lose the g, and only allowed groups keep their words", () => {
  const kept = fromBackup(row({ toUid: "g900", fromUid: "300", dName: "Chị Lan" }), ctx);
  assert.strictEqual(kept.uid, "zalo:msg:group:900:7001");
  assert.strictEqual(kept.payload.from_name, "Chị Lan");
  const counted = fromBackup(row({ toUid: "g901", fromUid: "300", mentions: [{ uid: OWN, pos: 0, len: 3, type: 0 }] }), ctx);
  assert.strictEqual(counted.payload.skipped, true);
  assert.strictEqual(counted.payload.mentions_me, true);
  assert.ok(!("text" in counted.payload));
});

test("blocked chats, withdrawn and deleted messages, system lines and bank cards are left out", () => {
  assert.strictEqual(fromBackup(row({ toUid: "666" }), ctx), null);
  assert.strictEqual(fromBackup(row({ msgType: 20, message: "[Tin nhắn đã được thu hồi]" }), ctx), null);
  assert.strictEqual(fromBackup(row({ msgType: -27, message: "[Tin nhắn đã bị xóa]" }), ctx), null);
  assert.strictEqual(fromBackup(row({ msgType: -4, message: "X đã rời nhóm" }), ctx), null);
  assert.strictEqual(fromBackup(row({ uidSenderDel: "200" }), ctx), null);
  assert.strictEqual(fromBackup(row({ msgType: 52, message: { action: "zinstant.bankcard", title: "VCB 0123" } }), ctx), null);
});

test("calls become a short line with their length", () => {
  const call = fromBackup(row({ msgType: 6, message: { action: "recommened.calltime", title: "Cuộc gọi thoại",
    params: JSON.stringify({ duration: 312, isCaller: 1 }) } }), ctx);
  assert.strictEqual(call.payload.text, "[call, 5 min]");
  const missed = fromBackup(row({ msgType: 6, message: { action: "recommened.misscall", params: "{}" } }), ctx);
  assert.strictEqual(missed.payload.text, "[missed call]");
});

test("photos and files keep a label and caption, never the download link", () => {
  const photo = fromBackup(row({ msgType: 2, message: { title: "bản vẽ tầng 2", oriUrl: "https://cdn/x.jpg?sig=abc",
    hdUrl: "https://cdn/x.jpg?sig=abc" } }), ctx);
  assert.strictEqual(photo.payload.text, "[photo] bản vẽ tầng 2");
  const file = fromBackup(row({ msgType: 19, message: { title: "bao-gia.pdf", href: "https://f.zdn.vn/x?token=1" } }), ctx);
  assert.strictEqual(file.payload.text, "[file: bao-gia.pdf]");
  const link = fromBackup(row({ msgType: 6, message: { action: "recommened.link", title: "Bài báo", href: "https://vnexpress.net/a" } }), ctx);
  assert.strictEqual(link.payload.text, "[link: Bài báo] https://vnexpress.net/a");
});

test("rich text, polls, contact cards and unknown types still read sensibly", () => {
  assert.strictEqual(fromBackup(row({ message: { action: "rtf", title: "chữ **đậm**" } }), ctx).payload.text, "chữ **đậm**");
  assert.strictEqual(fromBackup(row({ msgType: 26, message: { action: "vote", title: "Ăn trưa ở đâu?" } }), ctx).payload.text,
                     "[poll] Ăn trưa ở đâu?");
  assert.strictEqual(fromBackup(row({ msgType: 25, message: { action: "show.profile", title: "Chị Lan" } }), ctx).payload.text,
                     "[contact card] Chị Lan");
  const odd = fromBackup(row({ msgType: 99, message: { title: "?" } }), ctx);
  assert.strictEqual(odd.payload.msg_type, "other");
});

test("names from the backup are dated at the last message, so a newer name wins", () => {
  const [c] = contactsFrom([row({ sendDttm: "1700000000000", dName: "Nam" }), row({ sendDttm: TS, dName: "Anh Nam" }),
                             row({ fromUid: "0", dName: "Kim" }), row({ toUid: "g900", fromUid: "300", dName: "Chị Lan" })]);
  assert.strictEqual(c.payload.name, "Anh Nam");
  assert.strictEqual(c.ts, new Date(Number(TS)).toISOString());
  assert.strictEqual(contactsFrom([row({ fromUid: "0" })]).length, 0);
});

test("a message split over several lines by a raw line break is read whole", async () => {
  const { Readable } = require("node:stream");
  const { readRows } = require("./import");
  const a = JSON.stringify(row({ msgId: "1" }));
  const split = JSON.stringify(row({ msgId: "2", message: "dòng một\nDÒNG_HAI" })).replace("\\nDÒNG_HAI", "\nDÒNG_HAI");
  const c = JSON.stringify(row({ msgId: "3" }));
  const out = [];
  for await (const r of readRows(Readable.from([`${a}\n${split}\n\n${c}\n{"msgId":"4", broken\n`]))) out.push(r);
  assert.deepStrictEqual(out.filter((r) => r.row).map((r) => r.row.msgId), ["1", "2", "3"]);
  assert.strictEqual(out.find((r) => r.row && r.row.msgId === "2").row.message, "dòng một\nDÒNG_HAI");
  assert.strictEqual(out.filter((r) => r.broken !== undefined).length, 1);
});
