// The client protocol for one open note (docs/API_CONTRACT.md, "Client protocol"):
// viewing -> acquiring -> editing -> (lock-lost) -> viewing, with the lock renewal timer
// and the long poll. It knows nothing about the DOM: a pane renders it after every
// onUpdate and feeds the user's typing into `draft`.
//
// The page makes one NoteSession per pane, each with its own ApiClient, so the
// two-user demo runs two independent copies of exactly this code.
//
// Every method that awaits captures `seq` first and does nothing afterwards if it
// changed: close() bumps it, so a slow response can never act on a note that was
// closed or replaced in the meantime.

import { ApiError, ifMatch } from './api.js';
import { time } from './dom.js';
import { Watcher } from './watcher.js';

export class NoteSession {
  // onUpdate(): state changed, re-render. onError(e): a failure no click is waiting
  // for (a timer-driven renewal). onClosed(message): the note went away under us.
  constructor(client, { onUpdate, onError, onClosed }) {
    this.client = client;
    this.onUpdate = onUpdate;
    this.onError = onError;
    this.onClosed = onClosed;
    this.team = null;          // Team, includes this user's role
    this.note = null;          // last known server state of the open note
    this.editing = false;
    this.editVersion = null;   // version the draft is based on; sent as If-Match with PATCH
    this.editStarting = false; // startEdit() is between its click and its last await
    this.lockLost = false;     // editing, but the lock is no longer ours; renewal stopped
    this.lostToIdle = false;   // the lock was lost because a renewal was refused as lock_idle
    this.typedSinceRenewal = false; // sent as `active` with the next renewal
    this.renewTimer = null;
    this.renewInfo = '';
    this.lastChange = '';
    this.seq = 0;
    // The editor's text. Only replaced from the server while not editing, or when an
    // edit starts; draftRev tells the pane when to copy it into the inputs.
    this.draft = { title: '', body: '' };
    this.draftRev = 0;
    this.watcher = new Watcher(client, {
      onChange: (change, signal) => this.onChange(change, signal),
      onGone: () => {
        this.close();
        this.onClosed('The open note was deleted, or this user is no longer in its team.');
      },
      onStatus: () => this.onUpdate(),
    });
  }

  get path() {
    return `/teams/${this.team.id}/notes/${this.note.id}`;
  }

  // The state names of the contract's client protocol table.
  get phase() {
    if (!this.note) return 'closed';
    if (this.editStarting) return 'acquiring';
    if (!this.editing) return 'viewing';
    return this.lockLost ? 'lock-lost' : 'editing';
  }

  get lockIsMine() {
    const lock = this.note && this.note.lock;
    return Boolean(lock && this.client.user && lock.user_id === this.client.user.id);
  }

  // Which buttons make sense now. Edit stays usable after a lost lock so the user can
  // re-take it. Save and Done wait while startEdit() is in flight.
  get can() {
    const open = Boolean(this.note);
    return {
      edit: open && !this.editStarting && !(this.editing && !this.lockLost),
      save: open && this.editing && !this.editStarting,
      done: open && this.editing && !this.editStarting,
      breakLock: open && !this.editing && Boolean(this.note.lock) && this.team.role === 'admin',
      other: open,
    };
  }

  // Derived from state rather than from each event, so a poll that lands just before
  // this session's own save response does not leave a stale warning behind.
  get warnings() {
    const warnings = [];
    if (!this.editing) return warnings;
    if (this.note.version !== this.editVersion) {
      warnings.push(`Someone saved version ${this.note.version} while you were editing. Save will get precondition_failed.`);
    }
    if (this.lockLost && this.lostToIdle) {
      warnings.push('The lock was released because you had not typed for a while, so others can edit the note. '
        + 'Your unsaved text is kept: press Edit to take the lock again and keep editing it, or Done to discard it.');
    } else if (this.lockLost) {
      warnings.push('You no longer hold the lock, so it is no longer being renewed and Save will be refused. '
        + 'Your text is kept: press Edit to re-take the lock and keep editing it, or Done to discard it.');
    }
    return warnings;
  }

  /* ---------- open, close, refresh ---------- */

  async open(team, noteId) {
    this.close();
    // Two quick Opens overlap across the await. Only the newest may show its note and
    // start a watch loop.
    const seq = this.seq;
    const note = await this.client.request('GET', `/teams/${team.id}/notes/${noteId}`);
    if (seq !== this.seq) return;
    this.team = team;
    this.lastChange = 'none seen yet';
    this.watcher.reset();
    this.apply(note);
    this.startWatch();
  }

  // Stops the poll and the renewals. It does not release the lock: the 60 second lease
  // lets it lapse, which is also how the other user sees lock expiry.
  close() {
    this.seq++;
    this.watcher.stop();
    this.stopEditing();
    this.editStarting = false;
    this.note = null;
    this.onUpdate();
  }

  startWatch() {
    this.watcher.start(this.path, () => this.note.tag);
  }

  // The draft is the one thing a refresh must never clobber.
  apply(note) {
    this.note = note;
    if (!this.editing) this.setDraft(note);
    this.checkLockHeld();
    this.onUpdate();
  }

  setDraft(note) {
    this.draft = { title: note.title, body: note.body };
    this.draftRev++;
  }

  // The pane calls this on every keystroke in the editor. The server cannot see typing,
  // so this flag is the only thing that keeps an editor's lock from going idle.
  typed() {
    this.typedSinceRenewal = true;
  }

  async reload() {
    const seq = this.seq;
    const note = await this.client.request('GET', this.path);
    if (seq === this.seq) this.apply(note);
  }

  // A 200 from the long poll. While editing, only the tag, version and lock are taken
  // from it (the draft stays); otherwise the note is re-read through the normal path.
  async onChange(change, signal) {
    const seen = `${time(Date.now())}: tag ${change.tag}`;
    if (this.editing) {
      Object.assign(this.note, change);
      this.checkLockHeld();
      this.lastChange = `${seen}. Editor text kept.`;
      this.onUpdate();
      return;
    }
    const note = await this.client.request('GET', this.path, undefined, signal);
    if (signal.aborted) return;
    this.lastChange = `${seen}. View refreshed.`;
    this.apply(note);
  }

  /* ---------- lock and edit ---------- */

  // Called whenever fresh server state arrives (poll, reload, save). Once the lock is
  // seen not to be ours (an admin broke it, or it expired), stop renewing: a renewal
  // would only be refused, and the user should see it now.
  checkLockHeld() {
    if (!this.editing || this.lockLost) return;
    if (!this.lockIsMine) this.markLockLost();
  }

  // Edit mode stays on with the draft untouched, but renewal stops.
  markLockLost() {
    this.stopRenewing();
    this.lockLost = true;
    this.renewInfo = 'stopped: lock lost';
  }

  // Also the way back after a lost lock: if still editing, it only re-takes the lock and
  // keeps the draft and editVersion, never reloading over them. If someone saved in the
  // meantime, the version warning shows and Save gets precondition_failed, so nothing is
  // overwritten silently in either direction.
  async startEdit() {
    // Guard before the first await: a double click would otherwise start two timers.
    if (!this.can.edit) return;
    const seq = this.seq;
    const path = this.path;
    this.editStarting = true;
    this.onUpdate();
    try {
      const lock = await this.client.request('PUT', `${path}/lock`);
      if (seq !== this.seq) return;
      if (this.editing) {
        this.lockLost = false;
        this.lostToIdle = false;
        this.note.lock = lock;
        this.startRenewing(lock);
      } else {
        // Re-read after taking the lock so the editor starts from the newest saved text.
        const note = await this.client.request('GET', path);
        if (seq !== this.seq) return;
        this.editing = true;
        this.editVersion = note.version;
        this.setDraft(note);
        this.apply(note);
        // The lock can already be gone again by the time the GET answers.
        if (!this.lockLost) this.startRenewing(lock);
      }
    } finally {
      if (seq === this.seq) {
        this.editStarting = false;
        this.onUpdate();
      }
    }
  }

  // The server says how often to renew (renew_after_seconds, a third of the lease).
  startRenewing(lock) {
    this.stopRenewing();
    this.typedSinceRenewal = false; // taking the lock already counted as activity
    const every = lock.renew_after_seconds;
    this.renewInfo = `every ${every} s; lease until ${time(lock.expires_at)}`;
    this.renewTimer = setInterval(() => this.renew().catch(this.onError), every * 1000);
  }

  stopRenewing() {
    clearInterval(this.renewTimer);
    this.renewTimer = null;
  }

  async renew() {
    // If the timer was stopped while a request was in flight (lock lost, Done, Close,
    // another note opened), the result belongs to a renewal that no longer exists.
    const timer = this.renewTimer;
    const seq = this.seq;
    const stillRenewing = () => timer !== null && this.renewTimer === timer;
    // Typing from here on belongs to the next renewal.
    const active = this.typedSinceRenewal;
    this.typedSinceRenewal = false;
    try {
      // renew=true only extends a lock we still hold; it never takes a free one, so a
      // renewal cannot re-acquire a lock that was broken, ran out or went idle.
      const lock = await this.client.request('PUT', `${this.path}/lock?renew=true&active=${active}`);
      if (!stillRenewing()) return;
      this.note.lock = lock;
      this.renewInfo = `every ${lock.renew_after_seconds} s; renewed at ${time(Date.now())}, lease until ${time(lock.expires_at)}`;
      this.onUpdate();
    } catch (e) {
      // The server wakes the long poll before it answers, so the poll may already have
      // marked the lock lost and stopped this timer. The idle reason still applies.
      if (e instanceof ApiError && e.code === 'lock_idle' && seq === this.seq && this.editing) {
        this.markLockLost();
        this.lostToIdle = true;
        this.renewInfo = 'stopped: lock released for inactivity';
        this.onUpdate();
        return;
      }
      if (!stillRenewing()) return;
      // lock_required or note_locked: the lock is gone. Anything else (network, 503): we
      // can no longer be sure we hold it. Either way treat it as lost so Edit is offered
      // again (it renews the lock if it is in fact still ours).
      this.markLockLost();
      this.renewInfo = `stopped: renewal failed (${e.code || e.message})`;
      this.onUpdate();
      throw e;
    }
  }

  async save() {
    const seq = this.seq;
    const sent = { title: this.draft.title, body: this.draft.body };
    const version = this.editVersion;
    let note;
    try {
      note = await this.client.request('PATCH', this.path, sent, undefined, ifMatch(version));
    } catch (e) {
      if (seq !== this.seq || !(e instanceof ApiError)) throw e;
      if (e.code === 'precondition_failed' && e.error.note) {
        const current = e.error.note;
        // The contract's retry rule: our own earlier attempt landed and its response was lost.
        const landed = current.version === version + 1
          && current.title === sent.title.trim() && current.body === sent.body;
        this.apply(current);
        if (!landed) throw e;
        note = current;
      } else {
        // note_locked: someone else holds it. lock_required: our lease ran out; this page
        // leaves the re-take to the Edit button. Either way the lock is not ours now.
        if (e.code === 'note_locked' || e.code === 'lock_required') {
          this.markLockLost();
          this.onUpdate();
        }
        throw e;
      }
    }
    if (seq !== this.seq) return;
    this.editVersion = note.version;
    this.apply(note);
  }

  stopEditing() {
    this.stopRenewing();
    this.editing = false;
    this.lockLost = false;
    this.lostToIdle = false;
    this.editVersion = null;
    this.renewInfo = '';
  }

  async done() {
    const seq = this.seq;
    // After a lost lock there is nothing of ours to release: a member's DELETE would get
    // 403, and an admin's would break the new holder's lock.
    const release = !this.lockLost;
    // Leave edit mode before the DELETE. That stops renewal, so no renewal is refused
    // after the release, and the poll (which the server wakes before answering the
    // DELETE) does not report our own unlock as a lost lock. If the DELETE fails, the
    // lock lapses with its lease, as on Close.
    this.stopEditing();
    this.onUpdate();
    try {
      if (release) await this.client.request('DELETE', `${this.path}/lock`);
    } finally {
      // Replace the discarded draft with the server's text, whether or not the DELETE worked.
      if (seq === this.seq) await this.reload();
    }
  }

  async breakLock() {
    await this.client.request('DELETE', `${this.path}/lock`);
    await this.reload();
  }

  // Returns true if the note was deleted (the caller confirms with the user first).
  async deleteNote() {
    const seq = this.seq;
    // The server wakes this note's long poll before the DELETE response arrives. Stop the
    // poll first so its 404 is not mistaken for someone else deleting the note.
    this.watcher.stop('paused while deleting');
    try {
      // The version last seen, from a read or a poll: a delete is refused (412) if the
      // note changed since, so nobody deletes text they have not seen.
      await this.client.request('DELETE', this.path, undefined, undefined, ifMatch(this.note.version));
    } catch (e) {
      // Still open (403, 409 note_locked, 412, network): resume watching. Using the last seen
      // tag means any change made during the pause is reported at once.
      if (seq === this.seq) this.startWatch();
      throw e;
    }
    if (seq !== this.seq) return false;
    this.close();
    return true;
  }
}
