// Entry point: the single-user page (log in, teams, members, notes list) and its note
// pane. The two-user demo (demo.js) is opt-in and adds a second pane beside this one.
//
// Every user action goes through action(), so any error lands in the banner instead of
// being swallowed. Errors from note buttons are shown inside the note's pane instead.

import { ApiClient, describeError } from './api.js';
import { $, el, hideBanner, remember, showBanner, stamp } from './dom.js';
import { Demo } from './demo.js';
import { NotePane } from './note-pane.js';

const client = new ApiClient({ prefix: 'main', tone: 'a', onUnauthorized: () => logout() });
const state = {
  team: null,       // selected Team, includes the caller's role
  nextCursor: null, // from the last notes page; null on the last page
  query: null,      // { q, limit } of the current walk through the notes list
};

const pane = new NotePane($('note-section'), client, {
  onDeleted: () => action(() => loadNotes())(),
  onOpened: (team, noteId) => demo.follow(team, noteId),
});
const demo = new Demo({
  mainClient: client,
  mainPane: pane,
  getTeam: () => state.team,
  onMembersChanged: () => action(loadMembers)(),
});

function action(fn) {
  return async (event) => {
    if (event && event.type === 'submit') event.preventDefault();
    try {
      await fn(event);
    } catch (e) {
      if (e.name !== 'AbortError') showBanner(describeError(e), 'error');
    }
  };
}

/* ---------- session ---------- */

function showView() {
  const loggedIn = client.loggedIn;
  $('login-view').hidden = loggedIn;
  $('app-view').hidden = !loggedIn;
  if (loggedIn) {
    $('me-name').textContent = client.user.name;
    $('me-email').textContent = client.user.email;
  }
}

async function login(email, password) {
  remember.set('main.email', email);
  await client.login(email, password);
  showView();
  await loadTeams();
}

async function signup(form) {
  const { name, email, password } = Object.fromEntries(new FormData(form));
  remember.set('main.email', email);
  await client.signup(name, email, password);
  showView();
  await loadTeams();
}

// Also called by the client after a 401, which has already cleared its session.
function logout() {
  demo.disable();
  deselectTeam();
  client.logout();
  $('teams').replaceChildren();
  showView();
}

/* ---------- teams and members ---------- */

const teamPath = () => `/teams/${state.team.id}`;

async function loadTeams() {
  const teams = await client.request('GET', '/teams');
  // Pick up a role change, or drop the selection if the caller was removed.
  const current = state.team && teams.find((t) => t.id === state.team.id);
  if (state.team && !current) deselectTeam();
  if (current) state.team = current;
  $('teams').replaceChildren(...teams.map((t) =>
    el('li', { class: state.team && t.id === state.team.id ? 'selected' : '' },
      el('span', { class: 'grow' }, t.name, ' ', el('span', { class: 'muted' }, t.role)),
      el('button', { type: 'button', onclick: action(() => selectTeam(t)) }, 'Select'))));
}

async function createTeam(form) {
  const { name } = Object.fromEntries(new FormData(form));
  const team = await client.request('POST', '/teams', { name });
  form.reset();
  await selectTeam(team);
}

async function selectTeam(team) {
  pane.close();
  state.team = team;
  $('team-name').textContent = team.name;
  $('team-role').textContent = `(you are ${team.role})`;
  $('members-section').hidden = false;
  $('notes-section').hidden = false;
  $('search-form').reset();
  await Promise.all([loadTeams(), loadMembers(), loadNotes()]);
}

function deselectTeam() {
  pane.close();
  state.team = null;
  $('members-section').hidden = true;
  $('notes-section').hidden = true;
}

async function loadMembers() {
  if (!state.team) return;
  const members = await client.request('GET', `${teamPath()}/members`);
  $('members').replaceChildren(...members.map((m) =>
    el('li', {},
      el('span', { class: 'grow' }, `${m.name} `, el('span', { class: 'muted' }, `${m.email} · ${m.role} · joined ${stamp(m.joined_at)}`)),
      el('button', { type: 'button', onclick: action(() => removeMember(m)) }, m.user_id === client.user.id ? 'Leave' : 'Remove'))));
}

async function addMember(form) {
  const { email, role } = Object.fromEntries(new FormData(form));
  await client.request('POST', `${teamPath()}/members`, { email, role });
  form.elements.email.value = '';
  await loadMembers();
}

async function removeMember(member) {
  await client.request('DELETE', `${teamPath()}/members/${member.user_id}`);
  if (member.user_id === client.user.id) await loadTeams();
  else await loadMembers();
}

/* ---------- notes list ---------- */

// Loads the page after `cursor`, or the first page without one. Every caller but
// "Next page" starts from the first page: after a new search, a team change or a
// write, the old cursor names a place in a list the user is no longer looking at.
// A first page reads q and limit from the form; "Next page" reuses the ones of its walk,
// because a cursor marks a place only in the list it came from, and the form may have
// been edited without pressing Search.
async function loadNotes(cursor = null) {
  if (!state.team) return;
  if (!cursor) {
    const f = $('search-form').elements;
    state.query = { q: f.q.value, limit: f.limit.value };
  }
  const params = new URLSearchParams(state.query);
  if (cursor) params.set('cursor', cursor);
  const page = await client.request('GET', `${teamPath()}/notes?${params}`);
  const notes = page.items;
  state.nextCursor = page.next_cursor;
  $('next-page').disabled = page.next_cursor === null;
  $('notes').replaceChildren(...notes.map((n) =>
    el('li', {},
      el('span', { class: 'grow' }, n.title, ' ',
        el('span', { class: 'muted' }, `v${n.version} · edited ${stamp(n.updated_at)}`), ' ',
        n.lock ? el('span', { class: 'badge locked' }, `locked by ${n.lock.user_name}`) : null),
      el('button', { type: 'button', onclick: action(() => pane.open(state.team, n.id)) }, 'Open'))));
  if (notes.length === 0) $('notes').append(el('li', { class: 'muted' }, 'No notes on this page.'));
}

async function createNote(form) {
  const { title, body } = Object.fromEntries(new FormData(form));
  const note = await client.request('POST', `${teamPath()}/notes`, { title, body });
  form.reset();
  await loadNotes();
  await pane.open(state.team, note.id);
}

/* ---------- wiring ---------- */

$('login-form').elements.email.value = remember.get('main.email');
$('login-form').addEventListener('submit', action((e) => login(e.target.elements.email.value, e.target.elements.password.value)));
$('signup-form').addEventListener('submit', action((e) => signup(e.target)));
$('logout').addEventListener('click', logout);
$('team-form').addEventListener('submit', action((e) => createTeam(e.target)));
$('teams-refresh').addEventListener('click', action(loadTeams));
$('member-form').addEventListener('submit', action((e) => addMember(e.target)));
$('members-refresh').addEventListener('click', action(loadMembers));
$('search-form').addEventListener('submit', action(() => loadNotes()));
$('next-page').addEventListener('click', action(() => state.nextCursor && loadNotes(state.nextCursor)));
$('note-form').addEventListener('submit', action((e) => createNote(e.target)));
$('log-clear').addEventListener('click', () => $('log').replaceChildren());
$('banner-close').addEventListener('click', hideBanner);

// A stored token may have expired; /me confirms it, and a 401 there falls back to login.
async function start() {
  if (!client.loggedIn) return showView();
  await client.refreshUser();
  showView();
  await loadTeams();
  demo.restore();
}
action(start)();
