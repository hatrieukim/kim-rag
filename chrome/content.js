// Chạy trong mỗi trang. Đếm thời gian bạn thật sự ở trang (tab đang hiện, cửa sổ đang được chọn, có
// động chuột hay bàn phím trong 3 phút gần nhất). Ở đủ 30 giây thì lấy phần chữ chính của bài bằng
// Readability. Không đọc ô nhập liệu, không đọc trang có ô mật khẩu, không đọc trang trong danh sách chặn.
/* global KIMRAG, Readability, chrome */
(() => {
  const READ_AFTER_S = 30;
  const IDLE_S = 180;
  const REPORT_EVERY_S = 60;     // gửi thời gian đã ở trang theo từng phút, để tab bị đóng đột ngột không mất hết
  const MIN_VISIT_S = 5;
  const MIN_TEXT = 300;
  const MAX_TEXT = 100000;
  const MAX_SELECTION = 5000;

  let blockedList = KIMRAG.DEFAULT_BLOCKED;
  let url = null;
  let unreported = 0;
  let active = 0;
  let since = new Date();
  let captured = false;
  let lastInput = Date.now();

  const send = (msg) => {
    try { chrome.runtime.sendMessage(msg).catch(() => {}); } catch { /* extension vừa được nạp lại */ }
  };
  const isBlocked = () => KIMRAG.blocked(location.hostname, blockedList);
  const title = () => KIMRAG.cleanTitle(document.title);

  function engaged() {
    return document.visibilityState === "visible" && document.hasFocus() && Date.now() - lastInput < IDLE_S * 1000;
  }

  function report() {
    if (url && unreported >= MIN_VISIT_S && !isBlocked()) {
      send({ kind: "visit", ts: since.toISOString(), url, title: title(), domain: location.hostname,
             seconds: unreported });
    }
    unreported = 0;
    since = new Date();
  }

  function startPage() {
    url = KIMRAG.normalizeUrl(location.href);
    active = 0;
    unreported = 0;
    captured = false;
    since = new Date();
  }

  // Chữ của từng khối (đoạn, tiêu đề, mục danh sách...), cách nhau một dòng trống, để giữ ranh giới đoạn văn.
  function blocksOf(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const selector = "h1,h2,h3,h4,h5,h6,p,li,pre,blockquote,figcaption,td,th,dd,dt";
    const out = [];
    for (const el of doc.body.querySelectorAll(selector)) {
      if (el.querySelector(selector)) continue;   // khối cha: chữ đã nằm trong khối con
      const text = el.textContent.replace(/[ \t ]+/g, " ").trim();
      if (text) out.push(text);
    }
    return out.join("\n\n");
  }

  function capture() {
    captured = true;
    if (isBlocked() || document.querySelector("input[type=password]")) return;
    let article;
    try {
      article = new Readability(document.cloneNode(true), { charThreshold: MIN_TEXT }).parse();
    } catch { return; }
    if (!article || !article.content) return;
    const text = KIMRAG.scrub(blocksOf(article.content) || (article.textContent || "").trim());
    if (text.length < MIN_TEXT) return;
    send({
      kind: "read", ts: new Date().toISOString(), url, domain: location.hostname,
      title: KIMRAG.cleanTitle(article.title || document.title),
      site: article.siteName || null, byline: article.byline || null, lang: article.lang || document.documentElement.lang || null,
      chars: text.length, text: text.slice(0, MAX_TEXT),
    });
  }

  for (const ev of ["mousemove", "keydown", "scroll", "wheel", "click", "touchstart"]) {
    addEventListener(ev, () => { lastInput = Date.now(); }, { passive: true, capture: true });
  }

  document.addEventListener("copy", () => {
    const el = document.activeElement;
    if (isBlocked() || (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)))) return;
    const text = (getSelection() || "").toString().trim();
    if (text.length < 20) return;
    send({ kind: "selection", ts: new Date().toISOString(), url, domain: location.hostname, title: title(),
           text: KIMRAG.scrub(text).slice(0, MAX_SELECTION) });
  }, true);

  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") report(); });
  addEventListener("pagehide", report);

  setInterval(() => {
    // Trang một-trang (YouTube, GitHub...) đổi địa chỉ mà không tải lại: tính như sang trang mới.
    if (KIMRAG.normalizeUrl(location.href) !== url) {
      report();
      startPage();
    }
    if (!engaged()) return;
    active += 1;
    unreported += 1;
    if (unreported >= REPORT_EVERY_S) report();
    if (!captured && active >= READ_AFTER_S) capture();
  }, 1000);

  chrome.storage.sync.get({ blocked: KIMRAG.DEFAULT_BLOCKED }, (cfg) => { blockedList = cfg.blocked; });
  startPage();
})();
