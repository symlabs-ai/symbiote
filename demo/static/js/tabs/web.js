// "Web": extraction and browser automation, delegation to the researcher,
// and the network guards.

import { h, mount, badge, empty } from '../lib.js';

const PROMPTS = [
  ['sol', 'Delegação: Sol pede ao Pesquisador', 'Peça ao Pesquisador um resumo de https://pt.wikipedia.org/wiki/Curitiba com 5 fatos úteis para quem vai se mudar, e salve em notas/curitiba.md.'],
  ['researcher', 'Ler uma página (web_extract)', 'Leia https://www.python.org/downloads/ e diga qual é a versão mais recente do Python.'],
  ['researcher', 'Navegar e capturar a tela', 'Abra https://example.com no navegador, tire um screenshot e descreva a página.'],
  ['researcher', 'Domínio bloqueado pela política', 'Abra https://promo.ads.example/oferta no navegador.'],
];

export default {
  id: 'web',
  title: 'Web',
  watch: ['turn_end'],
  async render(el, ctx) {
    const data = await ctx.api.get('/api/web');
    mount(el,
      h('p.tab-intro', h('code', 'symbiote.browser'), ' é um subpacote opcional: ', h('code', 'register(kernel, …)'),
        ' adiciona ', h('b', 'web_extract'), ' (leitura local com forge_scraper, sem custo), as tools ', h('b', 'browser_*'),
        ' (Chromium local via Playwright) e, com credenciais do SymGateway, ', h('b', 'web_search'), '. ',
        'Aqui elas pertencem ao ', h('b', 'Pesquisador'), ', que só enxerga tools com as tags web/browser/utilidades; o Sol delega para ele com ',
        h('code', 'spawn'), '.'),
      h('div.stats',
        h('div.stat', h('span.v', data.tools.length), h('span.l', 'tools web registradas')),
        h('div.stat', h('span.v', data.web_search ? 'sim' : 'não'), h('span.l', 'web_search (SymGateway)'))),
      data.error ? h('div.card', h('h3.error', 'Tools web indisponíveis'), h('p', data.error),
        h('p.dim', 'Rode ', h('code', 'bash demo/setup.sh'), ' para instalar os extras e o Chromium.')) : '',
      h('div.card',
        h('h3', 'Experimente'),
        h('div.stack', PROMPTS.map(([who, label, prompt]) => h('div.row',
          h('button.btn.sm', { onclick: () => ctx.sendPrompt(prompt, who) }, 'enviar'),
          badge(who === 'sol' ? 'Sol' : 'Pesquisador', who === 'sol' ? 'green' : 'blue'),
          h('b', label), h('span.dim.small', prompt))))),
      h('div.card',
        h('h3', 'Guardas de rede'),
        h('p.dim', 'Toda URL passa pela proteção contra SSRF: IPs privados, loopback, link-local (metadados de nuvem), IPv6 mapeado e NAT64 são recusados, ',
          'e a conexão vai para o IP que foi validado (sem DNS rebinding). A política de domínios do demo bloqueia ', h('code', '*.ads.example'),
          ' e ', h('code', '*.doubleclick.net'), '. Teste os bloqueios na aba Sistema.')),
      h('div.card',
        h('h3', 'Capturas de tela'),
        data.screenshots.length ? h('div.gallery', data.screenshots.map((src) => h('a', { href: src, target: '_blank', rel: 'noopener' }, h('img', { src, alt: 'captura do browser' }))))
          : empty('Nenhuma captura ainda.')),
      h('p.faint.small', 'Tools: ', data.tools.map((t) => badge(t))),
    );
  },
};
