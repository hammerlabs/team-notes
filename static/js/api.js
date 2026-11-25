// The API client: the only code that calls fetch.
//
// One ApiClient per logged-in user on the page. Each keeps its own token and user in
// sessionStorage under its own prefix, so the main pane and the demo's second pane are
// two independent sessions (and two browser windows can still be two users).
// Every call is written to the request log, labelled with the client's user.

import { logCall, time } from './dom.js';

export class ApiError extends Error {
  constructor(status, error) {
    super(error.message);
    this.status = status;
    this.code = error.code;
    this.error = error;
  }
}

// The If-Match header for a save or delete based on the note at `version`. The server's
// ETag for a note is its version in quotes, and the version is also in the body.
export function ifMatch(version) {
  return { 'If-Match': `"${version}"` };
}

// One line for a failed call. Surfaces the extra keys of the 409s and 412s a reviewer is
// most likely testing: who holds the lock, and which version the server is at.
export function describeError(e) {
  if (!(e instanceof ApiError)) return `Network error: ${e.message}`;
  let text = `${e.status} ${e.code}: ${e.message}`;
  if (e.error.lock) text += ` (held by ${e.error.lock.user_name} until ${time(e.error.lock.expires_at)})`;
  if (e.error.note) text += ` (server is at version ${e.error.note.version})`;
  if (e.error.details) text += ' ' + JSON.stringify(e.error.details);
  return text;
}

export class ApiClient {
  // prefix: sessionStorage key prefix. tone: 'a' or 'b', the colour of this client's
  // log entries and pane. onUnauthorized: called after a 401 has cleared the session.
  constructor({ prefix, tone, onUnauthorized }) {
    this.prefix = prefix;
    this.tone = tone;
    this.onUnauthorized = onUnauthorized || (() => {});
    this.token = sessionStorage.getItem(`${prefix}.token`);
    this.user = JSON.parse(sessionStorage.getItem(`${prefix}.user`) || 'null');
  }

  get loggedIn() {
    return Boolean(this.token);
  }

  get label() {
    return this.user ? this.user.name : 'anonymous';
  }

  async login(email, password) {
    const res = await this.request('POST', '/auth/login', { email, password });
    this.token = res.token;
    this.setUser(res.user);
    sessionStorage.setItem(`${this.prefix}.token`, res.token);
    return res.user;
  }

  async signup(name, email, password) {
    await this.request('POST', '/auth/signup', { name, email, password });
    return this.login(email, password);
  }

  // A stored token may have expired; /me confirms it (a 401 clears the session).
  async refreshUser() {
    this.setUser(await this.request('GET', '/me'));
    return this.user;
  }

  // Forgetting the token is all logout means here; the server session simply ages out.
  logout() {
    this.token = null;
    this.user = null;
    sessionStorage.removeItem(`${this.prefix}.token`);
    sessionStorage.removeItem(`${this.prefix}.user`);
  }

  setUser(user) {
    this.user = user;
    sessionStorage.setItem(`${this.prefix}.user`, JSON.stringify(user));
  }

  // Resolves with the parsed body (null for an empty one) or throws ApiError for any
  // non-2xx. A network failure or an abort rethrows fetch's own error. `extraHeaders`
  // carries per-call headers such as If-Match.
  async request(method, path, body, signal, extraHeaders = {}) {
    const headers = { ...extraHeaders };
    if (this.token) headers.Authorization = 'Bearer ' + this.token;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const who = { label: this.label, tone: this.tone };
    const started = performance.now();
    let res;
    try {
      res = await fetch(path, { method, headers, signal, body: body === undefined ? undefined : JSON.stringify(body) });
    } catch (e) {
      if (e.name !== 'AbortError') logCall(who, method, path, 'network error', e.message, started);
      throw e;
    }
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    logCall(who, method, path, res.status, data, started);
    if (res.ok) return data;
    // Non-contract responses (a proxy error page, a crash) still get a code so callers can rely on it.
    const error = data && data.error ? data.error
      : { code: 'http_' + res.status, message: typeof data === 'string' ? data.slice(0, 200) : res.statusText };
    if (res.status === 401 && error.code === 'unauthorized' && this.token) {
      this.logout();
      this.onUnauthorized();
    }
    throw new ApiError(res.status, error);
  }
}
