// "Ao vivo": every turn as a timeline — context assembled, each model call,
// each tool (with policy blocks and approvals), iterations and the stop
// reason — plus what runs outside the turn (reflection, dream, reviews).

import { h, fmtMs, short, fmtTime, pre } from '../lib.js';

const root = h('div');
const turnsBox = h('div');
const bgBox = h('div.bg-feed');
const turns = new Map();   // turn_id → {el, steps, streams}
const calls = new Map();   // call_id → {el, stream}
let built = false;

function build() {
  if (built) return;
  built = true;
  root.append(
    h('p.tab-intro', 'Cada turno do chat aparece aqui enquanto acontece: o ',
      h('b', 'contexto'), ' que o kernel montou, cada ', h('b', 'chamada ao modelo'),
      ' (com o texto bruto que ele devolveu), cada ', h('b', 'tool'),
      ' executada ou bloqueada, os pedidos de ', h('b', 'aprovação'), ' e o motivo de parada do loop. ',
      'O chat só mostra a resposta final, validada pelo kernel.'),
    h('div.grid2', { style: { gridTemplateColumns: 'minmax(0, 2fr) minmax(260px, 1fr)' } },
      h('div', h('h4', 'Turnos'), turnsBox),
      h('div', h('h4', 'Fora do turno'), h('div.card.tight', bgBox),
        h('p.faint.small', { style: { marginTop: '8px' } },
          'Chamadas do modelo auxiliar (reflexão, dream, review de skills, tags semânticas) e ações da página.'))),
  );
  turnsBox.append(h('div.empty#live-empty', 'Mande uma mensagem no chat para ver o turno por dentro.'));
  bgBox.append(h('div.faint.small#bg-empty', 'Nada ainda.'));
}

function step(kind, icon, body, meta = '') {
  return h(`div.step.${kind}`, h('span.ico', icon), h('div.body', body), h('span.meta', meta));
}

function turnFor(ev) {
  if (!ev.turn_id) return null;
  let t = turns.get(ev.turn_id);
  if (!t) {
    root.querySelector('#live-empty')?.remove();
    const steps = h('div.steps');
    const head = h('div.turn-head', h('span.badge.blue', ev.symbiote_id ? 'turno' : 'turno'),
      h('span.msg', ''), h('span.meta.faint.small', fmtTime(ev.ts)));
    const foot = h('div.turn-foot', h('span.spinner'), ' em andamento…');
    const el = h('div.turn', head, steps, foot);
    turnsBox.prepend(el);
    t = { el, steps, head, foot };
    turns.set(ev.turn_id, t);
    while (turns.size > 25) {
      const oldest = turns.keys().next().value;
      turns.get(oldest).el.remove();
      turns.delete(oldest);
    }
  }
  return t;
}

function addBg(node) {
  bgBox.querySelector('#bg-empty')?.remove();
  bgBox.prepend(node);
  while (bgBox.children.length > 60) bgBox.lastChild.remove();
}

function llmBody(ev) {
  const asked = (ev.tool_calls || []).map((c) => c.tool);
  const text = (ev.text || '').trim();
  const summary = ev.error
    ? h('span.error', `erro: ${short(ev.error, 160)}`)
    : asked.length
      ? h('span', 'pediu ', h('b', asked.join(', ')), text ? ` · “${short(text, 80)}”` : '')
      : h('span', '“', short(text, 160) || '(vazio)', '”');
  const usage = ev.usage && ev.usage.total_tokens ? ` · ${ev.usage.total_tokens} tokens` : '';
  return { summary, meta: `${fmtMs(ev.elapsed_ms)}${usage}` };
}

function onEvent(ev) {
  build();
  const t = turnFor(ev);
  switch (ev.type) {
    case 'turn_start': {
      t.head.querySelector('.msg').textContent = ev.message;
      if (ev.history_messages) {
        t.steps.append(step('ctx', '↺', h('span.dim', `histórico injetado pelo host: ${ev.history_messages} linha(s) da conversa`)));
      }
      break;
    }
    case 'context': {
      const body = h('span', 'contexto montado: ',
        h('b', `${ev.memories} memória(s)`), ', ', h('b', `${ev.knowledge} knowledge`), ', ',
        `${ev.tools} tools visíveis · modo ${ev.tool_mode}/${ev.tool_loading}/${ev.context_mode}`);
      const node = step('ctx', '◇', body, `~${ev.tokens}/${ev.budget} tok`);
      (t ? t.steps : bgBox).append(node);
      break;
    }
    case 'progress': {
      if (!t) break;
      if (ev.event === 'iteration_start' && ev.index > 1) {
        t.steps.append(step('iter', '', `iteração ${ev.index} de ${ev.total}`));
      }
      if (ev.event === 'block_start') t.steps.append(step('iter', '', `bloco ${ev.index} de ${ev.total}`));
      break;
    }
    case 'plan': {
      if (!t) break;
      t.steps.append(step('ctx', '▤', h('span', 'plano do long-run: ',
        h('b', (ev.blocks || []).map((b) => b.name).join(' → ')))));
      break;
    }
    case 'block': {
      if (!t) break;
      const ok = ev.success;
      t.steps.append(step(ok ? 'tool' : 'tool.fail', ok ? '■' : '□',
        h('span', `bloco “${ev.block_name}” ${ok ? 'concluído' : 'falhou'}`,
          ev.evaluator_score != null ? ` · avaliador ${Number(ev.evaluator_score).toFixed(2)}` : '',
          ev.evaluator_feedback ? h('div.faint.small', short(ev.evaluator_feedback, 220)) : ''),
        fmtMs(ev.elapsed_ms)));
      break;
    }
    case 'llm_start': {
      const stream = h('div.live-stream');
      const body = h('div', h('span', h('span.spinner'), ` ${ev.purpose} · ${ev.model}`,
        h('span.faint', ` · ${ev.n_messages} msgs, ${ev.n_tools} tools`)), stream);
      const node = step('llm', '◆', body, '');
      calls.set(ev.call_id, { node, body, stream });
      if (t) t.steps.append(node);
      else addBg(node);
      break;
    }
    case 'llm_delta': {
      const c = calls.get(ev.call_id);
      if (c) c.stream.textContent = short(c.stream.textContent + ev.text, 600);
      break;
    }
    case 'llm_end': {
      const c = calls.get(ev.call_id);
      if (!c) break;
      const { summary, meta } = llmBody(ev);
      c.body.replaceChildren(
        h('div', h('span.faint', `${ev.purpose}: `), summary),
        ev.text && ev.text.length > 160 ? h('details', h('summary', 'texto completo'), pre(ev.text)) : '');
      c.node.querySelector('.meta').textContent = meta;
      calls.delete(ev.call_id);
      break;
    }
    case 'tool_start': {
      const node = step('tool', '⚙', h('div',
        h('span', h('span.spinner'), ' ', h('b', ev.tool_id), h('span.faint', ` · risco ${ev.risk}`)),
        h('details', h('summary', 'parâmetros'), pre(ev.params))), '');
      node.dataset.tool = ev.tool_id;
      node.dataset.open = '1';
      (t ? t.steps : bgBox).append(node);
      break;
    }
    case 'tool_end': {
      const box = t ? t.steps : bgBox;
      const node = [...box.querySelectorAll(`.step[data-tool="${CSS.escape(ev.tool_id)}"][data-open="1"]`)][0];
      const status = ev.blocked ? h('span.badge.red', 'bloqueada pela política')
        : ev.success ? h('span.badge.green', 'ok') : h('span.badge.red', 'erro');
      const body = h('div',
        h('span', h('b', ev.tool_id), ' ', status),
        ev.error ? h('div.error.small', short(ev.error, 300)) : '',
        ev.screenshot ? h('img', { src: ev.screenshot, style: { maxWidth: '260px', marginTop: '6px', borderRadius: '6px' } }) : '',
        ev.output ? h('details', h('summary', 'resultado'), pre(ev.output)) : '');
      if (node) {
        const params = node.querySelector('details');
        node.classList.toggle('fail', !ev.success);
        node.classList.toggle('blocked', !!ev.blocked);
        node.querySelector('.body').replaceChildren(body);
        if (params) body.append(params);
        node.querySelector('.meta').textContent = fmtMs(ev.elapsed_ms);
        node.dataset.open = '0';
      } else {
        box.append(step(ev.success ? 'tool' : 'tool.fail', '⚙', body, fmtMs(ev.elapsed_ms)));
      }
      break;
    }
    case 'approval_request':
      (t ? t.steps : bgBox).append(step('approval', '✋', h('span',
        'aprovação pedida para ', h('b', ev.tool_id), ' (risco alto) — o loop está pausado')));
      break;
    case 'approval_result':
      (t ? t.steps : bgBox).append(step('approval', '✋', h('span',
        ev.timed_out ? 'sem resposta: negado' : ev.approved ? 'aprovado pelo humano' : 'negado pelo humano',
        ' · ', h('b', ev.tool_id))));
      break;
    case 'turn_end': {
      if (!t) break;
      if (ev.ok) {
        const stop = { end_turn: 'terminou normalmente', max_iterations: 'limite de iterações',
          stagnation: 'estagnação (repetiu a mesma ação)', circuit_breaker: 'circuit breaker (tool falhando)',
          timeout: 'timeout do loop', block_failure: 'bloco do long-run falhou' }[ev.stop_reason] || ev.stop_reason;
        t.foot.replaceChildren(
          h('span', 'parada: ', h('b', stop)), ` · ${ev.iterations} iteração(ões) · ${ev.tool_calls} tool call(s)`,
          ev.elapsed_ms ? ` · ${fmtMs(ev.elapsed_ms)}` : '');
      } else {
        t.foot.replaceChildren(h('span.error', `turno falhou: ${short(ev.error, 300)}`));
      }
      break;
    }
    case 'close_start':
      addBg(step('ctx', '◎', h('span', 'encerrando a sessão: score, memória de falha, reflexão, handoff, skills, dream…')));
      break;
    case 'learned':
      addBg(step('tool', '★', h('span', h('b', `${ev.learned} memória(s) nova(s)`),
        ev.score ? ` · score ${Number(ev.score.final_score).toFixed(2)} (${ev.score.stop_reason || 'sem tools'})` : '')));
      break;
    case 'dream':
      addBg(step('llm', '☾', h('span', ev.state === 'start' ? `dream ${ev.dry_run ? '(simulação)' : ''} começou`
        : `dream terminou: ${(ev.phases || []).join(', ')}`)));
      break;
    case 'discovery':
      addBg(step('ctx', '⌕', h('span', `descoberta: ${ev.found} operação(ões) na API pessoal`)));
      break;
    case 'tools':
      addBg(step('ctx', '⚙', h('span', ev.action === 'authorize'
        ? `${ev.tool_id} ${ev.authorized ? 'autorizada' : 'revogada'}`
        : ev.action === 'loaded' ? `${(ev.tools || []).length} tool(s) descoberta(s) carregadas no kernel`
          : `tool HTTP ${ev.tool_id} registrada`)));
      break;
    case 'config':
      addBg(step('ctx', '✎', h('span', `config alterada: ${(ev.fields || []).join(', ')}`)));
      break;
    case 'memory':
      addBg(step('ctx', '✎', h('span', ev.action === 'learned' ? 'fato ensinado pela página (learn)' : 'memória desativada')));
      break;
    default:
      break;
  }
}

export default {
  id: 'live',
  title: 'Ao vivo',
  render(el) {
    build();
    if (!el.contains(root)) el.replaceChildren(root);
  },
  onEvent,
};
