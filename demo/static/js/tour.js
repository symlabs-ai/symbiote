// "Roteiro": a guided path through every feature, for presenting.

import { h, mount, act, toast } from './lib.js';

const KEY = 'demo-tour-done';

const post = (ctx, path, body) => ctx.api.post(path, { symbiote: 'sol', ...body });
const setConfig = (ctx, fields) => post(ctx, '/api/config', { fields });

const STEPS = [
  {
    title: 'Conheça o Sol',
    why: 'Um symbiote tem identidade (nome, papel, persona) e um conjunto de tools. Cada mensagem passa pelo ContextAssembler antes de chegar ao modelo.',
    actions: [{ label: 'Enviar no chat', prompt: 'Oi! Em poucas linhas: o que você consegue fazer por mim?' }],
    look: 'Ao vivo: “contexto montado” e a chamada ao modelo. Depois, Contexto: persona, tools e as mensagens exatas enviadas.',
    tab: 'live',
  },
  {
    title: 'Ensinar preferências e encerrar',
    why: 'O aprendizado acontece ao encerrar a sessão: score, reflexão sobre o que você disse e memórias novas.',
    actions: [
      { label: '1. Enviar no chat', prompt: 'Lembre disso: eu prefiro respostas em tópicos curtos. Nunca marque compromissos antes das 9h. Decidi que a reserva de emergência é a prioridade deste ano.' },
      { label: '2. Encerrar sessão e aprender', close: true },
    ],
    look: 'Aprendizado: memórias novas do tipo preference, constraint e decision, e o score da sessão.',
    tab: 'learning',
  },
  {
    title: 'Ele lembra na sessão seguinte',
    why: 'Nova sessão, sem histórico. As memórias voltam pelo recall: termos em comum, importância e recência.',
    actions: [{ label: 'Enviar no chat', prompt: 'Como você deve formatar as respostas para mim, e que horários devo evitar?' }],
    look: 'Contexto: “Memórias injetadas”. Memória: teste o recall com outras frases e veja que o Pesquisador não enxerga nada disso.',
    tab: 'context',
  },
  {
    title: 'Descobrir tools a partir de um OpenAPI',
    why: 'Nenhuma linha de código por tool: o kernel lê /pa/openapi.json, cria tools pendentes e só carrega o que for aprovado.',
    actions: [{
      label: 'Descobrir, aprovar tudo e carregar',
      run: async (ctx) => {
        const r = await post(ctx, '/api/discovery/run');
        for (const id of r.found) await post(ctx, '/api/discovery/status', { tool_id: id, status: 'approved' });
        const l = await post(ctx, '/api/discovery/load');
        toast(`${l.loaded.length} tools carregadas e autorizadas para o Sol.`);
      },
    }],
    look: 'Tools: a seção de descoberta e a matriz (origem “descoberta”, risco vindo de x-risk-level).',
    tab: 'tools',
  },
  {
    title: 'Uma tarefa de vários passos',
    why: 'No modo brief o kernel faz um loop: chama o modelo, executa tools (em paralelo quando dá), devolve os resultados e repete.',
    actions: [{ label: 'Enviar no chat', prompt: 'O que tenho para fazer esta semana? Junte tarefas e agenda e me diga o que é mais urgente.' }],
    look: 'Ao vivo: as iterações, cada tool com parâmetros e resultado, e o motivo de parada.',
    tab: 'live',
  },
  {
    title: 'Aprovação humana',
    why: 'Tools de risco alto pausam o loop. Negue a primeira vez e veja o modelo lidar com a recusa; depois permita.',
    actions: [
      { label: '1. Pedir para apagar', prompt: 'Apague a tarefa “Organizar fotos da viagem”.' },
      { label: '2. Confirmar', prompt: 'Pode apagar sim, eu confirmo.' },
    ],
    look: 'O modal de aprovação; Ao vivo (✋); Tools → dados da API pessoal e audit log.',
    tab: 'live',
  },
  {
    title: 'Deny-by-default',
    why: 'O Sol vê as tools de browser (visibilidade é por tag e ele não tem tags), mas não está autorizado a usá-las. O pedido manda ele tentar antes de delegar.',
    actions: [{ label: 'Enviar no chat', prompt: 'Tente você mesmo abrir https://example.com com a tool browser_navigate e me diga o título. Se ela for bloqueada, peça ao Pesquisador.' }],
    look: 'Ao vivo: browser_navigate “bloqueada pela política” e, em seguida, o spawn do Pesquisador. Tools → audit log.',
    tab: 'live',
  },
  {
    title: 'Carga de tools em índice e memória sob demanda',
    why: 'Com muitas tools, “index” manda só um catálogo e o modelo busca o schema que precisar. “on_demand” tira memórias do prompt e dá ferramentas de busca.',
    actions: [
      { label: '1. Ligar index + on_demand', run: (ctx) => setConfig(ctx, { tool_loading: 'index', context_mode: 'on_demand' }).then(() => toast('index + on_demand')) },
      { label: '2. Enviar no chat', prompt: 'Quanto gastei com mercado este mês? E qual é a minha prioridade financeira deste ano?' },
      { label: '3. Voltar para full + packed', run: (ctx) => setConfig(ctx, { tool_loading: 'full', context_mode: 'packed' }).then(() => toast('full + packed')) },
    ],
    look: 'Contexto: tools “(índice)”; Ao vivo: get_tool_schema e search_memories sendo chamadas.',
    tab: 'context',
  },
  {
    title: 'Long-run: planejador, blocos e avaliador',
    why: 'Para pedidos grandes: um planejador quebra em blocos, cada bloco roda um loop e um avaliador dá nota e pede retrabalho. O avaliador recebe a lista das tools que o bloco executou, e um retrabalho é avisado do que já foi feito.',
    actions: [
      {
        label: '1. Ligar long_run com avaliador',
        run: (ctx) => setConfig(ctx, {
          // compaction: each block receives the previous blocks' output (the
          // list from block 1 is what block 2 turns into tasks).
          tool_mode: 'long_run', max_blocks: 3, context_strategy: 'compaction',
          evaluator_prompt: 'Você é um avaliador (evaluator) rigoroso. Confira se o bloco cumpriu o critério de sucesso. Responda SOMENTE com JSON: {"passed": true ou false, "overall_score": 0.0 a 1.0, "feedback": "o que falta", "blocking_issues": []}',
        }).then(() => toast('long_run ligado')),
      },
      { label: '2. Enviar no chat', prompt: 'Organize a mudança para Curitiba: levante as pendências, crie uma tarefa para cada uma e salve um resumo em notas/mudanca.md.' },
      { label: '3. Voltar para brief', run: (ctx) => setConfig(ctx, { tool_mode: 'brief', evaluator_prompt: null, context_strategy: 'hybrid' }).then(() => toast('brief')) },
    ],
    look: 'Execução: plano e blocos com a nota do avaliador. Ao vivo: “planejador” e “avaliador”.',
    tab: 'execution',
  },
  {
    title: 'Delegação e web',
    why: 'O Sol chama outro symbiote com spawn. O Pesquisador tem as tools web e só enxerga as tags web/browser/utilidades.',
    actions: [{ label: 'Enviar no chat', prompt: 'Peça ao Pesquisador 3 fatos sobre o clima de Curitiba a partir de https://pt.wikipedia.org/wiki/Curitiba.' }],
    look: 'Ao vivo: spawn e, dentro dele, as tools do Pesquisador. Web: capturas e outros testes.',
    tab: 'live',
  },
  {
    title: 'Falha controlada',
    why: 'A cotação está fora do ar de propósito. O circuit breaker para o loop, o turno ainda responde, e o encerramento grava uma memória de falha e um score baixo.',
    actions: [
      { label: '1. Enviar no chat', prompt: 'Quanto estão o dólar, o euro e a libra agora? Use só a tool consultar_cotacao, uma moeda por vez, e tente de novo se falhar. Não pesquise na web nem delegue.' },
      { label: '2. Encerrar sessão e aprender', close: true },
    ],
    look: 'Ao vivo: “circuit breaker”. Aprendizado: score baixo e uma memória procedural de falha.',
    tab: 'learning',
  },
  {
    title: 'Skills que o agente escreve',
    why: 'Com o review ligado, o modelo auxiliar transforma o que funcionou num SKILL.md, que nasce em quarentena.',
    actions: [
      { label: '1. Ligar review e injeção', run: (ctx) => post(ctx, '/api/skills/config', { fields: { skill_review_enabled: true, skill_injection_enabled: true } }).then(() => toast('review de skills ligado')) },
      { label: '2. Enviar no chat', prompt: 'Monte o resumo financeiro do mês por categoria, compare com o orçamento em financas/orcamento-2026.csv e salve em financas/resumo-mes.md.' },
      { label: '3. Encerrar sessão e aprender', close: true },
    ],
    look: 'Skills: a skill criada (pode levar alguns segundos, roda em segundo plano) e a trilha do review.',
    tab: 'skills',
  },
  {
    title: 'Dream',
    why: 'Fora das sessões, o dream poda memórias velhas, une quase-duplicatas e, no modo full, generaliza e minera padrões.',
    actions: [{
      label: 'Sonhar agora (modo full)',
      run: async (ctx) => {
        await post(ctx, '/api/learning/config', { dream_mode: 'full' });
        await post(ctx, '/api/dream', { dry_run: false });
        toast('Dream concluído');
      },
    }],
    look: 'Aprendizado: relatório por fase (propostas × aplicadas). O demo já nasce com memórias para isso: prune desativa 2 paradas há meses (o Wi-Fi de Ubatuba, a revista cancelada), reconcile junta a preferência de lembretes de aniversário que está duplicada, e generalize transforma os 3 procedimentos de reserva de viagem numa regra só. Mine e evaluate usam as falhas e os scores das sessões anteriores (o passo 11). Na aba Memória dá para ver o antes e o depois. O dream vem desligado até aqui; agora em full, ele roda sozinho a cada 3 sessões encerradas.',
    tab: 'learning',
  },
  {
    title: 'Harness que se ajusta',
    why: 'Com os scores, o tuner recalibra parâmetros e o evolver propõe instruções melhores, com guard rails e rollback automático.',
    actions: [{ label: 'Abrir Harness', tab: 'harness' }],
    look: 'Harness: quantas sessões faltam, o tier do tuner e “Evoluir agora” (mesmo recusando, mostra o guard rail).',
    tab: 'harness',
  },
];

function loadDone() {
  try { return new Set(JSON.parse(localStorage.getItem(KEY) || '[]')); } catch { return new Set(); }
}
function saveDone(done) {
  try { localStorage.setItem(KEY, JSON.stringify([...done])); } catch { /* private mode */ }
}

export function initTour(ctx) {
  const drawer = document.getElementById('tour');
  const done = loadDone();

  function render() {
    mount(drawer,
      h('div.row.between', h('h2', 'Roteiro'), h('button.btn.ghost.sm', { onclick: () => { drawer.hidden = true; } }, 'fechar')),
      h('p.dim.small', 'Um caminho por todos os recursos. Cada passo diz o que fazer e onde olhar. O Sol é o symbiote usado em todos eles.'),
      STEPS.map((s, i) => h(`div.tour-step${done.has(i) ? '.done' : ''}`,
        h('div.n', `${String(i + 1).padStart(2, '0')}${done.has(i) ? ' ✓' : ''}`),
        h('h3', s.title),
        h('p.dim.small', s.why),
        h('div.row', s.actions.map((a) => h('button.btn.sm', {
          onclick: (e) => act(e.currentTarget, async () => {
            if (a.prompt) {
              ctx.sendPrompt(a.prompt, 'sol');
              ctx.openTab(s.tab);
            } else if (a.close) {
              await ctx.closeSession(null);
            } else if (a.run) {
              await a.run(ctx);
              ctx.openTab(s.tab);
            } else if (a.tab) {
              ctx.openTab(a.tab);
            }
            done.add(i);
            saveDone(done);
            render();
          }),
        }, a.label))),
        h('p.look', { style: { marginTop: '6px' } }, '→ ', s.look))),
      h('div.row.end', h('button.btn.ghost.sm', { onclick: () => { done.clear(); saveDone(done); render(); } }, 'recomeçar')));
  }

  document.getElementById('btn-tour').onclick = () => {
    drawer.hidden = !drawer.hidden;
    if (!drawer.hidden) render();
  };
}
