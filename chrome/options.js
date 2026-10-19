/* global chrome, KIMRAG */
const $ = (id) => document.getElementById(id);
const DEFAULTS = { server: "http://127.0.0.1:8000", token: "", blocked: KIMRAG.DEFAULT_BLOCKED };

async function load() {
  const cfg = await chrome.storage.sync.get(DEFAULTS);
  $("blocked").value = cfg.blocked.join("\n");
  $("server").value = cfg.server;
  $("token").value = cfg.token;
  showStatus();
}

async function showStatus() {
  const { queue = [], lastSent, lastError } = await chrome.storage.local.get(["queue", "lastSent", "lastError"]);
  const parts = [];
  if (lastError) parts.push(`Chưa gửi được (${lastError})`);
  else if (lastSent) parts.push(`Đã gửi lúc ${new Date(lastSent).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" })}`);
  else parts.push("Chưa gửi lần nào");
  if (queue.length) parts.push(`chờ ${queue.length}`);
  $("status").textContent = parts.join(" · ");
  $("dot").classList.toggle("warn", Boolean(lastError));
}

$("save").addEventListener("click", async () => {
  const blocked = $("blocked").value.split("\n").map((s) => s.trim().toLowerCase()).filter(Boolean);
  await chrome.storage.sync.set({ blocked, server: $("server").value.trim().replace(/\/+$/, ""), token: $("token").value.trim() });
  $("saved").style.opacity = 1;
  setTimeout(() => { $("saved").style.opacity = 0; }, 1500);
});

$("reset").addEventListener("click", () => { $("blocked").value = KIMRAG.DEFAULT_BLOCKED.join("\n"); });

load();
setInterval(showStatus, 5000);
