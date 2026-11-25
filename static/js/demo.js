// The two-user demo: an opt-in second pane, logged in as a second user with its own
// ApiClient (token in sessionStorage under "demo."), running its own NoteSession on
// the note open in the main pane. Both panes are the same NotePane code; only the
// client differs. Its requests appear in the shared request log under that user's name.

import { ApiClient, ApiError, describeError } from './api.js';
import { $, remember } from './dom.js';
import { NotePane } from './note-pane.js';

export class Demo {
  // mainClient / mainPane: the logged-in user's client and note pane.
  // getTeam(): the team selected in the main page. onMembersChanged(): refresh members.
  constructor({ mainClient, mainPane, getTeam, onMembersChanged }) {
    this.mainClient = mainClient;
    this.mainPane = mainPane;
    this.getTeam = getTeam;
    this.onMembersChanged = onMembersChanged;
    this.enabled = false;
    this.client = new ApiClient({ prefix: 'demo', tone: 'b', onUnauthorized: () => this.render() });
    this.pane = new NotePane($('demo-pane'), this.client);

    const form = $('demo-login-form');
    form.elements.email.value = remember.get('demo.email');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      this.run(() => this.login(false));
    });
    $('demo-signup').addEventListener('click', () => this.run(() => this.login(true)));
    $('demo-logout').addEventListener('click', () => this.logout());
    $('demo-open').addEventListener('click', () => this.run(() => this.openMainNote()));
    $('demo-add').addEventListener('click', () => this.run(() => this.addToTeam()));
    $('demo-toggle').addEventListener('change', (e) => (e.target.checked ? this.enable() : this.disable()));
  }

  // Errors from the demo's own controls are shown in the demo pane.
  async run(fn) {
    this.pane.hideMessage();
    try {
      await fn();
    } catch (e) {
      if (e.name !== 'AbortError') this.pane.showMessage(describeError(e), 'error');
    }
  }

  // After a page reload: turn the demo back on if it was on in this tab.
  restore() {
    if (sessionStorage.getItem('demo.on') === '1') this.enable();
  }

  async enable() {
    this.enabled = true;
    sessionStorage.setItem('demo.on', '1');
    this.render();
    if (!this.client.loggedIn) return;
    await this.run(async () => {
      await this.client.refreshUser();
      this.render();
      await this.openMainNote(true);
    });
  }

  disable() {
    this.enabled = false;
    sessionStorage.removeItem('demo.on');
    this.pane.close();
    this.pane.hideMessage();
    this.render();
  }

  render() {
    $('demo-toggle').checked = this.enabled;
    $('demo-section').hidden = !this.enabled;
    $('note-area').classList.toggle('two-up', this.enabled);
    const loggedIn = this.client.loggedIn;
    $('demo-login-form').hidden = loggedIn;
    $('demo-session').hidden = !loggedIn;
    if (loggedIn) {
      $('demo-name').textContent = this.client.user.name;
      $('demo-email').textContent = this.client.user.email;
    } else {
      this.pane.close();
    }
  }

  async login(signup) {
    const { name, email, password } = Object.fromEntries(new FormData($('demo-login-form')));
    remember.set('demo.email', email);
    if (signup) await this.client.signup(name, email, password);
    else await this.client.login(email, password);
    $('demo-login-form').elements.password.value = '';
    this.render();
    await this.openMainNote(true);
  }

  logout() {
    this.pane.close();
    this.pane.hideMessage();
    this.client.logout();
    this.render();
  }

  // The main pane opened a note: open it here too.
  follow(team, noteId) {
    if (this.enabled && this.client.loggedIn) this.run(() => this.open(team, noteId));
  }

  // quiet: say nothing if the main pane has no note open (used after login/enable).
  async openMainNote(quiet = false) {
    const main = this.mainPane.session;
    if (!main.note) {
      if (!quiet) this.pane.showMessage('Open a note in the main pane first.', 'info');
      return;
    }
    await this.open(main.team, main.note.id);
  }

  // Opens the note as the second user, with that user's own role in the team.
  async open(team, noteId) {
    let ownTeam;
    try {
      ownTeam = await this.client.request('GET', `/teams/${team.id}`);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 404)) throw e;
      this.pane.close();
      this.pane.showMessage(`${this.client.user.name} is not a member of ${team.name}. `
        + `Add ${this.client.user.email} under Members, or press "Add to the main user's team".`, 'info');
      return;
    }
    await this.pane.open(ownTeam, noteId);
  }

  // Convenience: the main user (who must be an admin) adds the second user as a member.
  async addToTeam() {
    const team = this.getTeam();
    if (!team) {
      this.pane.showMessage('Select a team in the main page first.', 'info');
      return;
    }
    await this.mainClient.request('POST', `/teams/${team.id}/members`, { email: this.client.user.email, role: 'member' });
    this.onMembersChanged();
    await this.openMainNote(true);
  }
}
