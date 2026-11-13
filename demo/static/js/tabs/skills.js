// "Skills": procedures the agent writes for itself, with quarantine and promotion.

import { h, mount, badge, empty, act, fmtTime, pre, fold, short } from '../lib.js';

const STATUS = { quarantine: ['quarentena', 'amber'], active: ['ativa', 'green'], stale: ['parada', ''], archived: ['arquivada', ''] };

export default {
  id: 'skills',
  title: 'Skills',
  watch: ['learned', 'turn_end'],
  async render(el, ctx) {
    const data = await ctx.api.get(`/api/skills?symbiote=${ctx.symbiote}`);
    const c = data.config;
    const toggle = (field, label, hint) => h('label.switch', { title: hint },
      h('input', {
        type: 'checkbox', checked: c[field],
        onchange: (e) => act(null, async () => {
          await ctx.api.post('/api/skills/config', { symbiote: ctx.symbiote, fields: { [field]: e.target.checked } });
          ctx.refresh('skills');
        }, `${label}: ${e.target.checked ? 'ligado' : 'desligado'}`),
      }), label);
    const setStatus = (name, status) => (e) => act(e.currentTarget, async () => {
      await ctx.api.post(`/api/skills/${encodeURIComponent(name)}/status`, { symbiote: ctx.symbiote, status });
      ctx.refresh('skills');
    });

    const list = data.skills.length ? h('table',
      h('tr', h('th', 'skill'), h('th', 'status'), h('th', 'origem'), h('th.num', 'usos'), h('th.num', 'patches'), h('th', '')),
      data.skills.map((s) => {
        const content = h('div');
        return h('tr',
          h('td', h('b', s.name), h('div.faint.small', short(s.description, 120)),
            h('details', {
              ontoggle: async (e) => {
                if (!e.target.open || content.childElementCount) return;
                const r = await ctx.api.get(`/api/skills/${encodeURIComponent(s.name)}?symbiote=${ctx.symbiote}`);
                mount(content, pre(r.content));
              },
            }, h('summary', 'SKILL.md'), content)),
          h('td', badge(...(STATUS[s.status] || [s.status]))),
          h('td', s.agent_created ? badge('criada pelo agente', 'purple') : badge('humana')),
          h('td.num', s.use_count), h('td.num', s.patch_count),
          h('td', h('div.row',
            s.status !== 'active' ? h('button.btn.sm', { onclick: setStatus(s.name, 'active') }, 'promover') : '',
            s.status !== 'archived' ? h('button.btn.sm.ghost', { onclick: setStatus(s.name, 'archived') }, 'arquivar') : '')));
      }))
      : empty('Nenhuma skill ainda. Ligue o review, faça uma tarefa de vários passos (ex.: “monte o resumo financeiro do mês e salve em financas/resumo.md”) e encerre a sessão.');

    const reviews = data.reviews.length ? h('div.stack', data.reviews.map((r) => h('div.small',
      badge(r.trigger), ' ', r.ok ? badge('ok', 'green') : badge('erro', 'red'),
      ` ${fmtTime(r.created_at)} · ${r.applied} aplicada(s), ${r.skipped} ignorada(s)`,
      r.error ? h('span.error', ` · ${short(r.error, 120)}`) : '',
      (r.ops || []).length ? fold('operações', pre(r.ops)) : ''))) : empty('Nenhum review rodou ainda.');

    mount(el,
      h('p.tab-intro', 'Skills são procedimentos em ', h('code', 'SKILL.md'), ' que o symbiote escreve para si mesmo. Com o ', h('b', 'review'),
        ' ligado, o modelo auxiliar relê a conversa (a cada N iterações de tool e ao encerrar a sessão) e cria ou corrige skills. ',
        'O que o agente cria nasce em ', h('b', 'quarentena'), ' e só vira ativa quando é promovida — por você ou por uso repetido. ',
        'Com a ', h('b', 'injeção'), ' ligada, as skills ativas entram no contexto e o modelo as consulta com ', h('code', 'skill_view'), '.'),
      h('div.card',
        h('div.row', toggle('skill_review_enabled', 'review em segundo plano', 'Requer o modelo auxiliar'),
          toggle('skill_injection_enabled', 'injetar skills no contexto', ''),
          h('span.faint.small', `nudge a cada ${c.skill_nudge_interval} iterações · promove após ${c.skill_auto_promote_threshold} usos · até ${c.max_active_skills} ativas`)),
        h('p.faint.small', { style: { marginTop: '6px' } }, `Pasta: ${data.root}`)),
      h('div.card', h('h3', 'Biblioteca'), list),
      h('div.card', h('h3', 'Reviews (skill_review_audit)'), reviews),
    );
  },
};
