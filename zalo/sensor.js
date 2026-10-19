#!/usr/bin/env node
// kim-rag Zalo sensor (product 10). Listens to Kim's Zalo account through zca-js and sends every message
// to the kim-rag server as events. It only listens: it never sends a message, a reaction or a "seen".
//
//   node sensor.js --login    show a QR code, scan it with the Zalo app on the phone, save the session
//   node sensor.js            listen (run by the LaunchAgent com.kim.kimrag.zalo)
//
// Zalo allows one web session per account: opening Zalo in a browser closes this listener. Use the Zalo
// app on the Mac instead.
"use strict";

const { execFileSync } = require("node:child_process");
const { Zalo, ThreadType } = require("zca-js");
const config = require("./config");
const { Queue } = require("./queue");
const { Backoff } = require("./backoff");
const N = require("./normalize");

const TICK_MS = 5 * 60 * 1000;
const GROUP_INFO_BATCH = 50;
const AGENT = "com.kim.kimrag.zalo";

const log = (...args) => console.log(new Date().toISOString(), ...args);

function zalo() {
  return new Zalo({ selfListen: true, checkUpdate: false, logging: false });
}

// ---------------------------------------------------------------- login

async function login() {
  let tries = 0;
  const api = await zalo().loginQR({ qrPath: config.PATHS.qr }, async (event) => {
    switch (event.type) {
      case 0:   // QRCodeGenerated
        await event.actions.saveToFile(config.PATHS.qr);
        require("node:fs").chmodSync(config.PATHS.qr, 0o600);
        log(`QR code saved to ${config.PATHS.qr}. Scan it in the Zalo app on your phone (QR icon next to search).`);
        try { execFileSync("open", [config.PATHS.qr]); } catch { /* not on a desktop session */ }
        break;
      case 1:   // QRCodeExpired
        if (++tries < 3) { log("QR code expired, making a new one"); event.actions.retry(); }
        else { log("QR code expired three times, giving up"); event.actions.abort(); }
        break;
      case 2:   // QRCodeScanned
        log(`Scanned by ${event.data.display_name}. Confirm on the phone.`);
        break;
      case 3:   // QRCodeDeclined
        log("Login declined on the phone");
        event.actions.abort();
        break;
      case 4:   // GotLoginInfo
        config.writePrivate(config.PATHS.credentials, {
          imei: event.data.imei, userAgent: event.data.userAgent, cookie: event.data.cookie,
          savedAt: new Date().toISOString(),
        });
        log("Session saved");
        break;
      default:
        break;
    }
  });
  try { require("node:fs").unlinkSync(config.PATHS.qr); } catch { /* already gone */ }   // single use
  if (!api) throw new Error("login did not finish");
  const state = config.readJson(config.PATHS.state, {});
  config.writePrivate(config.PATHS.state, { ...state, needsLogin: false, ownId: api.getOwnId() });
  log(`Logged in as ${api.getOwnId()}`);
  try {
    execFileSync("launchctl", ["kickstart", "-k", `gui/${process.getuid()}/${AGENT}`], { stdio: "ignore" });
    log(`Restarted ${AGENT}`);
  } catch {
    log(`${AGENT} is not installed yet; run zalo/install.sh`);
  }
}

// ---------------------------------------------------------------- listen

const looksOffline = (err) => /ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EAI_AGAIN|fetch failed|network/i.test(String(err && (err.code || err.message || err)));

async function run() {
  const cfg = config.load();
  const queue = new Queue(config.PATHS.queue, { server: cfg.server, token: cfg.token, log });
  const state = { lastIds: {}, names: {}, ...config.readJson(config.PATHS.state, {}) };
  const saveState = () => config.writePrivate(config.PATHS.state, state);

  const creds = config.readJson(config.PATHS.credentials, null);
  if (!creds) {
    log("No Zalo session. Run: cd zalo && npm run login");
    queue.push([N.status("needs_login", { reason: "no session" })]);
    await queue.flush();
    return;
  }

  let api;
  try {
    api = await zalo().login({ imei: creds.imei, userAgent: creds.userAgent, cookie: creds.cookie });
  } catch (err) {
    if (looksOffline(err)) throw err;     // exit non-zero, launchd tries again later
    log(`Zalo refused the saved session: ${err.message || err}. Run: cd zalo && npm run login`);
    state.needsLogin = true;
    saveState();
    queue.push([N.status("needs_login", { reason: String(err.message || err).slice(0, 200) })]);
    await queue.flush();
    return;
  }

  const ownId = String(api.getOwnId());
  state.ownId = ownId;
  state.needsLogin = false;
  try {
    config.writePrivate(config.PATHS.credentials, { ...creds, cookie: api.getCookie().toJSON().cookies,
                                                    savedAt: new Date().toISOString() });
  } catch (err) { log(`could not save the refreshed session: ${err.message}`); }
  saveState();
  log(`Logged in as ${ownId}`);

  const ctx = () => ({ config: cfg, ownId, names: state.names });

  // Names of people and groups. Groups in zalo.json may be listed by name, so this runs before listening.
  async function refreshContacts() {
    const events = [];
    try {
      for (const u of await api.getAllFriends()) {
        const name = u.displayName || u.zaloName;
        state.names[String(u.userId)] = name;
        events.push(N.fromContact("user", u.userId, name));
      }
      const ids = Object.keys((await api.getAllGroups()).gridVerMap || {});
      for (let i = 0; i < ids.length; i += GROUP_INFO_BATCH) {
        const info = await api.getGroupInfo(ids.slice(i, i + GROUP_INFO_BATCH));
        for (const [id, g] of Object.entries(info.gridInfoMap || {})) {
          state.names[id] = g.name;
          events.push(N.fromContact("group", id, g.name, { members: g.totalMember || null }));
        }
      }
      state.contactsAt = new Date().toISOString();
      saveState();
      queue.push(events);
      log(`contacts: ${events.filter(Boolean).length} people and groups`);
    } catch (err) {
      log(`contacts: could not refresh: ${err.message || err}`);
    }
  }

  function keep(message) {
    const event = N.fromMessage(message, ctx());
    const kind = message.type === ThreadType.Group ? "group" : "user";
    state.lastIds[kind] = N.laterId(state.lastIds[kind], String(message.data.msgId || ""));
    return event;
  }

  await refreshContacts();

  const listener = api.listener;
  let connected = false;
  let caughtUp = false;
  const backoff = new Backoff();
  let restartTimer = null;
  let timers = [];
  let stopping = false;

  listener.on("connected", () => {
    connected = true;
    caughtUp = false;
    backoff.connected();
    log("listening");
    queue.push([N.status("connected")]);
  });

  // The cipher key arrives right after connecting; old messages can only be decoded after it. Asking for
  // messages since the last one seen is what Zalo Web itself does when it reconnects.
  listener.on("cipher_key", () => {
    if (caughtUp) return;
    caughtUp = true;
    listener.requestOldMessages(ThreadType.User, state.lastIds.user || null);
    listener.requestOldMessages(ThreadType.Group, state.lastIds.group || null);
  });

  listener.on("message", (message) => queue.push([keep(message)]));
  listener.on("old_messages", (messages) => {
    queue.push(messages.map(keep));
    log(`caught up ${messages.length} messages`);
  });
  listener.on("undo", (undo) => queue.push([N.fromUndo(undo, ctx())]));
  listener.on("error", (err) => log(`listener error: ${err && (err.message || err)}`));

  listener.on("disconnected", () => backoff.disconnected());

  // Asks Zalo one cheap question. A network failure is not proof the session is gone.
  async function sessionAlive() {
    try { await api.fetchAccountInfo(); return true; } catch (err) { return looksOffline(err); }
  }

  listener.on("closed", async (code, reason) => {
    connected = false;
    if (stopping) return;   // our own listener.stop()
    const { delay, kicked, suspectDead } = backoff.next(code);
    if (suspectDead) {
      if (!(await sessionAlive())) {
        log("Zalo ended this session (logged out, or another login replaced it). Run: cd zalo && npm run login");
        state.needsLogin = true;
        queue.push([N.status("needs_login", { reason: "session ended", code })]);
        await stop("session ended");
        return;
      }
      backoff.sessionOk();
    }
    log(kicked ? `closed: another Zalo web session took over (${code}). Close Zalo in the browser; retrying in 30 minutes`
               : `closed (${code} ${reason || ""}), retrying in ${Math.round(delay / 1000)} s`);
    queue.push([N.status("closed", { code, reason: reason || null, kicked })]);
    clearTimeout(restartTimer);
    restartTimer = setTimeout(() => {
      try { listener.start({ retryOnClose: true }); } catch (err) { log(`restart failed: ${err.message}`); }
    }, delay + Math.floor(Math.random() * 5000));
  });

  listener.start({ retryOnClose: true });

  const flush = async () => {
    try {
      await queue.flush();
      saveState();
    } catch (err) { log(`flush: ${err.message}`); }
  };
  timers = [
    setInterval(flush, cfg.flushEverySeconds * 1000),
    setInterval(() => queue.push([N.status("tick", { connected, queued: queue.size() })]), TICK_MS),
    setInterval(refreshContacts, cfg.contactsEveryHours * 3600 * 1000),
  ];

  // Exits 0 either way: launchd restarts the sensor only after a crash, and `npm run login` restarts it
  // after a new login.
  async function stop(signal) {
    if (stopping) return;
    stopping = true;
    log(`stopping (${signal})`);
    timers.forEach(clearInterval);
    clearTimeout(restartTimer);
    try { listener.stop(); } catch { /* already closed */ }
    if (!state.needsLogin) queue.push([N.status("stopped")]);
    await Promise.race([flush(), new Promise((r) => setTimeout(r, 3000))]);
    process.exit(0);
  }
  process.on("SIGTERM", () => stop("SIGTERM"));
  process.on("SIGINT", () => stop("SIGINT"));
}

const main = process.argv.includes("--login") ? login : run;
main().catch((err) => {
  log(`fatal: ${err && (err.stack || err.message || err)}`);
  process.exit(1);
});
