// DOM helpers, the page banner and the request log.
//
// All server text reaches the DOM through textContent, text nodes (el()) or input
// values, never innerHTML.

export const $ = (id) => document.getElementById(id);
export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const time = (iso) => new Date(iso).toLocaleTimeString();
export const stamp = (iso) => new Date(iso).toLocaleString();

// el('li', { class: 'x', onclick: fn }, 'text', child): attributes, listeners, children.
export function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else if (k === 'class') node.className = v;
    else node.setAttribute(k, v);
  }
  node.append(...kids.filter((k) => k !== null && k !== undefined));
  return node;
}

// localStorage for conveniences only (a remembered email); it may be unavailable.
export const remember = {
  get(key) {
    try { return localStorage.getItem(key) || ''; } catch { return ''; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* not important */ }
  },
};

/* ---------- banner ---------- */

export function showBanner(text, kind) {
  $('banner-text').textContent = text;
  $('banner').className = kind;
  $('banner').hidden = false;
}

export function hideBanner() {
  $('banner').hidden = true;
}

/* ---------- request log ---------- */

const MAX_LOG_CHARS = 4000;
const MAX_LOG_ENTRIES = 200;

// who: { label, tone } of the client that made the call.
export function logCall(who, method, path, status, data, started) {
  const ms = Math.round(performance.now() - started);
  const failed = typeof status !== 'number' || status >= 400;
  const err = data && data.error;
  let json = data === null ? '(no body)' : typeof data === 'string' ? data : JSON.stringify(data, null, 2);
  if (json.length > MAX_LOG_CHARS) json = json.slice(0, MAX_LOG_CHARS) + `\n... truncated (${json.length} chars)`;
  const entry = el('div', { class: `entry tone-${who.tone}` + (failed ? ' err' : '') },
    el('div', { class: 'head' },
      `${time(Date.now())}  `, el('span', { class: 'who' }, `[${who.label}]`),
      `  ${method} ${path}  ->  ${status}  (${ms} ms)`),
    err ? el('div', { class: 'errline' }, `${err.code}: ${err.message}`) : null,
    el('pre', {}, json));
  const log = $('log');
  log.prepend(entry);
  while (log.childElementCount > MAX_LOG_ENTRIES) log.lastChild.remove();
}
