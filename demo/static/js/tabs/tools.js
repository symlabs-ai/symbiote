// "Tools": every way to give the symbiote a tool, the visible × authorized
// matrix, the approval gate, the audit log and the personal API's data.

import { h, mount, badge, riskBadge, empty, act, fmtTime, pre, fold, short, select, toast } from '../lib.js';

const SOURCE = {
  builtin: ['builtin', ''], python: ['handler Python', 'blue'], http: ['HTTP declarativa', 'purple'],
  descoberta: ['descoberta (OpenAPI)', 'green'], mcp: ['MCP', 'amber'], web: ['web/browser', 'blue'],
  'meu chrome': ['Chrome do Sol', 'amber'],
};
let sourceFilter = 'all';

function tryRow(ctx, tool) {
  const out = h('div');
  const area = h('textarea', { rows: 3 }, '{}');
  return fold('testar direto', h('div.stack',
    h('p.faint.small', 'Executa pelo ToolGateway, sem LLM: passa pela política, pelos hooks e pelo audit log.'),
    area,
    h('div.row.end', h('button.btn.sm', {
      onclick: (e) => act(e.currentTarget, async () => {
        let params;
        try { params = JSON.parse(area.value || '{}'); } catch { throw new Error('JSON inválido'); }
        const r = await ctx.api.post('/api/tools/try', { symbiote: ctx.symbiote, tool_id: tool.tool_id, params });
        mount(out, r.success ? badge('ok', 'green') : badge('falhou', 'red'), pre(r.success ? r.output : r.error));
      }),
    }, 'executar')), out));
}

function matrix(ctx, data) {
  const rows = data.tools.filter((t) => sourceFilter === 'all' || t.source === sourceFilter);
  const sources = ['all', ...new Set(data.tools.map((t) => t.source))];
  return h('div',
    h('div.chips', { style: { marginBottom: '8px' } }, sources.map((s) => h(`button.chip${sourceFilter === s ? '.active' : ''}`, {
      onclick: () => { sourceFilter = s; ctx.refresh('tools'); },
    }, s === 'all' ? `todas (${data.tools.length})` : (SOURCE[s]?.[0] || s)))),
    h('table',
      h('tr', h('th', 'tool'), h('th', 'origem'), h('th', 'risco'), h('th', 'tags'),
        h('th', { title: 'Aparece no prompt (filtrada pelas tool_tags do symbiote)' }, 'visível'),
        h('th', { title: 'Pode executar (deny-by-default)' }, 'autorizada')),
      rows.map((t) => h('tr',
        h('td', h('b', t.tool_id), h('div.faint.small', short(t.description, 90)), tryRow(ctx, t)),
        h('td', badge(...(SOURCE[t.source] || [t.source]))),
        h('td', riskBadge(t.risk)),
        h('td.small.dim', (t.tags || []).join(', ')),
        h('td', t.visible ? badge('sim', 'green') : badge('não')),
        h('td', h('label.switch', h('input', {
          type: 'checkbox', checked: t.authorized,
          onchange: (e) => act(null, async () => {
            await ctx.api.post('/api/tools/authorize', { symbiote: ctx.symbiote, tool_id: t.tool_id, authorized: e.target.checked });
            ctx.refresh('tools');
          }),
        }), t.visible && !t.authorized ? h('span.small', { style: { color: 'var(--amber)' } }, 'vê, não pode') : ''))))));
}

function tagsEditor(ctx, data) {
  const input = h('input', { value: data.tool_tags.join(', '), placeholder: 'vazio = vê todas as tools registradas' });
  return h('div.stack',
    h('p.dim.small', 'Se houver tags, o modelo só vê as tools com alguma delas. Tools sem tag (fs_*, search_*, spawn, MCP) somem junto. ',
      'Tags disponíveis: ', data.available_tags.map((t) => badge(t))),
    h('div.row', input, h('button.btn', {
      onclick: (e) => act(e.currentTarget, async () => {
        const tags = input.value.split(',').map((s) => s.trim()).filter(Boolean);
        await ctx.api.post('/api/tools/tags', { symbiote: ctx.symbiote, tags });
        ctx.refresh('tools');
      }, 'tool_tags atualizadas'),
    }, 'aplicar')));
}

function discovery(ctx, data) {
  const pending = data.discovered.filter((t) => t.status === 'pending').length;
  const tags = [...new Set(data.discovered.flatMap((t) => t.tags))];
  const setStatus = (body) => act(null, async () => {
    await ctx.api.post('/api/discovery/status', { symbiote: ctx.symbiote, ...body });
    ctx.refresh('tools');
  });
  return h('div.card',
    h('div.card-title', h('h3', '1 · Descoberta via OpenAPI'), h('span.faint.small', data.pa_base + '/openapi.json')),
    h('p.dim.small', 'O kernel lê a especificação OpenAPI de um serviço e transforma cada operação em uma tool HTTP: ',
      h('code', 'operation_id'), ' vira o id, as ', h('code', 'tags'), ' controlam a visibilidade e ', h('code', 'x-risk-level'),
      ' marca o que precisa de aprovação. Tudo nasce ', h('b', 'pendente'), '; só o que você aprovar é carregado.'),
    h('div.row', { style: { marginBottom: '8px' } },
      h('button.btn.accent', {
        onclick: (e) => act(e.currentTarget, async () => {
          const r = await ctx.api.post('/api/discovery/run', { symbiote: ctx.symbiote });
          if (r.errors.length) toast(r.errors.join('; '), 'warn');
          ctx.refresh('tools');
        }, 'Descoberta concluída'),
      }, 'Descobrir na API pessoal'),
      pending ? h('button.btn', {
        onclick: (e) => act(e.currentTarget, async () => {
          for (const t of data.discovered.filter((x) => x.status === 'pending')) {
            await ctx.api.post('/api/discovery/status', { symbiote: ctx.symbiote, tool_id: t.tool_id, status: 'approved' });
          }
          ctx.refresh('tools');
        }, 'Todas aprovadas. Agora carregue no kernel.'),
      }, `Aprovar as ${pending} pendentes`) : '',
      h('button.btn', {
        onclick: (e) => act(e.currentTarget, async () => {
          const r = await ctx.api.post('/api/discovery/load', { symbiote: ctx.symbiote });
          ctx.refresh('tools');
          return r;
        }, 'Aprovadas carregadas no kernel e autorizadas'),
      }, 'Carregar aprovadas no kernel')),
    tags.length ? h('div.row.small', h('span.dim', 'Aprovar por tag:'), tags.map((tag) =>
      h('button.btn.sm', { onclick: () => setStatus({ tag, status: 'approved' }) }, tag))) : '',
    data.discovered.length ? h('table', { style: { marginTop: '8px' } },
      h('tr', h('th', 'tool'), h('th', 'método'), h('th', 'risco'), h('th', 'tags'), h('th', 'status')),
      data.discovered.map((t) => h('tr',
        h('td', h('b', t.tool_id), h('div.faint.small', short(t.url_template, 70))),
        h('td.mono', t.method), h('td', riskBadge(t.risk_level)), h('td.small.dim', t.tags.join(', ')),
        h('td', select('status', [['pending', 'pendente'], ['approved', 'aprovada'], ['disabled', 'desativada']], t.status,
          (e) => setStatus({ tool_id: t.tool_id, status: e.target.value }))))))
      : empty('Nada descoberto ainda.'));
}

function httpTools(ctx, data) {
  const form = h('form.stack', {
    onsubmit: async (e) => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.currentTarget));
      let parameters = {};
      try { parameters = JSON.parse(f.parameters || '{}'); } catch { toast('parâmetros: JSON inválido', 'err'); return; }
      await act(e.submitter, () => ctx.api.post('/api/http-tools', { symbiote: ctx.symbiote, ...f, parameters }), `Tool ${f.tool_id} registrada e autorizada`);
      ctx.refresh('tools');
    },
  },
  h('div.form-grid',
    h('label', 'tool_id', h('input', { name: 'tool_id', required: true, placeholder: 'feriados_nacionais' })),
    h('label', 'Método', select('method', ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'], 'GET')),
    h('label', 'Risco', select('risk', ['low', 'medium', 'high'], 'low'))),
  h('label', 'URL (com {placeholders})', h('input', { name: 'url_template', required: true, placeholder: 'https://brasilapi.com.br/api/feriados/v1/{ano}' })),
  h('label', 'Descrição', h('input', { name: 'description', placeholder: 'Feriados nacionais de um ano' })),
  h('label', 'Parâmetros (JSON Schema)', h('textarea', { name: 'parameters', rows: 3 }, '{"type": "object", "properties": {"ano": {"type": "integer"}}, "required": ["ano"]}')),
  h('div.row.end', h('button.btn', { type: 'submit' }, 'Registrar')));
  return h('div.card',
    h('h3', '2 · HTTP declarativa'),
    h('p.dim.small', 'Sem código: método, URL com placeholders e um JSON Schema. O kernel valida a URL contra SSRF, codifica os valores e conecta no IP que validou.'),
    h('div.row', { style: { marginBottom: '8px' } }, data.http_examples.map((ex) => h('button.btn.sm', {
      title: ex.url_template,
      onclick: (e) => act(e.currentTarget, async () => {
        await ctx.api.post('/api/http-tools', { symbiote: ctx.symbiote, example: ex.tool_id });
        ctx.refresh('tools');
      }, `${ex.tool_id} registrada e autorizada`),
    }, `+ ${ex.name}`))),
    fold('outra tool HTTP', form));
}

function otherSources(data) {
  const mcp = data.tools.filter((t) => t.source === 'mcp');
  const py = data.tools.filter((t) => t.source === 'python');
  return h('div.card',
    h('h3', '3 · Handler Python, MCP e builtins'),
    h('p.dim.small', h('b', 'Python'), ': uma função registrada com ', h('code', 'register_descriptor'), ' — ',
      py.map((t) => h('code', t.tool_id + ' ')), '. A ', h('code', 'consultar_cotacao'), ' falha de propósito para mostrar o circuit breaker.'),
    h('p.dim.small', h('b', 'MCP'), ': um servidor MCP de exemplo (', h('code', 'demo/mcp_server.py'), ') rodando por stdio, carregado com ',
      h('code', 'kernel.load_mcp_tools'), ' — ', mcp.length ? mcp.map((t) => h('code', t.tool_id + ' ')) : h('span.error', data.mcp_error || 'indisponível')),
    h('p.dim.small', h('b', 'Builtins'), ': ', h('code', 'fs_*'), ' e ', h('code', 'bash'), ' ficam presos à pasta sandbox do demo (',
      h('code', 'fs_allowed_root'), '); ', h('code', 'bash'), ' é risco alto e pede aprovação.'));
}

function approvals(ctx, data) {
  const modes = {
    low: 'todas as tools', medium: 'risco médio e alto', high: 'risco alto',
    none: 'só ações no Chrome', allow_all: 'permitir tudo',
  };
  const mode = data.approval_mode || (data.approvals_enabled ? 'high' : 'none');
  return h('div.card',
    h('div.card-title', h('h3', 'Aprovação humana'), badge(modes[mode] || mode, mode === 'allow_all' ? 'amber' : 'blue')),
    h('p.dim.small', mode === 'allow_all'
      ? 'Todas as tools executam sem pedir confirmação, inclusive cliques e preenchimentos no seu Chrome.'
      : mode === 'none'
        ? 'Só cliques e preenchimentos no seu Chrome pedem confirmação.'
        : `Pede confirmação para ${modes[mode]}; ações no seu Chrome também pedem confirmação.`),
    h('button.btn.sm', { type: 'button', onclick: () => ctx.openTab('chat') }, 'Alterar ao lado do chat'));
}

function audit(data) {
  if (!data.audit.length) return empty('Nenhuma execução ainda.');
  return h('table',
    h('tr', h('th', 'quando'), h('th', 'tool'), h('th', 'ação'), h('th', 'resultado'), h('th', 'parâmetros')),
    data.audit.map((a) => h('tr',
      h('td.small.faint', fmtTime(a.created_at)), h('td', h('b', a.tool_id)),
      h('td', a.action === 'blocked' ? badge('bloqueada', 'red') : badge('executou')),
      h('td', a.result === 'success' ? badge('ok', 'green') : badge(a.result, 'red')),
      h('td.mono.small.dim', short(JSON.stringify(a.params), 80)))));
}

async function personalData(ctx) {
  const pa = await ctx.api.get('/api/pa');
  const table = (rows, cols) => rows.length ? h('table', h('tr', cols.map(([, l]) => h('th', l))),
    rows.map((r) => h('tr', cols.map(([k]) => h('td.small', String(r[k] ?? '')))))) : empty('vazio');
  return h('div.grid2',
    h('div', h('h4', 'Tarefas'), table(pa.tasks, [['title', 'tarefa'], ['due', 'prazo'], ['priority', 'prioridade'], ['done', 'feita']])),
    h('div', h('h4', 'Agenda'), table(pa.events, [['title', 'compromisso'], ['start', 'início'], ['location', 'local']])),
    h('div', h('h4', 'Despesas'), table(pa.expenses, [['day', 'dia'], ['description', 'descrição'], ['category', 'categoria'], ['amount', 'R$']])),
    h('div', h('h4', 'Caixa de saída (e-mails “enviados”)'), table(pa.outbox, [['sent_at', 'quando'], ['recipient', 'para'], ['subject', 'assunto']])));
}

export default {
  id: 'tools',
  title: 'Tools',
  watch: ['turn_end', 'tools', 'discovery'],
  async render(el, ctx) {
    const data = await ctx.api.get(`/api/tools?symbiote=${ctx.symbiote}`);
    const authorizedCount = data.tools.filter((t) => t.authorized).length;
    const visibleCount = data.tools.filter((t) => t.visible).length;
    mount(el,
      h('p.tab-intro', 'Tools chegam ao kernel de cinco jeitos e todas passam pelo mesmo ', h('b', 'ToolGateway'), ': ',
        h('b', 'deny-by-default'), ' (só roda o que foi autorizado para o symbiote), hooks, timeout e audit log. ',
        'Ser ', h('b', 'visível'), ' (aparecer no prompt) e ser ', h('b', 'autorizada'), ' (poder executar) são coisas separadas.'),
      h('div.stats',
        h('div.stat', h('span.v', data.tools.length), h('span.l', 'registradas no kernel')),
        h('div.stat', h('span.v', visibleCount), h('span.l', 'visíveis para este symbiote')),
        h('div.stat', h('span.v', authorizedCount), h('span.l', 'autorizadas')),
        h('div.stat', h('span.v', data.audit.filter((a) => a.action === 'blocked').length), h('span.l', 'bloqueios recentes'))),
      discovery(ctx, data),
      h('div.grid2', { style: { marginTop: '12px' } }, httpTools(ctx, data), otherSources(data), approvals(ctx, data)),
      h('div.card', { style: { marginTop: '12px' } }, h('h3', 'Visibilidade por tag'), tagsEditor(ctx, data)),
      h('div.card', h('h3', 'Registradas × visíveis × autorizadas'), matrix(ctx, data)),
      h('div.card', h('h3', 'Audit log'), audit(data)),
      h('div.card', h('h3', 'Dados da API pessoal (fictícios)'),
        h('p.dim.small', 'O que as tools descobertas leem e alteram. Útil para conferir que “apagar” e “enviar e-mail” só acontecem depois da aprovação.'),
        await personalData(ctx)),
    );
  },
};
