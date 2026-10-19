// Where the sensor keeps its settings and private files.
//
//   ~/Library/Application Support/kim-rag/zalo.json        rules Kim edits: which chats to keep
//   ~/Library/Application Support/kim-rag/zalo/            private, mode 700: login session, state, queue
//
// The server address and token default to the ones in mac.json, so the secret lives in one place.
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const HOME = process.env.KIMRAG_HOME || path.join(os.homedir(), "Library", "Application Support", "kim-rag");
const PRIVATE = path.join(HOME, "zalo");

const PATHS = {
  config: path.join(HOME, "zalo.json"),
  mac: path.join(HOME, "mac.json"),
  credentials: path.join(PRIVATE, "credentials.json"),
  state: path.join(PRIVATE, "state.json"),
  queue: path.join(PRIVATE, "queue.jsonl"),
  qr: path.join(PRIVATE, "qr.png"),
};

const DEFAULTS = {
  users: true,          // direct chats: keep
  groups: [],           // groups to keep in full, by id or exact name; other groups are only counted
  blocked: [],          // people or groups never recorded at all, by id or exact name
  flushEverySeconds: 15,
  contactsEveryHours: 24,
};

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch (err) {
    if (err.code === "ENOENT") return fallback;
    throw new Error(`${file}: ${err.message}`);
  }
}

function writePrivate(file, data) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n", { mode: 0o600 });
  fs.renameSync(tmp, file);
}

function load() {
  fs.mkdirSync(PRIVATE, { recursive: true, mode: 0o700 });
  fs.chmodSync(PRIVATE, 0o700);
  if (!fs.existsSync(PATHS.config)) {
    fs.writeFileSync(PATHS.config, JSON.stringify(DEFAULTS, null, 2) + "\n", { mode: 0o600 });
  }
  const own = readJson(PATHS.config, {});
  const mac = readJson(PATHS.mac, {});
  return {
    ...DEFAULTS,
    ...own,
    server: own.server || mac.server || "http://127.0.0.1:8000",
    token: own.token || mac.token || "",
  };
}

module.exports = { HOME, PRIVATE, PATHS, DEFAULTS, load, readJson, writePrivate };
