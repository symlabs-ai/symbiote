// "Sistema": identity, knowledge, sessions, the six capabilities, security
// checks, retention, the CLI/Console on the same database, and settings.

import { h, mount, badge, empty, act, fmtTime, pre, fold, short, select, toast, confirmDialog, score } from '../lib.js';

async function sessionsCard(ctx) {
  const sessions = await ctx.api.get(`/api/sessions?symbiote=${ctx.symbiote}`);
  if (!sessions.length) return empty('Sem sessões.');
  return h('table',
    h('tr', h('th', 'sessão'), h('th', 'status'), h('th', 'início'), h('th.num', 'score'), h('th', 'resumo')),
    sessions.map((s) => {
      const msgs = h('div');
      return h('tr',
        h('td.mono', s.id.slice(0, 8), s.id === ctx.sessionId ? h('div', badge('atual', 'green')) : ''),
        h('td', s.status === 'active' ? badge('aberta', 'blue') : badge('encerrada')),
        h('td.small.faint', fmtTime(s.started_at)),
        h('td.num', score(s.final_score)),
        h('td', h('details', {
          ontoggle: async (e) => {
            if (!e.target.open || msgs.childElementCount) return;
            const list = await ctx.api.get(`/api/sessions/${s.id}/messages`);
            mount(msgs, list.length ? list.map((m) => h('div.small', badge(m.role, m.role === 'user' ? 'blue' : ''), ' ', short(m.content, 300)))
              : empty('sem mensagens'));
          },
        }, h('summary', short(s.summary || s.goal || '—', 80)), msgs)));
    }));
}

async function knowledgeCard(ctx) {
  const items = await ctx.api.get(`/api/knowledge?symbiote=${ctx.symbiote}`);
  const out = h('div');
  return h('div.stack',
    items.length ? h('div.stack', items.map((k) => h('div.row',
      badge(k.type), h('b', k.name), h('span.dim.small', { style: { flex: 1 } }, short(k.content, 120)),
      h('button.btn.sm.ghost', {
        onclick: (e) => act(e.currentTarget, async () => { await ctx.api.del(`/api/knowledge/${k.id}`); ctx.refresh('system'); }),
      }, 'remover')))) : empty('Sem knowledge.'),
    fold('adicionar', h('form.stack', {
      onsubmit: async (e) => {
        e.preventDefault();
        const f = Object.fromEntries(new FormData(e.currentTarget));
        await act(e.submitter, () => ctx.api.post('/api/knowledge', { symbiote: ctx.symbiote, ...f }), 'Knowledge registrado');
        ctx.refresh('system');
      },
    }, h('div.form-grid', h('label', 'Nome', h('input', { name: 'name', required: true })),
      h('label', 'Tipo', select('type', ['note', 'document', 'reference'], 'note'))),
    h('label', 'Conteúdo', h('textarea', { name: 'content', rows: 3, required: true })),
    h('div.row.end', h('button.btn', { type: 'submit' }, 'Adicionar')))),
    h('form.row', {
      onsubmit: async (e) => {
        e.preventDefault();
        const query = new FormData(e.currentTarget).get('query');
        const r = await act(e.submitter, () => ctx.api.post('/api/knowledge/query', { symbiote: ctx.symbiote, query }));
        if (r) mount(out, r.length ? r.map((k) => h('div.small', badge('casou', 'green'), ' ', h('b', k.name))) : empty('nada casou'));
      },
    }, h('input', { name: 'query', placeholder: 'Testar busca: quando é a mudança?', style: { flex: 1 } }), h('button.btn', { type: 'submit' }, 'buscar')),
    out);
}

async function personaCard(ctx) {
  const p = await ctx.api.get(`/api/persona?symbiote=${ctx.symbiote}`);
  const area = h('textarea', { rows: 10 }, JSON.stringify(p.persona, null, 2));
  return h('div.stack',
    h('p.dim.small', 'A persona vira a primeira parte do prompt de sistema. Toda mudança fica registrada em persona_audit.'),
    area,
    h('div.row.end', h('button.btn', {
      onclick: (e) => act(e.currentTarget, async () => {
        let persona;
        try { persona = JSON.parse(area.value); } catch { throw new Error('JSON inválido'); }
        await ctx.api.post('/api/persona', { symbiote: ctx.symbiote, persona });
      }, 'Persona atualizada: vale na próxima mensagem.'),
    }, 'Salvar persona')));
}

function capabilitiesCard(ctx) {
  const out = h('div');
  const input = h('input', { placeholder: 'tema (teach/show) ou "processo: tarefa" (work)', value: 'mudança', style: { flex: 1 } });
  const run = (name) => (e) => act(e.currentTarget, async () => {
    const r = await ctx.api.post('/api/capability', { symbiote: ctx.symbiote, name, query: input.value });
    mount(out, pre(r.result));
  });
  return h('div.stack',
    h('p.dim.small', 'Além de ', h('b', 'chat'), ' e ', h('b', 'learn'), ' (usados no resto do demo), o kernel expõe ', h('b', 'teach'),
      ' (explica o que sabe sobre um tema), ', h('b', 'show'), ' (mostra mensagens, memórias e knowledge), ', h('b', 'reflect'),
      ' (estatísticas da sessão) e ', h('b', 'work'), ' (despacha uma tarefa para o runner certo). Nenhuma delas chama o LLM, exceto work com chat.'),
    h('div.row', input, ['teach', 'show', 'reflect', 'work'].map((n) => h('button.btn.sm', { onclick: run(n) }, n))),
    out);
}

function securityCard(ctx) {
  const out = h('div');
  const run = (test) => (e) => act(e.currentTarget, async () => {
    const r = await ctx.api.post('/api/security', { test });
    mount(out, h('div.row', r.blocked ? badge('bloqueado', 'green') : badge('NÃO bloqueou', 'red')), pre(r.error || r.output));
  });
  return h('div.stack',
    h('p.dim.small', 'Chamadas diretas ao ToolGateway, como se o modelo tivesse pedido.'),
    h('div.row',
      h('button.btn.sm', { onclick: run('ssrf'), title: 'HTTP tool apontando para 169.254.169.254' }, 'SSRF: metadados da nuvem'),
      h('button.btn.sm', { onclick: run('fs'), title: 'fs_read em ../../../../etc/hostname' }, 'Ler fora da sandbox'),
      h('button.btn.sm', { onclick: run('policy'), title: 'Sol tentando usar browser_navigate' }, 'Tool não autorizada')),
    out);
}

export default {
  id: 'system',
  title: 'Sistema',
  watch: ['learned', 'session'],
  async render(el, ctx) {
    const [sys, sessions, knowledge, persona] = await Promise.all([
      ctx.api.get('/api/system'), sessionsCard(ctx), knowledgeCard(ctx), personaCard(ctx),
    ]);
    const s = ctx.state.status.settings;
    const pruneOut = h('div');
    mount(el,
      h('p.tab-intro', 'O resto do kernel: identidade, knowledge, sessões, as seis capabilities e as proteções. Todo o estado vive num único SQLite, ',
        'que a CLI e o Console admin do Symbiote também abrem.'),
      h('div.grid2',
        h('div.card', h('h3', 'Persona'), persona),
        h('div.card', h('h3', 'Knowledge'), h('p.dim.small', 'Documentos e notas que entram no contexto quando a mensagem tem termos em comum (ou via search_knowledge no modo on_demand).'), knowledge)),
      h('div.card', { style: { marginTop: '12px' } }, h('h3', 'Sessões'), sessions),
      h('div.grid2', { style: { marginTop: '12px' } },
        h('div.card', h('h3', 'Capabilities'), capabilitiesCard(ctx)),
        h('div.card', h('h3', 'Proteções'), securityCard(ctx))),
      h('div.grid2', { style: { marginTop: '12px' } },
        h('div.card', h('h3', 'Mesmo banco, outras interfaces'),
          h('p.dim.small', 'Com o demo rodando, a CLI e o Console admin leem o mesmo arquivo:'),
          sys.commands.map((c) => pre(c)),
          h('p.faint.small', `Sandbox dos arquivos: ${sys.sandbox}`)),
        h('div.card', h('h3', 'Retenção'),
          h('p.dim.small', h('code', 'kernel.prune()'), ' apaga mensagens e traces de sessões encerradas há mais de N dias (memórias e scores ficam). Aqui em simulação, com N = 0.'),
          h('button.btn.sm', {
            onclick: (e) => act(e.currentTarget, async () => mount(pruneOut, pre(await ctx.api.post('/api/prune')))),
          }, 'prune (dry run)'), pruneOut)),
      h('div.card', { style: { marginTop: '12px' } },
        h('h3', 'Configuração do demo'),
        h('p', badge(s.provider, 'purple'), ' ', `${s.server} · principal ${s.model} · auxiliar ${s.aux_model} · chave ${s.api_key}`,
          s.gateway_project ? ` (projeto ${s.gateway_project})` : ''),
        h('p.dim.small', 'Servidor, chave e modelos são editados e testados nas configurações (a engrenagem no topo da página).'),
        h('div.row',
          h('button.btn', { onclick: () => ctx.showSetup(true) }, 'Abrir configurações do LLM'),
          h('button.btn.danger', {
            onclick: async (e) => {
              const ok = await confirmDialog('Zerar o demo?', 'Apaga memórias, sessões, skills, tools descobertas e os dados da API pessoal, e começa do zero. A chave continua salva.');
              if (!ok) return;
              await act(e.currentTarget, () => ctx.api.post('/api/reset'));
              toast('Demo zerado.');
              location.reload();
            },
          }, 'Zerar demo'))),
    );
  },
};
