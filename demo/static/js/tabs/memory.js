// "Memória": the symbiote's long-term memory, the recall ranking and isolation.

import { h, mount, badge, empty, act, fmtTime, meter, select, toast } from '../lib.js';

const CATEGORY_COLOR = { declarative: 'blue', procedural: 'green', meta: 'purple', handoff: 'amber', ephemeral: '' };
const SOURCE_LABEL = { user: 'ensinado', reflection: 'reflexão', system: 'sistema', inference: 'inferência' };
const seenKey = (sid) => `demo-mem-seen-${sid}`;
let filter = 'all';
let showInactive = false;

function lastSeen(sid) {
  try { return localStorage.getItem(seenKey(sid)) || ''; } catch { return ''; }
}
function markSeen(sid) {
  try { localStorage.setItem(seenKey(sid), new Date().toISOString()); } catch { /* private mode */ }
}

export default {
  id: 'memory',
  title: 'Memória',
  watch: ['learned', 'memory', 'turn_end', 'dream'],
  async render(el, ctx) {
    const sid = ctx.symbioteId;
    const seen = lastSeen(sid);
    const data = await ctx.api.get(`/api/memory?symbiote=${ctx.symbiote}&inactive=${showInactive}`);
    const items = data.items.filter((m) => filter === 'all' || m.category === filter);
    const refresh = () => ctx.refresh('memory');

    const list = items.length ? h('div', items.map((m) => {
      const fresh = seen && m.created_at > seen;
      return h(`div.mem${m.is_active ? '' : '.inactive'}${fresh ? '.fresh' : ''}`,
        h('div.content', m.content),
        h('div.row', m.is_active ? h('button.btn.sm.ghost', {
          title: 'Soft delete: is_active = 0',
          onclick: (e) => act(e.currentTarget, async () => {
            await ctx.api.post(`/api/memory/${m.id}/deactivate`, { symbiote: ctx.symbiote });
            refresh();
          }),
        }, 'desativar') : badge('inativa')),
        h('div.tags',
          fresh ? badge('nova', 'green') : '',
          badge(m.category, CATEGORY_COLOR[m.category]), badge(m.type),
          badge(SOURCE_LABEL[m.source] || m.source),
          h('span.faint.small', `importância ${m.importance}`), meter(m.importance, 1),
          (m.tags || []).filter((t) => t).map((t) => h('span.faint.small', `#${t}`))),
        h('span.faint.small', `criada ${fmtTime(m.created_at)} · usada ${fmtTime(m.last_used_at)}`));
    })) : empty('Nenhuma memória nesta categoria ainda. Ensine algo no chat e encerre a sessão, ou use o formulário abaixo.');

    const learnForm = h('form.stack', {
      onsubmit: async (e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        await act(e.submitter, () => ctx.api.post('/api/memory/learn', {
          symbiote: ctx.symbiote, content: f.get('content'), type: f.get('type'), importance: Number(f.get('importance')),
        }), 'Fato gravado (capability learn).');
        refresh();
      },
    },
    h('label', 'Fato', h('input', { name: 'content', required: true, placeholder: 'Ex.: Meu time é o Coritiba' })),
    h('div.form-grid',
      h('label', 'Tipo', select('type', ['preference', 'constraint', 'factual', 'procedural', 'decision', 'relational'], 'preference')),
      h('label', 'Importância', h('input', { name: 'importance', type: 'number', min: 0, max: 1, step: 0.1, value: 0.7 }))),
    h('div.row.end', h('button.btn.accent', { type: 'submit' }, 'Ensinar')));

    const recallOut = h('div');
    const recallForm = h('form.stack', {
      onsubmit: async (e) => {
        e.preventDefault();
        const q = new FormData(e.currentTarget).get('query');
        const r = await act(e.submitter, () => ctx.api.post('/api/memory/recall', { symbiote: ctx.symbiote, query: q }));
        if (!r) return;
        mount(recallOut,
          h('p.small.dim', 'Termos comparados (sem stop-words, acentos e sufixos): ',
            r.terms.length ? r.terms.map((t) => badge(t, 'blue')) : '—'),
          r.results.length ? h('div.stack', r.results.map((m, i) => h('div.row',
            h('span.mono.faint', `#${i + 1}`), badge(m.type), h('span', { style: { flex: 1 } }, m.content),
            h('span.small', 'em comum: ', m.shared_terms.map((t) => badge(t, 'green'))))))
            : empty('Nada em comum: nenhuma memória entraria no contexto.'),
          h('p.small', { style: { marginTop: '8px', color: r.other_symbiote.length ? 'var(--amber)' : 'var(--dim)' } },
            r.other_symbiote.length
              ? `O outro symbiote tem ${r.other_symbiote.length} memória(s) que casariam com esta busca, mas elas nunca aparecem aqui: a memória é isolada por symbiote.`
              : 'Isolamento: a mesma busca não enxerga as memórias do outro symbiote.'));
      },
    },
    h('label', 'Frase', h('input', { name: 'query', required: true, placeholder: 'Ex.: como prefiro receber as respostas?' })),
    h('div.row.end', h('button.btn', { type: 'submit' }, 'Testar recall')));

    const cats = ['all', 'declarative', 'procedural', 'meta', 'handoff'];
    mount(el,
      h('p.tab-intro', 'Memória de longo prazo do ', h('b', ctx.state.status.symbiotes[ctx.symbiote].name),
        '. Ela cresce quando você ensina (', h('code', 'learn'), '), quando a sessão é encerrada (reflexão, memória de falha, handoff) e quando o dream consolida. ',
        'A cada turno as mais relevantes entram no contexto. Nada é apagado: desativar é soft delete.'),
      h('div.stats',
        h('div.stat', h('span.v', data.items.filter((m) => m.is_active).length), h('span.l', 'ativas')),
        Object.entries(data.by_category).map(([k, v]) => h('div.stat', h('span.v', v), h('span.l', k)))),
      h('div.row.between', { style: { marginBottom: '8px' } },
        h('div.chips', cats.map((c) => h(`button.chip${filter === c ? '.active' : ''}`, {
          onclick: () => { filter = c; ctx.refresh('memory'); },
        }, c === 'all' ? 'todas' : c))),
        h('div.row',
          h('label.switch', h('input', { type: 'checkbox', checked: showInactive, onchange: (e) => { showInactive = e.target.checked; refresh(); } }), 'mostrar inativas'),
          h('button.btn.sm', { onclick: () => { markSeen(sid); toast('Marcadas como vistas.'); refresh(); } }, 'marcar como vistas'))),
      h('div.card', list),
      h('div.grid2', { style: { marginTop: '12px' } },
        h('div.card', h('h3', 'Ensinar um fato'), h('p.dim.small', 'Grava direto, sem LLM (capability learn).'), learnForm),
        h('div.card', h('h3', 'Testar o recall'), h('p.dim.small', 'O mesmo ranking que decide quais memórias entram no prompt: termos em comum, importância e recência.'), recallForm, recallOut)),
    );
    if (!seen) markSeen(sid);
  },
};
