// When to reconnect, and when to suspect that Zalo ended the session.
//
// A session that was logged out elsewhere still accepts the websocket, then closes it within milliseconds.
// So "connected" alone proves nothing: the wait only goes back to its minimum after a connection stayed up
// for a while, and several connections in a row that die at once mean the session is probably gone.
// Retrying fast in that state looks like a misbehaving client, which is what gets accounts locked.
"use strict";

const MIN_WAIT_MS = 30 * 1000;
const MAX_WAIT_MS = 15 * 60 * 1000;
const KICKED_WAIT_MS = 30 * 60 * 1000;   // another web session took over: do not fight it
const STABLE_MS = 60 * 1000;             // a connection this old was real
const SHORT_MS = 10 * 1000;              // a connection closed sooner than this was refused
const DEAD_AFTER = 5;                    // refused this many times in a row: check the session
const KICKED = new Set([3000, 3003]);    // zca-js CloseReason.DuplicateConnection, KickConnection

class Backoff {
  constructor() {
    this.wait = MIN_WAIT_MS;
    this.connectedAt = null;
    this.refused = 0;
  }

  connected(now = Date.now()) {
    this.connectedAt = now;
  }

  // Every socket close, including the retries zca-js makes on its own.
  disconnected(now = Date.now()) {
    if (this.connectedAt === null) return;
    const lived = now - this.connectedAt;
    this.connectedAt = null;
    if (lived >= STABLE_MS) {
      this.wait = MIN_WAIT_MS;
      this.refused = 0;
    } else if (lived < SHORT_MS) {
      this.refused += 1;
    }
  }

  // The listener gave up. How long to wait before starting it again, and whether to check the session first.
  next(code) {
    if (KICKED.has(code)) return { delay: KICKED_WAIT_MS, kicked: true, suspectDead: false };
    const delay = this.wait;
    this.wait = Math.min(this.wait * 2, MAX_WAIT_MS);
    return { delay, kicked: false, suspectDead: this.refused >= DEAD_AFTER };
  }

  // The session was checked and is fine: keep backing off, but stop suspecting it.
  sessionOk() {
    this.refused = 0;
  }
}

module.exports = { Backoff, MIN_WAIT_MS, MAX_WAIT_MS, KICKED_WAIT_MS, STABLE_MS, SHORT_MS, DEAD_AFTER };
