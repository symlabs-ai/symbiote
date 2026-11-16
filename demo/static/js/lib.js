// Small helpers shared by every tab: API calls, DOM building, formatting.

const TOKEN = document.querySelector('meta[name="demo-token"]').content;
const TOKEN_ERROR = 'token do demo ausente ou inválido';

// Each server start makes a new token. A page opened before a restart holds
// the old one and every action would fail with 403: reload it instead.
export async function serverRestarted() {
  try {
    const html = await (await fetch('/', { cache: 'no-store' })).text();
    const m = html.match(/name="demo-token" content="([^"]+)"/);
    return Boolean(m && m[1] !== TOKEN);
  } catch {
    return false; // server still down: the event stream retries
  }
}

let reloading = false;
export function reloadAfterRestart(message = 'O servidor do demo reiniciou. Recarregando a página…') {
  if (reloading) return;
  reloading = true;
  toast(message, 'warn');
  setTimeout(() => window.location.reload(), 1500);
}

async function request(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== 'GET') opts.headers['X-Demo-Token'] = TOKEN;
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(path, opts);
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!resp.ok) {
    if (resp.status === 403 && data && data.detail === TOKEN_ERROR) reloadAfterRestart();
    const detail = data && data.detail ? data.detail : `HTTP ${resp.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return data;
}

export const api = {
  token: TOKEN,
  get: (path) => request('GET', path),
  post: (path, body = {}) => request('POST', path, body),
  del: (path) => request('DELETE', path),
};

// h('div.card#id', {onclick, title, ...}, child, 'text', [children]) → Element
export function h(spec, attrs, ...children) {
  if (attrs === null || typeof attrs !== 'object' || attrs instanceof Node || Array.isArray(attrs)) {
    children.unshift(attrs);
    attrs = {};
  }
  const [, tag = 'div', rest = ''] = spec.match(/^([a-z0-9-]*)(.*)$/i);
  const el = document.createElement(tag || 'div');
  for (const part of rest.match(/[.#][^.#]+/g) || []) {
    if (part[0] === '.') el.classList.add(part.slice(1));
    else el.id = part.slice(1);
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className += ' ' + v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k === 'value') el.value = v;
    else if (k === 'checked') el.checked = !!v;
    else if (k === 'html') el.innerHTML = v; // only for trusted, static markup
    else el.setAttribute(k, v === true ? '' : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function mount(container, ...children) {
  container.replaceChildren();
  append(container, children);
}

export const badge = (text, color = '') => h(`span.badge${color ? '.' + color : ''}`, text);

export function riskBadge(risk) {
  return badge(risk || 'low', { high: 'red', medium: 'amber', low: 'green' }[risk] || '');
}

export function empty(text) { return h('div.empty', text); }

export function pre(value) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return h('pre.code', text);
}

export function fold(summary, body, open = false) {
  return h('details', { open }, h('summary', summary), body);
}

export function meter(value, max = 1, color = '') {
  const pct = Math.max(0, Math.min(100, (value / (max || 1)) * 100));
  return h(`div.meter${color ? '.' + color : ''}`, h('span', { style: { width: pct + '%' } }));
}

export function fmtTime(value) {
  if (!value) return '';
  const d = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(d)) return String(value);
  return d.toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined) return '';
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function short(text, n = 140) {
  const s = String(text ?? '');
  return s.length > n ? s.slice(0, n) + '…' : s;
}

export function score(v) {
  return v === null || v === undefined ? '—' : Number(v).toFixed(2);
}

export function toast(text, kind = '') {
  const el = h(`div.toast${kind ? '.' + kind : ''}`, text);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'err' || kind === 'warn' ? 7000 : 4000);
}

// Run an async action from a button: disables it, shows errors as toasts.
export async function act(button, fn, okText) {
  const label = button ? button.textContent : '';
  if (button) { button.disabled = true; button.textContent = label + '…'; }
  try {
    const out = await fn();
    if (okText) toast(okText);
    return out;
  } catch (e) {
    toast(e.message, 'err');
    return undefined;
  } finally {
    if (button) { button.disabled = false; button.textContent = label; }
  }
}

export function confirmDialog(title, text) {
  return new Promise((resolve) => {
    const box = document.getElementById('confirm');
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-text').textContent = text;
    box.hidden = false;
    const done = (v) => { box.hidden = true; resolve(v); };
    document.getElementById('confirm-yes').onclick = () => done(true);
    document.getElementById('confirm-no').onclick = () => done(false);
  });
}

export function select(name, options, value, onchange) {
  return h('select', { name, onchange },
    options.map((o) => {
      const [v, label] = Array.isArray(o) ? o : [o, o];
      return h('option', { value: v, selected: String(v) === String(value) }, label);
    }));
}

// Markdown from the model goes through marked inside <symbiote-chat>; raw
// HTML outside code is neutralized first so web content cannot inject markup.
export function neutralizeHtml(text) {
  return String(text || '')
    .split(/(```[\s\S]*?```|`[^`\n]*`)/g)
    .map((part, i) => (i % 2 ? part : part.replace(/</g, '&lt;')))
    .join('');
}
