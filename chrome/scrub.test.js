// Chạy: node --test chrome/
const test = require("node:test");
const assert = require("node:assert");
const K = require("./scrub.js");

test("che khóa, mật khẩu, số thẻ; giữ số thường", () => {
  assert.ok(!K.scrub("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv").includes("sk-ant"));
  assert.strictEqual(K.scrub("password=hunter2hunter2"), `password=${K.MASK}`);
  assert.strictEqual(K.scrub("thẻ 4111 1111 1111 1111"), `thẻ ${K.MASK}`);
  assert.strictEqual(K.scrub("MST 0312345678"), "MST 0312345678");
});

test("chặn trang nhạy cảm và máy trong mạng riêng", () => {
  const list = K.DEFAULT_BLOCKED;
  for (const host of ["mail.google.com", "ebanking.vietinbank.vn", "login.misa.vn", "localhost",
                      "192.168.1.20", "10.0.0.5", "myserver.local", "[::1]"]) {
    assert.ok(K.blocked(host, list), host);
  }
  for (const host of ["github.com", "www.youtube.com", "cafe", "docs.python.org"]) {
    assert.ok(!K.blocked(host, list), host);
  }
});

test("tiêu đề gọn một dòng", () => {
  assert.strictEqual(K.cleanTitle("(13) Gergely on X: \"What is\n\nthe PR output\""), 'Gergely on X: "What is the PR output"');
  assert.strictEqual(K.cleanTitle("x".repeat(300)).length, 200);
});

test("chuẩn hóa địa chỉ", () => {
  assert.strictEqual(K.normalizeUrl("https://a.com/x?id=1&utm_source=fb&fbclid=2#top"), "https://a.com/x?id=1");
});
