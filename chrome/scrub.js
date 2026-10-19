// Bộ lọc dùng chung cho content script và trang cấu hình. Cùng luật che bí mật với app KimRag Mac
// (mac/Sources/KimRagCore/Scrub.swift), và danh sách trang mặc định không bao giờ đọc.
/* exported KIMRAG */
var KIMRAG = (() => {
  const MASK = "[đã che]";
  const SECRETS = [
    /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
    /\b(?:sk|pk|rk)-[A-Za-z0-9_-]{20,}/g,
    /\bhf_[A-Za-z0-9]{20,}/g,
    /\bAIza[0-9A-Za-z_-]{30,}/g,
    /\bgh[pousr]_[A-Za-z0-9]{30,}/g,
    /\bglpat-[A-Za-z0-9_-]{20,}/g,
    /\bxox[abprs]-[A-Za-z0-9-]{10,}/g,
    /\bAKIA[0-9A-Z]{16}\b/g,
    /\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}/g,
  ];
  const ASSIGNED = /\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)(['"]?)[^\s'"]{8,}/gi;
  const CARD = /\b\d(?:[ -]?\d){12,18}\b/g;

  function luhn(digits) {
    let sum = 0;
    for (let i = 0; i < digits.length; i++) {
      let d = digits[digits.length - 1 - i];
      if (i % 2 === 1) { d *= 2; if (d > 9) d -= 9; }
      sum += d;
    }
    return sum % 10 === 0;
  }

  function scrub(text) {
    let out = text;
    for (const re of SECRETS) out = out.replace(re, MASK);
    out = out.replace(ASSIGNED, (_, k, sep) => `${k}${sep}${MASK}`);
    return out.replace(CARD, (m) => {
      const digits = [...m].filter((c) => c >= "0" && c <= "9").map(Number);
      return digits.length >= 13 && luhn(digits) ? MASK : m;
    });
  }

  // Pages never read: mail, banks, passwords, sign-in pages, internal systems, machines on a private network.
  // One domain per entry; subdomains match too. Add your own (your company's intranet, for example) on the
  // extension's options page.
  const DEFAULT_BLOCKED = [
    "mail.google.com", "outlook.live.com", "outlook.office.com", "outlook.office365.com", "mail.yahoo.com",
    "mail.zoho.com", "mail.proton.me",
    "accounts.google.com", "passwords.google.com", "login.microsoftonline.com", "login.live.com",
    "appleid.apple.com", "vault.bitwarden.com", "1password.com",
    "vietinbank.vn", "vietcombank.com.vn", "vcbdigibank.vietcombank.com.vn", "techcombank.com",
    "techcombank.com.vn", "bidv.com.vn", "agribank.com.vn", "mbbank.com.vn", "tpb.vn", "acb.com.vn",
    "vpbank.com.vn", "sacombank.com", "sacombank.com.vn", "hdbank.com.vn", "vib.com.vn", "shb.com.vn",
    "paypal.com", "momo.vn", "zalopay.vn", "vnpay.vn",
    "meinvoice.vn", "misa.vn",
  ];

  function blocked(hostname, list) {
    const host = hostname.toLowerCase();
    if (host === "localhost" || host.endsWith(".local") || host.endsWith(".localhost") ||
        /^(?:10|127)\.\d+\.\d+\.\d+$/.test(host) || /^192\.168\.\d+\.\d+$/.test(host) ||
        /^172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+$/.test(host) || /^\[?[0-9a-f]*:[0-9a-f:]+\]?$/.test(host)) {
      return true;
    }
    return list.some((d) => {
      const domain = d.trim().toLowerCase();
      return domain && (host === domain || host.endsWith("." + domain));
    });
  }

  // Bỏ phần sau dấu # và các tham số chỉ dùng để đếm lượt quảng cáo, để một trang chỉ có một địa chỉ.
  function normalizeUrl(href) {
    const url = new URL(href);
    url.hash = "";
    for (const key of [...url.searchParams.keys()]) {
      if (/^(utm_|fbclid$|gclid$|mc_|igshid$|si$|ref_src$)/.test(key)) url.searchParams.delete(key);
    }
    return url.toString();
  }

  // Tiêu đề gọn một dòng: bỏ số thông báo chưa đọc ở đầu ("(13) ..."), gộp khoảng trắng, cắt bớt.
  function cleanTitle(title) {
    const t = scrub((title || "").replace(/\s+/g, " ").trim().replace(/^\(\d+\+?\)\s+/, ""));
    return t.length > 200 ? t.slice(0, 199).trimEnd() + "…" : t;
  }

  return { MASK, scrub, blocked, normalizeUrl, cleanTitle, DEFAULT_BLOCKED };
})();

if (typeof module === "object") module.exports = KIMRAG;
