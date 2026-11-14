// Page shell: setup screen, chat, live event feed, tabs, approvals, tour.

import { api, h, act, toast, neutralizeHtml, short } from './lib.js';
import live from './tabs/live.js';
import context from './tabs/context.js';
import memory from './tabs/memory.js';
import learning from './tabs/learning.js';
import tools from './tabs/tools.js';
import execution from './tabs/execution.js';
import skills from './tabs/skills.js';
import harness from './tabs/harness.js';
import web from './tabs/web.js';
import system from './tabs/system.js';
import { initTour } from './tour.js';

const TABS = [live, context, memory, learning, tools, execution, skills, harness, web, system];
const PAGE_LOADED = Date.now() / 1000;

const state = {
  status: null,
  symbiote: 'sol',
  tab: 'live',
  lastSeq: 0,
  longrun: { plan: null, blocks: [] },
  lastClose: null,
};

const panes = {};       // tab id → container element
const dirty = new Set();
const rendered = new Set();
let refreshTimer = null;

const ctx = {
  state,
  api,
  get symbiote() { return state.symbiote; },
  get symbioteId() { return state.status?.symbiotes?.[state.symbiote]?.id; },
  get sessionId() { return state.status?.symbiotes?.[state.symbiote]?.session_id; },
  openTab,
  refresh: (id = state.tab) => renderTab(id),
  sendPrompt,
  reloadStatus,
  closeSession,
  rebuildChat: buildChat,
  showSetup,
};

// ── status & setup ──────────────────────────────────────────────────

async function reloadStatus() {
  state.status = await api.get('/api/status');
  const s = state.status;
  document.getElementById('llm-pill').textContent =
    s.ready ? `${s.settings.provider} · ${s.settings.model}` : 'sem LLM';
  updateSessionInfo();
  return s;
}

function updateSessionInfo() {
  const sym = state.status?.symbiotes?.[state.symbiote];
  document.getElementById('session-info').textContent =
    sym ? `${sym.name} · sessão ${sym.session_id.slice(0, 8)}` : '';
}

function showSetup(cancellable = false) {
  const s = state.status || {};
  const box = document.getElementById('setup');
  const form = document.getElementById('setup-form');
  document.getElementById('env-path').textContent = s.env_path || 'demo/demo.env';
  document.getElementById('setup-cancel').hidden = !cancellable;
  document.getElementById('setup-cancel').onclick = () => { box.hidden = true; };
  const provider = form.elements.provider;
  const modelsBox = document.getElementById('setup-models-box');
  const modelsInfo = document.getElementById('setup-models-info');
  const gwModel = form.elements.gw_model;
  const gwAux = form.elements.gw_aux_model;
  const saved = s.settings || {};
  const hasSavedKey = () => saved.provider === provider.value && !!saved.api_key;
  // Fill both selects; keep the current choice when it is still allowed.
  const fillModels = (models) => {
    const main = gwModel.value || (saved.provider === 'symgateway' ? saved.model : '');
    const aux = gwAux.value || (saved.provider === 'symgateway' && saved.aux_model !== saved.model ? saved.aux_model : '');
    gwModel.replaceChildren(...models.map((id) => h('option', { value: id }, id)));
    gwAux.replaceChildren(h('option', { value: '' }, 'o mesmo do principal'),
      ...models.map((id) => h('option', { value: id }, id)));
    gwModel.value = models.includes(main) ? main : models[0] || '';
    gwAux.value = models.includes(aux) ? aux : '';
  };
  const loadModels = async () => {
    const btn = document.getElementById('setup-models');
    btn.disabled = true;
    modelsInfo.textContent = 'consultando /v1/models…';
    try {
      const { models } = await api.post('/api/setup/models', Object.fromEntries(new FormData(form)));
      fillModels(models);
      modelsInfo.textContent = models.length
        ? `${models.length} modelo(s) liberado(s) para esta chave`
        : 'nenhum modelo LLM liberado para esta chave';
      modelsBox.open = true;
    } catch (ex) {
      modelsInfo.textContent = ex.message;
    } finally {
      btn.disabled = false;
    }
  };
  const syncProvider = () => {
    const gateway = provider.value === 'symgateway';
    const [main, aux] = (s.default_models || {})[provider.value] || ['', ''];
    form.elements.model.placeholder = main;
    form.elements.aux_model.placeholder = aux;
    form.querySelectorAll('.gw-only').forEach((el) => { el.hidden = !gateway; });
    form.querySelectorAll('.std-only').forEach((el) => { el.hidden = gateway; });
    document.getElementById('setup-models-hint').textContent = gateway ? '(obrigatório)' : '(opcional)';
    if (gateway) modelsBox.open = true;
    form.elements.api_key.placeholder = hasSavedKey() ? `${saved.api_key} (vazio = manter)` : 'sk-…';
    if (gateway && hasSavedKey() && gwModel.options.length <= 1 && !gwModel.value) loadModels();
  };
  if (saved.provider) provider.value = saved.provider;
  if (saved.provider === 'symgateway') form.elements.project.value = saved.gateway_project || '';
  provider.onchange = syncProvider;
  form.elements.api_key.onchange = () => { if (provider.value === 'symgateway' && form.elements.api_key.value.trim()) loadModels(); };
  document.getElementById('setup-models').onclick = loadModels;
  syncProvider();
  box.hidden = false;
  form.elements.api_key.focus();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = document.getElementById('setup-error');
    err.hidden = true;
    const btn = document.getElementById('setup-submit');
    btn.disabled = true;
    btn.textContent = 'Validando a chave…';
    try {
      const body = Object.fromEntries(new FormData(form));
      if (body.provider === 'symgateway') {
        body.model = body.gw_model || '';
        body.aux_model = body.gw_aux_model || '';
      }
      delete body.gw_model;
      delete body.gw_aux_model;
      await api.post('/api/setup', body);
      location.reload();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Validar e começar';
    }
  };
}

// ── chat ────────────────────────────────────────────────────────────

const SUGGESTIONS = {
  sol: [
    { label: 'O que tenho para fazer esta semana?', message: 'O que tenho para fazer esta semana? Junte tarefas e agenda.' },
    { label: 'Ensinar uma preferência', message: 'Lembre disso: eu prefiro respostas em tópicos curtos e nunca quero que você marque compromissos antes das 9h.' },
    { label: 'Gastos do mês', message: 'Quanto gastei este mês por categoria? Estou dentro do orçamento da planilha financas/orcamento-2026.csv?' },
    { label: 'Converter unidades (MCP)', message: 'Quanto é 42 km em milhas? E 30 °C em Fahrenheit?' },
  ],
  researcher: [
    { label: 'Resumir uma página', message: 'Leia https://pt.wikipedia.org/wiki/Curitiba e me dê 5 fatos úteis para quem vai se mudar.' },
    { label: 'Navegar e capturar a tela', message: 'Abra https://example.com no navegador, tire um screenshot e diga o que a página mostra.' },
  ],
};

function buildChat() {
  const col = document.getElementById('chat-col');
  const sym = state.status?.symbiotes?.[state.symbiote];
  const el = document.createElement('symbiote-chat');
  el.setAttribute('mode', 'inline');
  el.setAttribute('name', sym?.name || 'Sol');
  el.setAttribute('placeholder', state.symbiote === 'sol' ? 'Peça algo ao Sol…' : 'Peça uma pesquisa…');
  el.setAttribute('greeting', state.symbiote === 'sol' ? 'Oi! Eu sou o Sol.' : 'Pesquisador aqui.');
  el.setAttribute('greeting-sub', state.symbiote === 'sol'
    ? 'Cuido de tarefas, agenda, contas, arquivos e lembro do que você me ensina. Acompanhe tudo nas abas ao lado.'
    : 'Leio páginas e navego na web. O Sol também me chama quando precisa.');
  el.setAttribute('session-key', `${state.symbiote}:${sym?.session_id || ''}`);
  el.adapter = {
    async sendMessage() { throw new Error('streaming only'); },
    async sendMessageStream(message) {
      return { url: '/api/chat/stream', headers: { 'X-Demo-Token': api.token },
               body: { message, symbiote: state.symbiote } };
    },
    async loadHistory() {
      const r = await api.get(`/api/chat/history?symbiote=${state.symbiote}`);
      return { messages: r.messages };
    },
    async clearHistory() {
      await api.post('/api/session/new', { symbiote: state.symbiote });
      await reloadStatus();
      toast('Nova sessão iniciada (a anterior continua aberta, sem aprendizado).');
    },
    textFilter: neutralizeHtml,
    loadingContent: () => '<span class="symbiote-spinner"></span> pensando… (acompanhe em <b>Ao vivo</b>)',
  };
  col.replaceChildren(el);
  // After connecting: the setter renders into the shadow DOM built on connect.
  el.initialSuggestions = SUGGESTIONS[state.symbiote] || [];
}

function sendPrompt(text, symbiote) {
  const go = () => {
    const chat = document.querySelector('symbiote-chat');
    chat.sendMessage(text);
  };
  if (symbiote && symbiote !== state.symbiote) {
    switchSymbiote(symbiote).then(() => setTimeout(go, 150));
  } else go();
}

async function switchSymbiote(which) {
  state.symbiote = which;
  document.querySelectorAll('#symbiote-switch button').forEach((b) =>
    b.classList.toggle('active', b.dataset.symbiote === which));
  updateSessionInfo();
  buildChat();
  TABS.forEach((t) => dirty.add(t.id));
  renderTab(state.tab);
}

async function closeSession(button) {
  const out = await act(button, () => api.post('/api/session/close', { symbiote: state.symbiote }));
  if (!out) return;
  state.lastClose = out;
  await reloadStatus();
  buildChat();
  const n = out.learned.length;
  toast(`Sessão encerrada: score ${out.score ? Number(out.score.final_score).toFixed(2) : '—'}, ${n} memória(s) nova(s).`);
  TABS.forEach((t) => dirty.add(t.id));
  openTab('learning');
}

// ── tabs ────────────────────────────────────────────────────────────

function buildTabs() {
  const nav = document.getElementById('tabs');
  const body = document.getElementById('tab-body');
  for (const tab of TABS) {
    nav.append(h('button', { role: 'tab', 'data-tab': tab.id, onclick: () => openTab(tab.id) }, tab.title));
    panes[tab.id] = h('div', { hidden: true, 'data-pane': tab.id });
    body.append(panes[tab.id]);
  }
}

function openTab(id) {
  state.tab = id;
  document.querySelectorAll('#tabs button').forEach((b) => {
    const active = b.dataset.tab === id;
    b.classList.toggle('active', active);
    if (active) b.querySelector('.dot')?.remove();
  });
  for (const [tid, pane] of Object.entries(panes)) pane.hidden = tid !== id;
  if (!rendered.has(id) || dirty.has(id)) renderTab(id);
}

async function renderTab(id) {
  const tab = TABS.find((t) => t.id === id);
  if (!tab || !state.status?.ready) return;
  dirty.delete(id);
  rendered.add(id);
  try {
    await tab.render(panes[id], ctx);
  } catch (e) {
    panes[id].replaceChildren(h('div.empty', `Falha ao carregar: ${e.message}`));
  }
}

function markDirty(tab) {
  if (tab.id === state.tab) {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => renderTab(tab.id), 400);
    return;
  }
  dirty.add(tab.id);
  const btn = document.querySelector(`#tabs button[data-tab="${tab.id}"]`);
  if (btn && !btn.querySelector('.dot') && tab.id !== 'live') btn.append(h('span.dot'));
}

// ── events ──────────────────────────────────────────────────────────

const approvalQueue = [];

function handleEvent(ev) {
  if (ev.seq <= state.lastSeq) return;
  state.lastSeq = ev.seq;
  if (ev.type === 'plan') state.longrun = { plan: ev.blocks, blocks: [] };
  if (ev.type === 'block') state.longrun.blocks.push(ev);
  // A request replayed after a reload still shows if it has not expired; its
  // approval_result, when it already came, follows in the replay and closes it.
  if (ev.type === 'approval_request' && ev.ts + (ev.timeout_s || 180) > Date.now() / 1000) queueApproval(ev);
  if (ev.type === 'approval_result') resolveApproval(ev.id, ev);
  if (ev.type === 'session' || ev.type === 'turn_end' || ev.type === 'learned') reloadStatus();
  // Only a reset that happens after this page loaded: the event stays in the
  // replay buffer, and reacting to the old one would reload forever.
  if (ev.type === 'runtime' && ev.state === 'reset' && ev.ts > PAGE_LOADED) location.reload();
  for (const tab of TABS) {
    if (tab.onEvent) tab.onEvent(ev, ctx, panes[tab.id]);
    if (tab.watch && tab.watch.includes(ev.type) && rendered.has(tab.id)) markDirty(tab);
  }
}

function connectEvents() {
  const source = new EventSource(`/api/events?since=${state.lastSeq}`);
  source.onmessage = (msg) => {
    try { handleEvent(JSON.parse(msg.data)); } catch (e) { console.error(e); }
  };
}

// ── approvals ───────────────────────────────────────────────────────

function queueApproval(ev) {
  if (approvalQueue.some((a) => a.id === ev.id)) return;
  approvalQueue.push(ev);
  if (approvalQueue.length === 1) showApproval();
}

const PAGE_TITLE = document.title;
let approvalTimer = null;

function resolveApproval(id, result = null) {
  const idx = approvalQueue.findIndex((a) => a.id === id);
  if (idx === -1) return;
  const [ev] = approvalQueue.splice(idx, 1);
  if (result && result.timed_out && result.ts > PAGE_LOADED) {
    toast(`Ninguém respondeu ao pedido de “${ev.tool_id}” em ${Math.round(ev.timeout_s || 180)}s: contou como negado.`, 'warn');
  }
  if (idx === 0) {
    clearInterval(approvalTimer);
    document.getElementById('approval').hidden = true;
    document.title = PAGE_TITLE;
    if (approvalQueue.length) showApproval();
  }
}

// Seconds left, from the server's timestamp (same machine, same clock).
function approvalLeft(ev) {
  return Math.max(0, Math.ceil(ev.ts + (ev.timeout_s || 180) - Date.now() / 1000));
}

function showApproval() {
  const ev = approvalQueue[0];
  document.getElementById('approval-title').textContent = `Executar “${ev.tool_id}”?`;
  document.getElementById('approval-params').textContent = JSON.stringify(ev.params, null, 2);
  const counter = document.getElementById('approval-timeout');
  const tick = () => {
    const left = approvalLeft(ev);
    counter.textContent = left;
    counter.classList.toggle('urgent', left <= 30);
    // Visible from another tab or window, where the modal is not.
    document.title = `(${left}s) ✋ Aprovação pendente · ${PAGE_TITLE}`;
  };
  clearInterval(approvalTimer);
  tick();
  approvalTimer = setInterval(tick, 1000);
  const decide = async (approved) => {
    try {
      await api.post(`/api/approvals/${ev.id}`, { approved });
    } catch (e) { toast(e.message, 'err'); }
    resolveApproval(ev.id);
  };
  document.getElementById('approval-allow').onclick = () => decide(true);
  document.getElementById('approval-deny').onclick = () => decide(false);
  document.getElementById('approval').hidden = false;
  document.getElementById('approval-deny').focus();
}

// ── boot ────────────────────────────────────────────────────────────

async function boot() {
  buildTabs();
  try {
    await reloadStatus();
  } catch (e) {
    toast(`Servidor indisponível: ${e.message}`, 'err');
    return;
  }
  connectEvents();
  if (!state.status.ready) {
    showSetup(false);
    return;
  }
  for (const p of state.status.pending_approvals || []) {
    queueApproval(p);
  }
  document.querySelectorAll('#symbiote-switch button').forEach((b) =>
    b.addEventListener('click', () => switchSymbiote(b.dataset.symbiote)));
  document.getElementById('btn-new-session').onclick = async (e) => {
    await act(e.currentTarget, () => api.post('/api/session/new', { symbiote: state.symbiote }),
      'Nova sessão iniciada. A anterior continua aberta e não gera aprendizado.');
    await reloadStatus();
    buildChat();
  };
  document.getElementById('btn-close-session').onclick = (e) => closeSession(e.currentTarget);
  initTour(ctx);
  buildChat();
  openTab('live');
  if (state.status.mcp_error) toast(`Servidor MCP indisponível: ${short(state.status.mcp_error, 120)}`, 'warn');
  if (state.status.web_error) toast(`Tools web indisponíveis: ${short(state.status.web_error, 120)}`, 'warn');
}

boot();
