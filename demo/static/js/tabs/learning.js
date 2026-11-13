// "Aprendizado": what closing a session produced (score, reflection, new
// memories), reflection and dream settings, dream reports and score history.

import { h, mount, badge, empty, act, fmtTime, score, select, pre, fold, short } from '../lib.js';

const STOP = {
  end_turn: 'terminou normalmente', max_iterations: 'limite de iterações', stagnation: 'estagnação',
  circuit_breaker: 'circuit breaker', timeout: 'timeout',
};

function scoreBars(scores) {
  if (!scores.length) return empty('Sem sessões pontuadas ainda.');
  const ordered = [...scores].reverse();
  return h('div',
    h('div.bars', ordered.map((s) => {
      const v = s.final_score ?? 0;
      return h(`span${v < 0.5 ? '.low' : v < 0.8 ? '.mid' : ''}`, {
        style: { height: `${Math.max(4, v * 100)}%` },
        title: `${score(v)} · ${STOP[s.stop_reason] || s.stop_reason || 'sem tools'} · ${fmtTime(s.computed_at)}`,
      });
    })),
    h('p.faint.small', 'Uma barra por sessão encerrada (mais antiga à esquerda). Verde ≥ 0,8; âmbar entre 0,5 e 0,8; vermelho < 0,5.'));
}

function closeCard(close, ctx) {
  if (!close) {
    return h('div.card', h('h3', 'Encerre uma sessão para ver o que foi aprendido'),
      h('p.dim', 'O aprendizado não acontece a cada mensagem: ele roda quando a sessão é encerrada (botão ',
        h('b', 'Encerrar sessão e aprender'), '). Nessa hora o kernel calcula o score, grava uma memória de falha se o loop travou, ',
        'roda a reflexão sobre o que você disse, guarda o handoff do long-run, dispara o review de skills e, a cada N sessões, o dream.'));
  }
  const s = close.score;
  const feedback = (value) => (e) => act(e.currentTarget, async () => {
    await ctx.api.post('/api/feedback', { session_id: close.closed_session, score: value });
    ctx.refresh('learning');
  }, value >= 0.5 ? 'Feedback positivo registrado: final = 0,6·auto + 0,4·usuário.' : 'Feedback negativo registrado.');
  return h('div.card',
    h('div.card-title', h('h3', 'Última sessão encerrada'), h('span.mono.faint', close.closed_session.slice(0, 8))),
    h('div.stats',
      h('div.stat', h('span.v', score(s?.auto_score)), h('span.l', 'score automático')),
      h('div.stat', h('span.v', score(s?.user_score)), h('span.l', 'feedback do usuário')),
      h('div.stat', h('span.v', score(s?.final_score)), h('span.l', 'score final')),
      h('div.stat', h('span.v', { style: { fontSize: '14px' } }, STOP[s?.stop_reason] || s?.stop_reason || 'sem tools'), h('span.l', 'como o último loop parou'))),
    h('div.row', h('span.dim', 'Como foi?'), h('button.btn.sm', { onclick: feedback(1) }, '👍 boa'), h('button.btn.sm', { onclick: feedback(0) }, '👎 ruim')),
    h('h4', `Memórias criadas (${close.learned.length}) · reflexão em modo ${close.reflection_mode}`),
    close.learned.length ? h('div.stack', close.learned.map((m) => h('div.row',
      badge(m.category, { declarative: 'blue', procedural: 'green', meta: 'purple', handoff: 'amber' }[m.category] || ''),
      badge(m.type), badge(m.source), h('span', { style: { flex: 1 } }, m.content))))
      : empty(close.reflection_mode === 'keyword'
        ? 'Nada novo. No modo keyword a reflexão só aprende frases do usuário com marcadores como “prefiro”, “sempre”, “nunca”, “decidi”, “regra”.'
        : 'Nada novo nesta sessão.'),
    close.reflection ? fold('Trilha da reflexão (reflection_audit)', pre({
      modo: close.reflection.mode, keyword: close.reflection.keyword_facts, llm: close.reflection.llm_facts, erro: close.reflection.llm_error,
    })) : '',
    close.summary ? fold('Resumo gravado na sessão', pre(close.summary)) : '');
}

export default {
  id: 'learning',
  title: 'Aprendizado',
  watch: ['learned', 'dream'],
  async render(el, ctx) {
    const data = await ctx.api.get(`/api/learning?symbiote=${ctx.symbiote}`);
    const close = ctx.state.lastClose || data.last_close;
    const cfg = data.config;
    const save = (field) => async (e) => {
      const value = e.target.type === 'number' ? Number(e.target.value) : e.target.value;
      await act(null, () => ctx.api.post('/api/learning/config', { symbiote: ctx.symbiote, [field]: value }), `${field} = ${value}`);
      ctx.refresh('learning');
    };
    const dream = (dryRun) => (e) => act(e.currentTarget, async () => {
      await ctx.api.post('/api/dream', { symbiote: ctx.symbiote, dry_run: dryRun });
      ctx.refresh('learning');
    }, dryRun ? 'Simulação concluída: nada foi alterado.' : 'Dream concluído.');

    const dreams = data.dreams.length ? h('div.stack', data.dreams.map((r) => h('div.card.tight',
      h('div.row', badge(r.dream_mode, 'purple'), r.dry_run ? badge('simulação', 'amber') : badge('aplicado', 'green'),
        h('span.faint.small', `${fmtTime(r.started_at)} · ${r.total_llm_calls}/${r.max_llm_calls} chamadas LLM`)),
      h('table', h('tr', h('th', 'fase'), h('th.num', 'propostas'), h('th.num', 'aplicadas'), h('th', 'detalhes')),
        (r.phases || []).map((p) => h('tr',
          h('td', p.phase), h('td.num', p.actions_proposed), h('td.num', p.actions_applied),
          h('td', p.error ? h('span.error', short(p.error, 120)) : p.details?.length
            ? fold(`${p.details.length} item(ns)`, pre(p.details)) : h('span.faint', '—'))))))))
      : empty('Nenhum dream ainda.');

    mount(el,
      h('p.tab-intro', 'O symbiote melhora entre sessões. Ao encerrar uma sessão o kernel ', h('b', 'pontua'), ' o que aconteceu, ',
        h('b', 'reflete'), ' sobre o que você disse e grava ', h('b', 'memórias'), '; de tempos em tempos o ', h('b', 'dream'),
        ' poda e consolida essas memórias em segundo plano.'),
      closeCard(close, ctx),
      h('div.grid2', { style: { marginTop: '12px' } },
        h('div.card',
          h('h3', 'Reflexão'),
          h('p.dim.small', h('b', 'keyword'), ': regras sobre as frases do usuário, sem LLM. ', h('b', 'llm'), ': o modelo auxiliar extrai fatos. ',
            h('b', 'hybrid'), ': roda os dois e audita a diferença. ', h('b', 'llm_main'), ': usa o modelo principal.'),
          h('label', 'Modo', select('reflection_mode', ['keyword', 'llm', 'hybrid', 'llm_main'], cfg.reflection_mode, save('reflection_mode'))),
          data.reflections.length ? fold(`Auditorias recentes (${data.reflections.length})`, h('div.stack', data.reflections.map((r) =>
            h('div.small', badge(r.mode), ' ', fmtTime(r.created_at), ` · keyword ${(r.keyword_facts || []).length} / llm ${(r.llm_facts || []).length}`,
              r.llm_error ? h('span.error', ` · ${short(r.llm_error, 80)}`) : '')))) : ''),
        h('div.card',
          h('h3', 'Dream'),
          h('p.dim.small', h('b', 'light'), ': poda e reconcilia sem LLM. ', h('b', 'full'), ': também generaliza, minera padrões e avalia, com orçamento de chamadas. ',
            `Dispara sozinho a cada ${cfg.dream_min_sessions} sessões encerradas (${data.sessions_since_dream} desde o último).`),
          h('div.form-grid',
            h('label', 'Modo', select('dream_mode', ['off', 'light', 'full'], cfg.dream_mode, save('dream_mode'))),
            h('label', 'Sessões para disparar', h('input', { type: 'number', min: 1, max: 100, value: cfg.dream_min_sessions, onchange: save('dream_min_sessions') }))),
          h('div.row.end', { style: { marginTop: '8px' } },
            h('button.btn', { onclick: dream(true) }, 'Simular (dry run)'),
            h('button.btn.accent', { onclick: dream(false) }, 'Sonhar agora')))),
      h('div.card', { style: { marginTop: '12px' } }, h('h3', 'Scores das sessões'), scoreBars(data.scores)),
      h('div', { style: { marginTop: '12px' } }, h('h4', 'Relatórios de dream'), dreams),
    );
  },
};
