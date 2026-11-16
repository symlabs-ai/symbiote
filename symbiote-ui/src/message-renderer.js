/**
 * Symbiote Chat — Message renderer
 *
 * Renders markdown using marked.js if available, with a basic regex fallback.
 * Looks for marked in: 1) the shadow root's scope, 2) window.marked, 3) _symbioteMarked (vendored).
 *
 * Either way the result goes through symbioteLinkify: addresses written as
 * plain text become links, links open in a new tab, and links with a script
 * scheme are reduced to their text.
 */

// A web address in running text. Group 1 is the character before it (never
// part of a word, an e-mail or a longer URL); group 2 is the address:
//   - with a scheme or "www.":            https://site.com/x, www.site.com
//   - a bare domain followed by a path:   exame.com/inteligencia-artificial/
//   - a bare domain alone, common TLDs:   g1.globo.com
const _SYMBIOTE_ADDRESS = new RegExp(
  '(^|[^\\w@/.:%#=&?~+-])('
  + '(?:https?://|www\\.)[^\\s<>"\']+'
  + '|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,24}/[^\\s<>"\']*'
  + '|(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\\.)+(?:com|org|gov|edu|io|ai|dev|app|br)(?![\\w-]|\\.\\w)'
  + ')',
  'gi'
);
const _SYMBIOTE_UNSAFE_HREF = /^(javascript|data|vbscript):/i;

function symbioteLinkify(html) {
  if (typeof document === 'undefined') return html;
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const root = tpl.content;

  // 1) Plain-text addresses → links. Text already inside a link or code stays.
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const texts = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement;
    if (!parent || !parent.closest('a, code, pre')) texts.push(node);
  }
  for (const node of texts) {
    const text = node.nodeValue;
    const parts = [];
    let last = 0;
    _SYMBIOTE_ADDRESS.lastIndex = 0;
    for (let m = _SYMBIOTE_ADDRESS.exec(text); m; m = _SYMBIOTE_ADDRESS.exec(text)) {
      let address = m[2];
      // Punctuation that closes the sentence is not part of the address.
      address = address.replace(/[.,;:!?]+$/, '');
      if (address.endsWith(')') && !address.includes('(')) address = address.slice(0, -1);
      const start = m.index + m[1].length;
      const link = document.createElement('a');
      link.href = /^https?:\/\//i.test(address) ? address : 'https://' + address;
      link.textContent = address;
      parts.push(text.slice(last, start), link);
      last = start + address.length;
      _SYMBIOTE_ADDRESS.lastIndex = last;
    }
    if (parts.length) {
      parts.push(text.slice(last));
      node.replaceWith(...parts);
    }
  }

  // 2) Every link: no script schemes, and never navigate the chat away.
  for (const link of root.querySelectorAll('a')) {
    const href = (link.getAttribute('href') || '').replace(/[\u0000-\u0020]/g, '');
    if (_SYMBIOTE_UNSAFE_HREF.test(href)) {
      link.replaceWith(...link.childNodes);
    } else if (/^https?:/i.test(href)) {
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
    }
  }
  return tpl.innerHTML;
}

function symbioteFormatMarkdown(text) {
  // Try vendored marked first, then global
  const marked = (typeof _symbioteMarked !== 'undefined' && _symbioteMarked)
    || (typeof window !== 'undefined' && window.marked);

  if (marked && typeof marked.parse === 'function') {
    return symbioteLinkify(marked.parse(text, { breaks: true }));
  }

  // Basic fallback: escape HTML, then apply simple formatting
  const div = document.createElement('div');
  div.textContent = text;
  let s = div.innerHTML;
  s = s.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\n/g, '<br>');
  return symbioteLinkify(s);
}
