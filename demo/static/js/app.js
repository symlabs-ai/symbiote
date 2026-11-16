// Page shell: setup screen, chat, live event feed, tabs, approvals, tour.

import { api, h, act, mount, toast, neutralizeHtml, short, fmtMs, serverRestarted, reloadAfterRestart } from './lib.js';
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
import { initTour, resetTourProgress } from './tour.js';

const PAGE_LOADED_AT = Date.now();

const TABS = [live, context, memory, learning, tools, execution, skills, harness, web, system];
const PAGE_LOADED = Date.now() / 1000;

const state = {
  status: null,
  symbiote: 'sol',
  tab: 'chat',
  liveOpen: true,
  lastSeq: 0,
  longrun: { plan: null, blocks: [] },
  lastClose: null,
};

const APPROVAL_MODES = [
  ['low', 'Perguntar em tudo'],
  ['medium', 'Perguntar em médio e alto'],
  ['high', 'Perguntar só em alto risco'],
  ['none', 'Só Chrome pede confirmação'],
  ['allow_all', 'Permitir tudo (inclusive Chrome)'],
];
const STEP_LIMITS = [4, 8, 12, 20, 30, 50];

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
  const label = s.ready ? `${s.settings.provider} · ${s.settings.model}` : 'sem LLM';
  document.getElementById('llm-pill').textContent = label;
  // The pill hides on narrow windows; the gear stays and carries the same text.
  document.getElementById('btn-settings').title =
    s.ready ? `Configurações do LLM · ${label} · ${s.settings.server}` : 'Configurações do LLM';
  updateSessionInfo();
  return s;
}

function updateSessionInfo() {
  const sym = state.status?.symbiotes?.[state.symbiote];
  document.getElementById('session-info').textContent =
    sym ? `${sym.name} · sessão ${sym.session_id.slice(0, 8)}` : '';
}

// The LLM settings dialog. First run (`cancellable` false): the page cannot
// start without a key. Later, from the gear in the top bar: shows what is in
// use, lets every value be changed and tested before it is saved.
// Sol's own Chrome: the sites it may open. It opens by itself when Sol needs it.
function setupChrome(visible, status) {
  const section = document.getElementById('setup-chrome');
  section.hidden = !visible;
  if (!visible) return;
  const allow = document.getElementById('chrome-allowlist');
  const out = document.getElementById('chrome-result');
  allow.value = (status.settings?.chrome_allowlist || []).join('\n');
  const session = status.settings?.chrome_session === 'own' ? 'own' : 'open';
  document.getElementById(session === 'own' ? 'chrome-own' : 'chrome-open').checked = true;
  const chosen = () => (document.getElementById('chrome-own').checked ? 'own' : 'open');
  document.getElementById('chrome-status').textContent = status.chrome_error
    ? `Indisponível: ${status.chrome_error}`
    : `Tools: ${(status.chrome_tools || []).join(', ')}`;
  out.hidden = true;
  const show = (ok, ...children) => {
    out.className = `test-result ${ok ? 'ok' : 'fail'}`;
    mount(out, ...children);
    out.hidden = false;
    out.scrollIntoView({ block: 'nearest' });
  };
  document.getElementById('chrome-test').onclick = (e) => act(e.currentTarget, async () => {
    const r = await api.post('/api/setup/chrome/test');
    show(r.ok, h('div.small', h('b', r.ok ? 'Pronto' : 'Ainda não')), h('div.small.dim', r.detail));
  });
  document.getElementById('chrome-apply').onclick = (e) => act(e.currentTarget, async () => {
    try {
      const r = await api.post('/api/setup/chrome', { allowlist: allow.value, session: chosen() });
      if (r.chrome_error) {
        show(false, h('span.error', r.chrome_error));
        return;
      }
      toast(`Chrome do Sol: ${r.settings.chrome_allowlist.join(', ')}`);
      location.reload();  // the kernel restarted with the new list
    } catch (ex) {
      show(false, h('span.error', ex.message));
    }
  });
}

function showSetup(cancellable = false) {
  const s = state.status || {};
  const box = document.getElementById('setup');
  const form = document.getElementById('setup-form');
  const err = document.getElementById('setup-error');
  const testOut = document.getElementById('setup-test');
  const submit = document.getElementById('setup-submit');
  const testBtn = document.getElementById('setup-test-btn');
  const submitLabel = cancellable ? 'Salvar e aplicar' : 'Validar e começar';
  const close = () => { box.hidden = true; document.removeEventListener('keydown', onKey); };
  const onKey = (e) => { if (cancellable && e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);
  document.getElementById('setup-title').textContent = cancellable ? 'Configurações do LLM' : 'Symbiote demo';
  document.getElementById('env-path').textContent = s.env_path || 'demo/demo.env';
  document.getElementById('setup-cancel').hidden = !cancellable;
  document.getElementById('setup-cancel').onclick = close;
  submit.textContent = submitLabel;
  err.hidden = true;
  testOut.hidden = true;
  const provider = form.elements.provider;
  const modelsBox = document.getElementById('setup-models-box');
  const modelsInfo = document.getElementById('setup-models-info');
  const gwModel = form.elements.gw_model;
  const gwAux = form.elements.gw_aux_model;
  const saved = s.settings || {};

  // What is in use right now, read-only; the fields below edit it.
  const current = document.getElementById('setup-current');
  current.hidden = !s.ready;
  if (s.ready) {
    const rows = [
      ['Em uso', saved.provider],
      ['Servidor', saved.server],
      ['Modelo principal', saved.model],
      ['Modelo auxiliar', saved.aux_model],
      ['Chave', saved.api_key],
      saved.gateway_project ? ['Projeto', saved.gateway_project] : null,
      saved.search_via ? ['Busca na web', saved.search_via] : null,
      s.chrome_tools?.length ? ['Chrome do Sol', `${saved.chrome_session === 'own' ? 'separado' : 'o seu, já aberto'} · sites: ${(saved.chrome_allowlist || []).join(', ')}`] : null,
    ].filter(Boolean);
    current.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', k), h('dd', v || '—')]));
  }
  setupChrome(cancellable && s.ready, s);

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
  // The form as the API expects it: one model pair, whichever provider.
  const formBody = () => {
    const body = Object.fromEntries(new FormData(form));
    if (body.provider === 'symgateway') {
      body.model = body.gw_model || '';
      body.aux_model = body.gw_aux_model || '';
    }
    delete body.gw_model;
    delete body.gw_aux_model;
    delete body.chrome_session_;  // Sol's Chrome has its own endpoint
    return body;
  };
  const loadModels = async () => {
    const btn = document.getElementById('setup-models');
    btn.disabled = true;
    modelsInfo.textContent = 'consultando /v1/models…';
    try {
      const { models } = await api.post('/api/setup/models', formBody());
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
    const same = saved.provider === provider.value;
    const [main, aux] = (s.default_models || {})[provider.value] || ['', ''];
    form.elements.model.placeholder = main;
    form.elements.aux_model.placeholder = aux;
    // Saved values come back as editable text; another provider starts empty.
    form.elements.model.value = same && !gateway ? saved.model || '' : '';
    form.elements.aux_model.value = same && !gateway ? saved.aux_model || '' : '';
    form.elements.base_url.value = same && gateway ? saved.gateway_url || '' : '';
    form.elements.project.value = same && gateway ? saved.gateway_project || '' : '';
    form.querySelectorAll('.gw-only').forEach((el) => { el.hidden = !gateway; });
    form.querySelectorAll('.std-only').forEach((el) => { el.hidden = gateway; });
    document.getElementById('setup-models-hint').textContent = gateway ? '(obrigatório)' : '(opcional)';
    if (gateway || cancellable) modelsBox.open = true;
    form.elements.api_key.placeholder = hasSavedKey() ? `${saved.api_key} (vazio = manter)` : 'sk-…';
    testOut.hidden = true;
    if (gateway && hasSavedKey() && gwModel.options.length <= 1 && !gwModel.value) loadModels();
  };
  if (saved.provider) provider.value = saved.provider;
  form.elements.api_key.value = '';
  provider.onchange = syncProvider;
  form.elements.api_key.onchange = () => { if (provider.value === 'symgateway' && form.elements.api_key.value.trim()) loadModels(); };
  form.elements.base_url.onchange = () => { if (provider.value === 'symgateway' && (hasSavedKey() || form.elements.api_key.value.trim())) loadModels(); };
  document.getElementById('setup-models').onclick = loadModels;
  syncProvider();
  box.hidden = false;
  form.elements.api_key.focus();

  // Real calls with the values on screen; nothing is saved.
  testBtn.onclick = async () => {
    err.hidden = true;
    testBtn.disabled = true;
    testBtn.textContent = 'Testando…';
    testOut.className = 'test-result';
    testOut.replaceChildren(h('span.dim.small', 'chamando o servidor com os valores do formulário…'));
    testOut.hidden = false;
    try {
      const r = await api.post('/api/setup/test', formBody());
      testOut.classList.add(r.ok ? 'ok' : 'fail');
      // mount() skips empty children; replaceChildren would print "null".
      mount(testOut,
        h('div.small', h('b', r.ok ? 'Funcionou' : 'Falhou'), ` · ${r.provider} · ${r.server}`),
        r.checks.map((c) => h(`div.check.${c.ok ? 'ok' : 'fail'}`,
          h('span.mark', c.ok ? '✓' : '✗'),
          h('span', c.step, c.model ? h('span.dim', ` · ${c.model}`) : null),
          h('span.ms', fmtMs(c.elapsed_ms)),
          h('span.detail', c.detail))),
        r.note ? h('span.dim.small', r.note) : null,
        h('span.faint.small', 'Nada foi salvo. '
          + (cancellable ? '"Salvar e aplicar" grava estes valores e reinicia o kernel do demo.' : '')));
    } catch (ex) {
      testOut.classList.add('fail');
      testOut.replaceChildren(h('span.error', ex.message));
    } finally {
      testBtn.disabled = false;
      testBtn.textContent = 'Testar';
      testOut.scrollIntoView({ block: 'nearest' });  // clear of the sticky buttons
    }
  };

  form.onsubmit = async (e) => {
    e.preventDefault();
    err.hidden = true;
    submit.disabled = true;
    submit.textContent = 'Validando a chave…';
    try {
      await api.post('/api/setup', formBody());
      location.reload();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    } finally {
      submit.disabled = false;
      submit.textContent = submitLabel;
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

function buildChatControls(which) {
  const approval = h('select', { 'aria-label': 'Aprovação humana' },
    APPROVAL_MODES.map(([value, label]) => h('option', { value }, label)));
  approval.value = state.status?.approval_mode || (state.status?.approvals_enabled ? 'high' : 'none');
  const steps = h('select', { 'aria-label': 'Máximo de turnos por pedido', title: 'Iterações com tools em cada mensagem' },
    STEP_LIMITS.map((value) => h('option', { value }, String(value))));
  steps.disabled = true;
  const note = h('span.control-note', { 'aria-live': 'polite' }, 'Vale na próxima mensagem');
  if (approval.value === 'allow_all') note.textContent = 'Inclui ações no seu Chrome';
  const bar = h('div.chat-controls',
    h('label', 'Aprovação', approval),
    h('label', 'Máx. turnos', steps), note);

  approval.onchange = async () => {
    const previous = state.status?.approval_mode || 'high';
    approval.disabled = true;
    try {
      const result = await api.post('/api/approvals/mode', { mode: approval.value });
      state.status.approval_mode = result.mode;
      state.status.approvals_enabled = result.enabled;
      note.textContent = result.mode === 'allow_all' ? 'Inclui ações no seu Chrome' : 'Vale na próxima mensagem';
      dirty.add('tools');
      toast('Aprovação atualizada');
    } catch (error) {
      approval.value = previous;
      toast(error.message, 'err');
    } finally {
      approval.disabled = false;
    }
  };

  steps.onchange = async () => {
    const previous = steps.dataset.saved;
    steps.disabled = true;
    try {
      const result = await api.post('/api/config', {
        symbiote: which, fields: { max_tool_iterations: Number(steps.value) },
      });
      steps.dataset.saved = String(result.config.max_tool_iterations);
      dirty.add('execution');
      toast('Limite atualizado para a próxima mensagem');
    } catch (error) {
      steps.value = previous;
      toast(error.message, 'err');
    } finally {
      steps.disabled = false;
    }
  };

  api.get(`/api/config?symbiote=${which}`).then(({ config }) => {
    if (!bar.isConnected || state.symbiote !== which) return;
    const value = String(config.max_tool_iterations);
    if (![...steps.options].some((option) => option.value === value)) {
      steps.append(h('option', { value }, value));
    }
    steps.value = value;
    steps.dataset.saved = value;
    steps.disabled = false;
  }).catch((error) => { note.textContent = `Limite indisponível: ${error.message}`; });
  return bar;
}

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
    // Thumbnail cards for external links; the demo server fetches the page.
    async linkPreview(url) {
      try { return (await api.post('/api/preview', { url })).preview; } catch { return null; }
    },
    loadingContent: () => '<span class="symbiote-spinner"></span> pensando… (acompanhe em <b>Ao vivo</b>)',
  };
  col.replaceChildren(el, buildChatControls(state.symbiote));
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
  nav.append(h('button', { role: 'tab', 'data-tab': 'chat', onclick: () => openTab('chat') }, 'Chat'));
  for (const tab of TABS) {
    if (tab.id !== 'live') nav.append(h('button', { role: 'tab', 'data-tab': tab.id, onclick: () => openTab(tab.id) }, tab.title));
    panes[tab.id] = h('div', { hidden: true, 'data-pane': tab.id });
    (tab.id === 'live' ? document.getElementById('live-body') : body).append(panes[tab.id]);
  }
}

function setLiveOpen(open) {
  state.liveOpen = open;
  document.getElementById('live-col').hidden = !open;
  panes.live.hidden = !open;
  const button = document.getElementById('live-toggle');
  button.setAttribute('aria-expanded', String(open));
  button.classList.toggle('active', open);
  button.textContent = open ? 'Ocultar Ao vivo' : 'Mostrar Ao vivo';
  if (open && (!rendered.has('live') || dirty.has('live'))) renderTab('live');
}

function openTab(id) {
  if (id === 'live') {
    setLiveOpen(true);
    id = 'chat';
  }
  state.tab = id;
  document.querySelectorAll('#tabs button').forEach((b) => {
    const active = b.dataset.tab === id;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', String(active));
    if (active) b.querySelector('.dot')?.remove();
  });
  document.getElementById('chat-col').hidden = id !== 'chat';
  document.getElementById('panel-col').hidden = id === 'chat';
  for (const [tid, pane] of Object.entries(panes)) {
    if (tid !== 'live') pane.hidden = tid !== id;
  }
  if (id !== 'chat' && (!rendered.has(id) || dirty.has(id))) renderTab(id);
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
  // The demo was reset (here or from elsewhere): the tour marks and what this
  // page shows belong to data that is gone. Replayed resets predate the page.
  if (ev.type === 'runtime' && ev.state === 'reset' && ev.ts * 1000 > PAGE_LOADED_AT) {
    resetTourProgress();
    reloadAfterRestart('O demo foi zerado. Recarregando a página…');
  }
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
  let dropped = false;
  source.onmessage = (msg) => {
    try { handleEvent(JSON.parse(msg.data)); } catch (e) { console.error(e); }
  };
  // The stream drops when the server stops and reconnects by itself; a new
  // token on the other side means a restart, and this page is stale.
  source.onerror = () => { dropped = true; };
  source.onopen = async () => {
    if (!dropped) return;
    dropped = false;
    if (await serverRestarted()) reloadAfterRestart();
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
  document.getElementById('btn-settings').onclick = () => showSetup(true);
  document.getElementById('llm-pill').onclick = () => showSetup(true);
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
  document.getElementById('live-toggle').onclick = () => setLiveOpen(!state.liveOpen);
  document.getElementById('live-close').onclick = () => setLiveOpen(false);
  initTour(ctx);
  buildChat();
  setLiveOpen(true);
  openTab('chat');
  if (state.status.mcp_error) toast(`Servidor MCP indisponível: ${short(state.status.mcp_error, 120)}`, 'warn');
  if (state.status.web_error) toast(`Tools web indisponíveis: ${short(state.status.web_error, 120)}`, 'warn');
}

boot();
