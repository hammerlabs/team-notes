// A note pane: renders one NoteSession into a copy of <template id="note-pane-tpl">
// and turns its buttons into session calls. The page has one for the logged-in user and,
// in the two-user demo, a second one for the other user.

import { describeError } from './api.js';
import { $, el, time } from './dom.js';
import { NoteSession } from './note-session.js';

const PHASE_TEXT = {
  viewing: 'viewing',
  acquiring: 'acquiring the lock',
  editing: 'editing (lock held)',
  'lock-lost': 'lock lost',
};

export class NotePane {
  // container: element the pane is appended to; hidden while no note is open.
  // onDeleted(): this pane deleted its note. onOpened(team, noteId): a note was opened.
  constructor(container, client, { onDeleted, onOpened } = {}) {
    this.container = container;
    this.client = client;
    this.onDeleted = onDeleted || (() => {});
    this.onOpened = onOpened || (() => {});
    this.root = $('note-pane-tpl').content.firstElementChild.cloneNode(true);
    this.root.classList.add(`tone-${client.tone}`);
    container.append(this.root);
    this.renderedRev = -1;
    this.renderedHistoryRev = -1;

    this.session = new NoteSession(client, {
      onUpdate: () => this.render(),
      onError: (e) => this.showMessage(describeError(e), 'error'),
      onClosed: (message) => this.showMessage(message, 'info'),
    });

    // Typing goes straight into the session's draft; render() only writes it back when
    // the session replaced the draft (draftRev). It also marks the user active, which the
    // next lock renewal reports.
    this.f('title').addEventListener('input', (e) => { this.session.draft.title = e.target.value; this.session.typed(); });
    this.f('body').addEventListener('input', (e) => { this.session.draft.body = e.target.value; this.session.typed(); });
    this.f('wait').addEventListener('change', (e) => {
      // At least 1 s: wait=0 answers 204 at once, which would make this a busy loop.
      const wait = Math.min(25, Math.max(1, Number(e.target.value) || 25));
      e.target.value = wait;
      this.session.watcher.wait = wait;  // used from the next poll on
    });

    const s = this.session;
    this.bind('edit', () => s.startEdit().then(() => this.f('body').focus()));
    this.bind('save', () => s.save());
    this.bind('done', () => s.done());
    this.bind('break-lock', () => s.breakLock());
    this.bind('reload', () => s.reload());
    this.bind('close', () => s.close());
    this.bind('delete', async () => {
      if (!confirm(`Delete "${s.note.title}"?`)) return;
      if (await s.deleteNote()) this.onDeleted();
    });

    // The waiting time of the current poll ticks while it is held.
    setInterval(() => this.renderWatch(), 250);
    this.render();
  }

  f(name) {
    return this.root.querySelector(`[data-f="${name}"]`);
  }

  // Runs a session call for a button; any failure is shown in this pane.
  bind(name, fn) {
    this.f(name).addEventListener('click', async () => {
      this.hideMessage();
      try {
        await fn();
      } catch (e) {
        if (e.name !== 'AbortError') this.showMessage(describeError(e), 'error');
      }
    });
  }

  async open(team, noteId) {
    this.hideMessage();
    await this.session.open(team, noteId);
    if (this.session.note) this.onOpened(team, noteId);
  }

  close() {
    this.session.close();
  }

  showMessage(text, kind) {
    const msg = this.f('msg');
    msg.textContent = `${time(Date.now())}  ${text}`;
    msg.className = `pane-msg ${kind}`;
    msg.hidden = false;
    this.render();
  }

  hideMessage() {
    this.f('msg').hidden = true;
    this.render();
  }

  render() {
    const s = this.session;
    const n = s.note;
    const user = this.client.user;
    this.f('who').textContent = user ? user.name : 'not logged in';
    this.f('role').textContent = s.team ? `${s.team.role} of ${s.team.name}` : '';
    // A pane keeps its last message visible after the note closes (for example "deleted").
    this.container.hidden = !n && this.f('msg').hidden;
    this.f('body-area').hidden = !n;
    this.renderWatch();
    if (!n) return;

    const phase = s.phase;
    this.f('phase').textContent = s.lostToIdle ? 'lock released for inactivity' : PHASE_TEXT[phase];
    this.f('phase').className = `phase ${phase}`;
    if (this.renderedRev !== s.draftRev) {
      this.f('title').value = s.draft.title;
      this.f('body').value = s.draft.body;
      this.renderedRev = s.draftRev;
    }
    this.f('title').readOnly = !s.editing;
    this.f('body').readOnly = !s.editing;

    this.f('version').textContent = s.editing && s.editVersion !== n.version
      ? `${n.version} on server, editor holds ${s.editVersion}` : String(n.version);
    this.f('tag').textContent = n.tag;
    const lock = n.lock;
    this.f('lock').textContent = lock
      ? `Locked by ${lock.user_name}${s.lockIsMine ? ' (you)' : ''} until ${time(lock.expires_at)}`
      : 'Unlocked';
    this.f('lock').className = lock && !s.lockIsMine ? 'locked' : '';
    this.f('renewal').textContent = s.renewInfo || 'not renewing';
    this.f('changed').textContent = s.lastChange;
    this.f('warning').textContent = s.warnings.join(' ');

    const can = s.can;
    this.f('edit').disabled = !can.edit;
    this.f('save').disabled = !can.save;
    this.f('done').disabled = !can.done;
    this.f('break-lock').disabled = !can.breakLock;
  }

  renderWatch() {
    const w = this.session.watcher;
    const st = w.status;
    const watch = this.f('watch');
    if (st.phase === 'waiting') {
      const secs = ((performance.now() - st.started) / 1000).toFixed(1);
      watch.textContent = `poll #${st.n} waiting ${secs} s for a change to tag ${st.tag} (wait=${w.wait})`;
      watch.className = 'watch live';
    } else {
      watch.textContent = st.text;
      watch.className = st.phase === 'retrying' ? 'watch retrying' : 'watch';
    }
    const history = this.f('history');
    if (this.renderedHistoryRev !== w.historyRev) {
      this.renderedHistoryRev = w.historyRev;
      history.replaceChildren(...w.history.map((h) => el('li', { class: h.cls }, h.text)));
    }
  }
}
