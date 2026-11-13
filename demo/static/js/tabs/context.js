// "Contexto": what the kernel put in front of the model on the last turn.

import { h, mount, badge, empty, pre, fold, meter, short } from '../lib.js';

function personaView(persona) {
  if (!persona || !Object.keys(persona).length) return empty('sem persona');
  return h('div.stack', Object.entries(persona).map(([k, v]) =>
    h('div', h('span.faint.small', k), h('div', { style: { whiteSpace: 'pre-wrap' } }, typeof v === 'string' ? v : JSON.stringify(v)))));
}

function messagesView(call) {
  if (!call) return empty('Nenhuma chamada ainda nesta sessão.');
  return h('div.stack',
    h('div.row', badge(call.purpose, 'purple'), badge(call.model), call.tools?.length ? badge(`${call.tools.length} tools enviadas como schema nativo`, 'blue') : '',
      h('span.faint.small', `${call.messages.length} mensagens`)),
    call.messages.map((m, i) => fold(
      h('span', badge(m.role, m.role === 'system' ? 'purple' : m.role === 'user' ? 'blue' : m.role === 'tool' ? 'green' : ''),
        ' ', h('span.dim', short(typeof m.content === 'string' ? m.content.replace(/\s+/g, ' ') : JSON.stringify(m.tool_calls || m.content), 110))),
      pre(typeof m.content === 'string' ? m.content : JSON.stringify(m, null, 2)),
      i === 0 && false)));
}

export default {
  id: 'context',
  title: 'Contexto',
  watch: ['turn_end'],
  async render(el, ctx) {
    const data = await ctx.api.get(`/api/context?symbiote=${ctx.symbiote}`);
    const c = data.context;
    const intro = h('p.tab-intro', 'A cada turno o ', h('b', 'ContextAssembler'),
      ' monta o que o modelo vai ver: persona, memórias e knowledge relevantes (dentro de um orçamento de tokens), as tools visíveis e o contexto injetado pelo host. ',
      'Abaixo, o contexto do último turno desta sessão e as mensagens exatas enviadas ao modelo.');
    if (!c) {
      mount(el, intro, empty('Ainda não houve turno nesta sessão. Mande uma mensagem no chat.'));
      return;
    }
    const authorized = new Set(c.authorized);
    const visibleNotAuth = c.tools.filter((t) => !authorized.has(t.tool_id));
    const pct = c.budget ? c.tokens / c.budget : 0;
    mount(el, intro,
      h('div.stats',
        h('div.stat', h('span.v', c.tool_mode), h('span.l', 'modo de execução')),
        h('div.stat', h('span.v', c.tool_loading), h('span.l', 'carga de tools')),
        h('div.stat', h('span.v', c.context_mode), h('span.l', 'memória/knowledge')),
        h('div.stat', h('span.v', `${c.tokens}`), h('span.l', `tokens estimados de ${c.budget}`),
          meter(c.tokens, c.budget, pct > 1 ? 'red' : pct > 0.8 ? 'amber' : ''))),
      h('div.grid2',
        h('div.card', h('h3', 'Persona'), personaView(c.persona)),
        h('div.card',
          h('h3', `Memórias injetadas (${c.memories.length})`),
          c.context_mode === 'on_demand'
            ? empty('Modo on_demand: nada é injetado; o modelo busca com search_memories quando precisa.')
            : c.memories.length ? h('div.stack', c.memories.map((m) =>
              h('div.row', badge(m.type, 'purple'), h('span', { style: { flex: 1 } }, m.content), h('span.faint.small', `imp ${m.importance}`))))
              : empty('Nenhuma memória relevante para esta mensagem.'),
          h('h3', { style: { marginTop: '12px' } }, `Knowledge injetado (${c.knowledge.length})`),
          c.knowledge.length ? h('div.stack', c.knowledge.map((k) => fold(h('b', k.name), h('div.dim', k.content))))
            : empty('Nenhum knowledge relevante.'))),
      h('div.grid2', { style: { marginTop: '12px' } },
        h('div.card',
          h('h3', 'Histórico injetado pelo host'),
          h('p.dim.small', 'O kernel grava as mensagens, mas não as recoloca no prompt. O demo passa as últimas como ',
            h('code', 'extra_context["conversation_history"]'), ' a cada turno.'),
          c.history ? pre(c.history) : empty('Primeira mensagem da sessão: sem histórico.'),
          Object.keys(c.extra_context).length ? h('div', h('h4', 'Outro extra_context'), pre(c.extra_context)) : ''),
        h('div.card',
          h('h3', `Tools visíveis ao modelo (${c.tools.length})`),
          visibleNotAuth.length ? h('p.small', { style: { color: 'var(--amber)' } },
            `${visibleNotAuth.length} visíveis mas não autorizadas: o modelo as vê, mas a política bloqueia se ele tentar. Visibilidade é por tag; autorização é outra lista.`) : '',
          h('div.chips', c.tools.map((t) => h('span.badge' + (authorized.has(t.tool_id) || ['get_tool_schema'].includes(t.tool_id) ? '.green' : '.amber'),
            t.tool_id + (c.tool_loading === 'index' && !t.full ? ' (índice)' : '')))),
          c.skills?.length ? h('div', h('h4', 'Skills injetadas'), h('div.chips', c.skills.map((s) => badge(s.name || String(s), 'purple')))) : '')),
      h('div.card', { style: { marginTop: '12px' } },
        h('h3', 'Mensagens enviadas ao modelo (última chamada)'),
        messagesView(data.last_call)),
    );
  },
};
