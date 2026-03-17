/**
 * Symbiote Chat — Message renderer
 *
 * Renders markdown using marked.js if available, with a basic regex fallback.
 * Looks for marked in: 1) the shadow root's scope, 2) window.marked, 3) _symbioteMarked (vendored).
 */

function symbioteFormatMarkdown(text) {
  // Try vendored marked first, then global
  const marked = (typeof _symbioteMarked !== 'undefined' && _symbioteMarked)
    || (typeof window !== 'undefined' && window.marked);

  if (marked && typeof marked.parse === 'function') {
    return marked.parse(text, { breaks: true });
  }

  // Basic fallback: escape HTML, then apply simple formatting
  const div = document.createElement('div');
  div.textContent = text;
  let s = div.innerHTML;
  s = s.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  s = s.replace(/\n/g, '<br>');
  return s;
}
