// "Harness": the self-tuning layer — scores, parameter tuner, prompt evolution
// with guard rails and rollback, cross-symbiote learning.

import { h, mount, badge, empty, act, fmtTime, pre, fold, meter, score, select, toast } from '../lib.js';

const LABEL = {
  tool_instructions: 'Instruções de uso de tools',
  injection_stagnation: 'Mensagem de estagnação',
  injection_circuit_breaker: 'Mensagem de circuit breaker',
};

let component = 'tool_instructions';

export default {
  id: 'harness',
  title: 'Harness',
  watch: ['learned'],
  async render(el, ctx) {
    const data = await ctx.api.get(`/api/harness?symbiote=${ctx.symbiote}`);
    const r = data.evolution_readiness;
    const ready = r.failed >= r.failed_needed && r.succeeded >= r.succeeded_needed;
    const t = data.tuning;
    const comp = data.components.find((x) => x.component === component) || data.components[0];
    const nextTier = t.tiers.find((x) => x.tier > t.tier);

    const versions = comp.versions.length ? h('table',
      h('tr', h('th', 'versão'), h('th', 'pai'), h('th.num', 'média'), h('th.num', 'sessões'), h('th', 'ativa'), h('th', 'criada')),
      comp.versions.map((v) => h('tr', h('td.mono', `v${v.version}`), h('td.mono', v.parent_version ?? '—'),
        h('td.num', score(v.avg_score)), h('td.num', v.session_count), h('td', v.is_active ? badge('ativa', 'green') : ''),
        h('td.small.faint', fmtTime(v.created_at)))))
      : empty('Só o texto padrão do kernel até agora.');

    mount(el,
      h('p.tab-intro', 'O harness é a camada que se ajusta sozinha a partir dos ', h('b', 'scores'), ' das sessões. O ',
        h('b', 'ParameterTuner'), ' recalibra números (iterações, compactação, fatias do contexto) por faixas de segurança; o ',
        h('b', 'HarnessEvolver'), ' pede ao modelo auxiliar uma versão melhor das instruções, valida com guard rails e ',
        h('b', 'reverte sozinho'), ' se a versão nova pontuar pior. Os números abaixo são reais: sem volume de sessões, ele espera.'),
      h('div.grid2',
        h('div.card',
          h('h3', 'Prontidão para evoluir (últimos 7 dias)'),
          h('div.stack',
            h('div', h('div.row.between', h('span', 'sessões com falha (score < 0,5)'), h('b', `${r.failed} de ${r.failed_needed}`)), meter(r.failed, r.failed_needed, 'red')),
            h('div', h('div.row.between', h('span', 'sessões bem-sucedidas (≥ 0,8)'), h('b', `${r.succeeded} de ${r.succeeded_needed}`)), meter(r.succeeded, r.succeeded_needed))),
          h('p.dim.small', { style: { marginTop: '8px' } }, ready
            ? 'Há dados suficientes: “Evoluir agora” vai propor uma versão nova.'
            : 'Ainda faltam sessões. “Evoluir agora” roda mesmo assim e mostra o motivo da recusa — é o guard rail funcionando.')),
        h('div.card',
          h('h3', `ParameterTuner · tier ${t.tier}`),
          h('p.dim.small', `${t.session_count} sessão(ões) pontuada(s) na janela. `,
            nextTier ? `O tier ${nextTier.tier} começa com ${nextTier.min}.` : 'Todos os tiers liberados.'),
          Object.keys(t.adjustments).length ? h('div.stack',
            pre(t.adjustments), pre(t.reasons),
            h('button.btn.accent', {
              onclick: (e) => act(e.currentTarget, async () => {
                await ctx.api.post('/api/harness/tune', { symbiote: ctx.symbiote });
                ctx.refresh('harness');
              }, 'Ajustes aplicados à configuração do symbiote'),
            }, 'Aplicar ajustes'))
            : empty(t.tier === 0 ? 'Tier 0: nenhum ajuste até 5 sessões.' : 'Nada a ajustar com os dados atuais.'))),
      h('div.card', { style: { marginTop: '12px' } },
        h('div.card-title', h('h3', 'Textos evoluíveis'),
          select('component', data.components.map((x) => [x.component, LABEL[x.component] || x.component]), comp.component,
            (e) => { component = e.target.value; ctx.refresh('harness'); })),
        h('div.row', { style: { marginBottom: '8px' } },
          comp.active ? badge(`ativa: v${comp.active.version} · média ${score(comp.active.avg_score)} em ${comp.active.session_count} sessões`, 'green') : badge('usando o texto padrão'),
          comp.baseline ? badge(`baseline: média ${score(comp.baseline.avg_score)} em ${comp.baseline.session_count} sessões`) : '',
          h('span.spacer'),
          h('button.btn', {
            onclick: async (e) => {
              const out = await act(e.currentTarget, () =>
                ctx.api.post('/api/harness/evolve', { symbiote: ctx.symbiote, component: comp.component }));
              if (!out) return;
              if (out.success) toast(`Nova versão v${out.new_version} criada e ativa.`);
              else toast(`Não evoluiu: ${out.reason}${out.guard_rail_failed ? ` (guard rail: ${out.guard_rail_failed})` : ''}`, 'warn');
              ctx.refresh('harness');
            },
          }, 'Evoluir agora'),
          h('button.btn.ghost', {
            onclick: (e) => act(e.currentTarget, async () => {
              const out = await ctx.api.post('/api/harness/rollback', { symbiote: ctx.symbiote, component: comp.component });
              ctx.refresh('harness');
              return out;
            }, 'Rollback feito: voltou para a versão de onde a atual derivou'),
          }, 'Rollback')),
        versions,
        fold('Texto padrão do kernel', pre(comp.default_text)),
        comp.active ? fold(`Texto ativo (v${comp.active.version})`, pre(comp.active.content)) : ''),
      h('div.card', { style: { marginTop: '12px' } },
        h('h3', 'Aprendizado entre symbiotes'),
        h('p.dim.small', 'Versões que funcionaram para outro symbiote com ferramentas parecidas (sobreposição de tools) e poderiam ser adotadas aqui.'),
        data.cross_learning.length ? pre(data.cross_learning) : empty('Nenhum candidato: o outro symbiote ainda não tem versões evoluídas com tools em comum.')),
      h('div.card', { style: { marginTop: '12px' } },
        h('h3', 'Scores recentes'),
        data.scores.length ? h('table', h('tr', h('th', 'sessão'), h('th.num', 'auto'), h('th.num', 'usuário'), h('th.num', 'final'), h('th', 'parada'), h('th.num', 'it.'), h('th.num', 'tools'), h('th', 'quando')),
          data.scores.slice(0, 20).map((s) => h('tr', h('td.mono', s.session_id.slice(0, 8)), h('td.num', score(s.auto_score)), h('td.num', score(s.user_score)),
            h('td.num', h('b', score(s.final_score))), h('td.small', s.stop_reason || 'sem tools'), h('td.num', s.total_iterations), h('td.num', s.total_tool_calls),
            h('td.small.faint', fmtTime(s.computed_at))))) : empty('Sem scores: encerre sessões para gerar.')),
    );
  },
};
