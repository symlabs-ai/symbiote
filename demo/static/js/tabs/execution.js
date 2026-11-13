// "Execução": execution modes and loop limits, long-run plan/blocks, the
// persisted traces and the process engine.

import { h, mount, badge, empty, act, fmtTime, fmtMs, pre, fold, short, select } from '../lib.js';

const MODES = {
  instant: 'Uma chamada ao modelo e no máximo uma rodada de tools, sem loop. Para comandos rápidos.',
  brief: 'Loop de tools (padrão): chama o modelo, executa tools, devolve os resultados, repete até ele responder ou o LoopController parar.',
  long_run: 'Projeto: um planejador quebra o pedido em blocos, cada bloco roda como um loop brief e um avaliador opcional dá nota e pede retrabalho.',
  auto: 'Escolhe sozinho: long_run se houver prompt de planejador, instant se o symbiote não tiver tools, senão brief.',
};
const LOADING = {
  full: 'Schemas completos de todas as tools visíveis no prompt.',
  index: 'Catálogo de uma linha por tool + a meta-tool get_tool_schema para buscar o schema sob demanda.',
  semantic: 'O modelo auxiliar escolhe as tags relevantes a cada mensagem antes de montar o contexto.',
};
const DEFAULT_EVALUATOR = 'Você é um avaliador (evaluator) rigoroso. Confira se o bloco cumpriu o critério de sucesso, '
  + 'sem confiar no que o gerador afirma. Responda SOMENTE com JSON: '
  + '{"passed": true ou false, "overall_score": 0.0 a 1.0, "feedback": "o que falta", "blocking_issues": []}';
const STOP = {
  end_turn: ['terminou', 'green'], max_iterations: ['limite de iterações', 'amber'], stagnation: ['estagnação', 'amber'],
  circuit_breaker: ['circuit breaker', 'red'], timeout: ['timeout', 'red'], block_failure: ['bloco falhou', 'red'],
  on_after_tool_result: ['encerrado pelo host', 'blue'],
};

function longRunPanel(ctx) {
  const lr = ctx.state.longrun;
  if (!lr.plan) return empty('Nenhum long-run nesta página ainda. Ponha o modo em long_run e peça algo com várias etapas.');
  const done = new Map(lr.blocks.map((b) => [b.block_name, b]));
  return h('table',
    h('tr', h('th', '#'), h('th', 'bloco'), h('th', 'critério'), h('th', 'status'), h('th', 'avaliador')),
    lr.plan.map((b, i) => {
      const r = done.get(b.name);
      return h('tr',
        h('td.mono', i + 1), h('td', h('b', b.name), h('div.faint.small', short(b.description, 120))),
        h('td.small.dim', short(b.success_criteria, 120)),
        h('td', r ? (r.success ? badge('concluído', 'green') : badge('falhou', 'red')) : badge('pendente')),
        h('td.small', r?.evaluator_score != null ? `${Number(r.evaluator_score).toFixed(2)} · ${short(r.evaluator_feedback, 100)}` : '—'));
    }));
}

export default {
  id: 'execution',
  title: 'Execução',
  watch: ['turn_end', 'config', 'block'],
  async render(el, ctx) {
    const [cfgData, traces] = await Promise.all([
      ctx.api.get(`/api/config?symbiote=${ctx.symbiote}`),
      ctx.api.get(`/api/traces?symbiote=${ctx.symbiote}`),
    ]);
    const c = cfgData.config;
    const form = h('form.stack', {
      onsubmit: async (e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        const num = (k) => Number(f.get(k));
        const fields = {
          tool_mode: f.get('tool_mode'), tool_loading: f.get('tool_loading'), context_mode: f.get('context_mode'),
          max_tool_iterations: num('max_tool_iterations'), tool_call_timeout: num('tool_call_timeout'), loop_timeout: num('loop_timeout'),
          memory_share: num('memory_share'), knowledge_share: num('knowledge_share'),
          prompt_caching: f.get('prompt_caching') === 'on', native_tools: f.get('native_tools') === 'on',
          planner_prompt: f.get('planner_prompt') || null, evaluator_prompt: f.get('use_evaluator') === 'on' ? (f.get('evaluator_prompt') || DEFAULT_EVALUATOR) : null,
          context_strategy: f.get('context_strategy'), max_blocks: num('max_blocks'),
        };
        await act(e.submitter, () => ctx.api.post('/api/config', { symbiote: ctx.symbiote, fields }), 'Configuração salva: vale a partir da próxima mensagem.');
        ctx.refresh('execution');
      },
    },
    h('div.form-grid',
      h('label', 'Modo (tool_mode)', select('tool_mode', ['instant', 'brief', 'long_run', 'auto'], c.tool_mode)),
      h('label', 'Carga de tools', select('tool_loading', ['full', 'index', 'semantic'], c.tool_loading)),
      h('label', 'Memória e knowledge', select('context_mode', [['packed', 'packed (no prompt)'], ['on_demand', 'on_demand (via tools)']], c.context_mode)),
      h('label', 'Máx. iterações', h('input', { name: 'max_tool_iterations', type: 'number', min: 1, max: 50, value: c.max_tool_iterations })),
      h('label', 'Timeout por tool (s)', h('input', { name: 'tool_call_timeout', type: 'number', min: 1, max: 300, value: c.tool_call_timeout })),
      h('label', 'Timeout do loop (s)', h('input', { name: 'loop_timeout', type: 'number', min: 10, max: 3600, value: c.loop_timeout })),
      h('label', 'Fatia de memórias', h('input', { name: 'memory_share', type: 'number', min: 0, max: 1, step: 0.05, value: c.memory_share })),
      h('label', 'Fatia de knowledge', h('input', { name: 'knowledge_share', type: 'number', min: 0, max: 1, step: 0.05, value: c.knowledge_share }))),
    h('div.row',
      h('label.switch', h('input', { type: 'checkbox', name: 'native_tools', checked: cfgData.native_tools }), 'tool calling nativo do provider (senão, blocos ```tool_call``` no texto)'),
      h('label.switch', h('input', { type: 'checkbox', name: 'prompt_caching', checked: c.prompt_caching }), 'prompt caching')),
    fold('Long-run: planejador e avaliador', h('div.stack',
      h('label', 'Prompt do planejador (vazio = padrão do kernel)', h('textarea', { name: 'planner_prompt', rows: 3 }, c.planner_prompt || '')),
      h('label.switch', h('input', { type: 'checkbox', name: 'use_evaluator', checked: !!c.evaluator_prompt }), 'usar avaliador (nota cada bloco e pede retrabalho)'),
      h('label', 'Prompt do avaliador', h('textarea', { name: 'evaluator_prompt', rows: 2 }, c.evaluator_prompt || '')),
      h('div.form-grid',
        h('label', 'Contexto entre blocos', select('context_strategy', ['hybrid', 'compaction', 'reset'], c.context_strategy)),
        h('label', 'Máx. blocos', h('input', { name: 'max_blocks', type: 'number', min: 1, max: 100, value: c.max_blocks })))), c.tool_mode === 'long_run'),
    h('div.row.end', h('button.btn.accent', { type: 'submit' }, 'Salvar')));

    const traceList = traces.length ? h('div.stack', traces.map((t) => {
      const [label, color] = STOP[t.stop_reason] || [t.stop_reason || '—', ''];
      return h('div.card.tight',
        h('div.row', badge(t.tool_mode || 'brief', 'purple'), badge(label, color),
          h('span.small', `${t.total_iterations} iteração(ões) · ${t.total_tool_calls} tool call(s) · ${fmtMs(t.total_elapsed_ms)}`),
          h('span.spacer'), h('span.faint.small', fmtTime(t.created_at))),
        (t.steps || []).length ? fold('passos', h('table',
          h('tr', h('th', 'it.'), h('th', 'tool'), h('th', 'ok'), h('th.num', 'ms'), h('th', 'erro')),
          t.steps.map((s) => h('tr', h('td.mono', s.iteration), h('td', h('b', s.tool_id), h('div.mono.faint.small', short(JSON.stringify(s.params), 80))),
            h('td', s.success ? badge('ok', 'green') : badge('falhou', 'red')), h('td.num', s.elapsed_ms), h('td.small.error', short(s.error, 100)))))) : '');
    })) : empty('Sem traces ainda: só turnos que usaram tools geram trace.');

    const workOut = h('div');
    const work = h('form.row', {
      onsubmit: async (e) => {
        e.preventDefault();
        const task = new FormData(e.currentTarget).get('task');
        const r = await act(e.submitter, () => ctx.api.post('/api/process', { symbiote: ctx.symbiote, task }));
        if (r) mount(workOut, pre(r));
      },
    }, h('input', { name: 'task', value: 'research_task: comparar escolas em Curitiba', style: { flex: 1 } }),
    h('button.btn', { type: 'submit' }, 'work()'));

    mount(el,
      h('p.tab-intro', 'Como o kernel executa um turno. Tudo aqui é configuração por symbiote (', h('code', 'EnvironmentConfig'),
        '), aplicada na próxima mensagem. O ', h('b', 'LoopController'), ' protege o loop: para por estagnação (mesma ação repetida), ',
        h('b', 'circuit breaker'), ' (tool falhando 3 vezes), limite de iterações ou timeout, e mesmo assim devolve uma resposta.'),
      h('div.grid2',
        h('div.card', h('h3', 'Configuração'), form),
        h('div.card', h('h3', 'O que cada opção faz'),
          h('h4', 'Modos'), h('div.stack.small', Object.entries(MODES).map(([k, v]) => h('div', badge(k, k === c.tool_mode ? 'green' : ''), ' ', h('span.dim', v)))),
          h('h4', 'Carga de tools'), h('div.stack.small', Object.entries(LOADING).map(([k, v]) => h('div', badge(k, k === c.tool_loading ? 'green' : ''), ' ', h('span.dim', v)))),
          h('h4', 'Memória e knowledge'), h('p.small.dim', h('b', 'packed'), ': injeta no prompt o que for relevante. ', h('b', 'on_demand'),
            ': não injeta nada e libera as tools search_memories e search_knowledge para o modelo buscar quando precisar.'))),
      h('div.card', { style: { marginTop: '12px' } }, h('h3', 'Long-run desta página'), longRunPanel(ctx)),
      h('div', { style: { marginTop: '12px' } }, h('h4', 'Traces persistidos (execution_traces)'), traceList),
      h('div.card', { style: { marginTop: '12px' } }, h('h3', 'Process engine'),
        h('p.dim.small', h('code', 'capabilities.work("processo: detalhe")'), ' escolhe um runner pela intenção; com o nome de um processo ',
          '(research_task, review_task, artifact_generation…) o ProcessEngine percorre os passos declarados e registra o andamento.'),
        work, workOut),
    );
  },
};
