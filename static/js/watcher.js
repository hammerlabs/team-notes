// The long-poll loop for one open note, with its state kept visible.
//
// GET .../changes?since=<tag>&wait=<s> in a loop: a 200 (the tag moved) is handed to
// onChange, a 204 (nothing changed within `wait`, or the server is shutting down) just
// polls again, a 404 (note deleted, or caller no longer a member) ends the loop through
// onGone. Anything else backs off for 2 s so a dead server cannot cause a busy loop.
//
// `status` and `history` describe what the loop is doing, for the pane to show:
// waiting (since when, for which tag), woke after N ms, 204 and re-polled, retrying.

import { ApiError, describeError } from './api.js';
import { sleep, time } from './dom.js';

const MAX_HISTORY = 8;

export class Watcher {
  constructor(client, { onChange, onGone, onStatus }) {
    this.client = client;
    this.onChange = onChange;   // async (change, signal) => void
    this.onGone = onGone;       // () => void
    this.onStatus = onStatus;   // () => void, status or history changed
    this.wait = 25;             // seconds the server may hold each poll (1 to 25 here)
    this.ctl = null;
    this.pollNo = 0;
    this.status = { phase: 'stopped', text: 'not watching' };
    this.history = [];          // newest first: { cls, text }
    this.historyRev = 0;        // bumped whenever history changes
  }

  // path: the note's URL. getTag: returns the tag of the last state the caller has seen.
  // Aborts any running loop first, so there is never more than one.
  start(path, getTag) {
    this.stop();
    this.ctl = new AbortController();
    this.loop(path, getTag, this.ctl.signal);
  }

  stop(text = 'not watching') {
    if (this.ctl) this.ctl.abort();
    this.ctl = null;
    this.setStatus({ phase: 'stopped', text });
  }

  reset() {
    this.stop();
    this.pollNo = 0;
    this.history = [];
    this.historyRev++;
    this.onStatus();
  }

  setStatus(status) {
    this.status = status;
    this.onStatus();
  }

  record(cls, text) {
    this.history.unshift({ cls, text: `${time(Date.now())}  ${text}` });
    this.history.length = Math.min(this.history.length, MAX_HISTORY);
    this.historyRev++;
  }

  async loop(path, getTag, signal) {
    while (!signal.aborted) {
      const n = ++this.pollNo;
      const tag = getTag();
      const started = performance.now();
      this.setStatus({ phase: 'waiting', n, tag, started });
      try {
        const params = new URLSearchParams({ since: tag, wait: this.wait });
        const change = await this.client.request('GET', `${path}/changes?${params}`, undefined, signal);
        if (signal.aborted) return;
        const ms = Math.round(performance.now() - started);
        if (change) {
          this.record('woke', `#${n} woke after ${ms} ms: tag ${tag} -> ${change.tag}`);
          this.setStatus({ phase: 'handling', text: `poll #${n} woke after ${ms} ms, handling the change` });
          await this.onChange(change, signal);
        } else {
          this.record('timeout', `#${n} 204 no change after ${(ms / 1000).toFixed(1)} s; polling again`);
        }
      } catch (e) {
        if (signal.aborted) return;
        if (e instanceof ApiError && e.status === 404) {
          this.record('failed', `#${n} 404: note gone or not a member; stopped`);
          this.ctl = null;
          this.setStatus({ phase: 'stopped', text: 'stopped (404)' });
          this.onGone();
          return;
        }
        this.record('failed', `#${n} failed: ${describeError(e)}; retrying in 2 s`);
        this.setStatus({ phase: 'retrying', text: 'poll failed, retrying in 2 s' });
        await sleep(2000);
      }
    }
  }
}
