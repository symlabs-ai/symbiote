/**
 * Symbiote Chat — Shadow DOM HTML template
 *
 * Placeholders (replaced at render time):
 *   {{name}}, {{avatar}}, {{placeholder}}, {{greeting}}, {{greetingSub}}, {{fabLabel}}
 */

/* Inline SVG icons (replaces Jinja icon macros) */
const ICON_SEND = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>`;
const ICON_X = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`;
const ICON_TRASH = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>`;

function buildTemplate(opts) {
  const { name, avatarHtml, placeholder, greeting, greetingSub, fabLabel } = opts;

  return `
    <!-- FAB -->
    <button class="symbiote-fab" part="fab" title="${name}">
      ${avatarHtml.fab}
      <span class="symbiote-fab-label">${fabLabel}</span>
    </button>

    <!-- Panel -->
    <div class="symbiote-panel" part="panel">
      <div class="symbiote-header" part="header">
        <div class="symbiote-title">
          ${avatarHtml.header}
          <h3>${name}</h3>
        </div>
        <div class="symbiote-header-actions">
          <button type="button" class="symbiote-btn-icon" data-action="clear" title="Clear">
            ${ICON_TRASH}
          </button>
          <button type="button" class="symbiote-btn-icon" data-action="close" title="Close">
            ${ICON_X}
          </button>
        </div>
      </div>

      <div class="symbiote-messages" part="messages">
        <div class="symbiote-empty" data-ref="empty">
          ${avatarHtml.empty}
          <p>${greeting}</p>
          <p class="symbiote-empty-sub">${greetingSub}</p>
          <div class="symbiote-suggestions" data-ref="initialSuggestions"></div>
        </div>
      </div>

      <div class="symbiote-input" part="input">
        <textarea data-ref="input" placeholder="${placeholder}" rows="2"></textarea>
        <button type="button" class="symbiote-send" data-ref="sendBtn" title="Send">
          ${ICON_SEND}
        </button>
      </div>
    </div>

    <!-- Overlay -->
    <div class="symbiote-overlay" data-action="close"></div>
  `;
}

/**
 * Build avatar HTML for a given size class.
 * Returns img tag if URL provided, otherwise initials.
 */
function buildAvatarHtml(name, avatarUrl) {
  const initials = (name || 'A').charAt(0).toUpperCase();

  if (avatarUrl) {
    return {
      fab: `<img src="${avatarUrl}" alt="${name}" class="symbiote-avatar-fab">`,
      header: `<img src="${avatarUrl}" alt="${name}" class="symbiote-avatar-header">`,
      empty: `<img src="${avatarUrl}" alt="${name}" class="symbiote-avatar-empty">`,
    };
  }

  return {
    fab: `<span class="symbiote-avatar-initials fab">${initials}</span>`,
    header: `<span class="symbiote-avatar-initials header">${initials}</span>`,
    empty: `<span class="symbiote-avatar-initials empty">${initials}</span>`,
  };
}
