// Nhận sự kiện từ các trang, xếp hàng trong chrome.storage.local, gửi theo lô vào POST /events của kim-rag.
// Server tắt thì hàng đợi giữ lại, mỗi phút thử gửi lại. Tab ẩn danh không bao giờ được ghi.
/* global chrome */
const SOURCE = "chrome";
const MAX_QUEUE = 5000;
const BATCH = 100;
const DEFAULTS = { server: "http://127.0.0.1:8000", token: "" };

let chain = Promise.resolve();   // ghi hàng đợi lần lượt, không để hai lần ghi đè lên nhau
let flushing = false;

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}

async function toEvent(msg) {
  const { kind, ts, ...payload } = msg;
  // Cùng một bài, cùng nội dung thì cùng mã: đọc lại không tạo bản đôi.
  const uid = kind === "read"
    ? `chrome:read:${await sha256(`${payload.url}\n${payload.text}`)}`
    : `chrome:${crypto.randomUUID()}`;
  return { uid, source: SOURCE, type: kind, ts: ts || new Date().toISOString(), payload };
}

function enqueue(msg) {
  chain = chain.then(async () => {
    const event = await toEvent(msg);
    const { queue = [] } = await chrome.storage.local.get("queue");
    queue.push(event);
    await chrome.storage.local.set({ queue: queue.slice(-MAX_QUEUE) });
  }).catch((err) => console.warn("kim-rag: không xếp được sự kiện", err));
  chain.then(flush);   // gửi sau khi ghi xong, nhưng không nằm trong chuỗi ghi để flush còn ghi tiếp được
}

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    const cfg = await chrome.storage.sync.get(DEFAULTS);
    for (;;) {
      const { queue = [] } = await chrome.storage.local.get("queue");
      if (!queue.length) break;
      const batch = queue.slice(0, BATCH);
      const headers = { "Content-Type": "application/json" };
      if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
      const resp = await fetch(`${cfg.server}/events`, { method: "POST", headers, body: JSON.stringify(batch) });
      if (resp.ok || resp.status === 400 || resp.status === 422) {
        // 400 và 422: server từ chối nội dung, gửi lại cũng vậy, nên bỏ mẻ đó để hàng không kẹt mãi.
        if (!resp.ok) console.warn("kim-rag: server từ chối", resp.status, await resp.text());
        await (chain = chain.then(async () => {
          const { queue: now = [] } = await chrome.storage.local.get("queue");
          const sent = new Set(batch.map((e) => e.uid));
          await chrome.storage.local.set({ queue: now.filter((e) => !sent.has(e.uid)), lastSent: Date.now(), lastError: null });
        }));
      } else {
        await chrome.storage.local.set({ lastError: `HTTP ${resp.status}` });
        break;
      }
    }
  } catch (err) {
    await chrome.storage.local.set({ lastError: "server chưa chạy" });
  } finally {
    flushing = false;
  }
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!sender.tab || sender.tab.incognito || !msg || !["visit", "read", "selection"].includes(msg.kind)) return;
  enqueue(msg);
});

chrome.alarms.create("flush", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => { if (alarm.name === "flush") flush(); });
chrome.runtime.onStartup.addListener(flush);
