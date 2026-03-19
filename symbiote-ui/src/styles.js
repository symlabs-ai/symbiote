/**
 * Symbiote Chat — Styles
 *
 * All colors use CSS custom properties so the host app can theme the component
 * by setting variables on <symbiote-chat> or :root.
 */
const SYMBIOTE_STYLES = `
/* ── FAB ── */
.symbiote-fab {
  position: fixed;
  bottom: var(--symbiote-fab-bottom, 1.5rem);
  right: var(--symbiote-fab-right, 1.25rem);
  width: auto;
  height: 44px;
  padding: 0 1rem 0 0.75rem;
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  color: white;
  border: none;
  border-radius: 22px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3);
  z-index: var(--symbiote-fab-z, 999);
  transition: all 0.2s ease;
  font-size: 0.8rem;
  font-weight: 600;
  font-family: inherit;
}

.symbiote-fab:hover {
  background: var(--symbiote-primary-hover, var(--primary-hover, #2563eb));
  transform: translateY(-2px);
  box-shadow: 0 6px 16px rgba(0,0,0,0.4);
}

.symbiote-fab-label {
  letter-spacing: 0.3px;
}

/* ── Avatars ── */
.symbiote-avatar-fab {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  object-fit: cover;
  object-position: top;
}

.symbiote-avatar-header {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  object-fit: cover;
  object-position: top;
}

.symbiote-avatar-empty {
  width: 80px;
  height: 80px;
  border-radius: 50%;
  object-fit: cover;
  object-position: top;
  margin-bottom: 0.5rem;
}

/* Avatar initials fallback */
.symbiote-avatar-initials {
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  color: white;
  font-weight: 700;
  border-radius: 50%;
}

.symbiote-avatar-initials.fab { width: 28px; height: 28px; font-size: 0.7rem; }
.symbiote-avatar-initials.header { width: 28px; height: 28px; font-size: 0.7rem; }
.symbiote-avatar-initials.empty { width: 80px; height: 80px; font-size: 1.8rem; margin-bottom: 0.5rem; }

/* ── Panel ── */
.symbiote-panel {
  position: fixed;
  top: 0;
  right: calc(-1 * var(--symbiote-width, 420px));
  width: var(--symbiote-width, 420px);
  height: 100vh;
  background: var(--symbiote-bg, var(--bg, #18181b));
  border-left: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  z-index: var(--symbiote-panel-z, 1100);
  display: flex;
  flex-direction: column;
  transition: right 0.3s ease;
}

.symbiote-panel.open {
  right: 0;
}

.symbiote-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  z-index: calc(var(--symbiote-panel-z, 1100) - 1);
  display: none;
}

.symbiote-overlay.open {
  display: block;
}

/* ── Header ── */
.symbiote-header {
  padding: 0.875rem 1rem;
  border-bottom: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.symbiote-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.symbiote-title h3 {
  margin: 0;
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: var(--symbiote-text, var(--text, #e4e4e7));
}

.symbiote-header-actions {
  display: flex;
  gap: 0.25rem;
}

.symbiote-btn-icon {
  background: none;
  border: none;
  color: var(--symbiote-text-muted, var(--text-muted, #a1a1aa));
  cursor: pointer;
  padding: 0.3rem;
  border-radius: 4px;
  display: flex;
  align-items: center;
}

.symbiote-btn-icon:hover {
  background: var(--symbiote-bg-secondary, var(--bg-secondary, #27272a));
  color: var(--symbiote-text, var(--text, #e4e4e7));
}

/* ── Messages ── */
.symbiote-messages {
  flex: 1;
  overflow-y: auto;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.symbiote-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: var(--symbiote-text-muted, var(--text-muted, #a1a1aa));
  padding: 2rem;
  height: 100%;
}

.symbiote-empty p { margin: 0.5rem 0 0; }
.symbiote-empty-sub { font-size: 0.8rem; opacity: 0.7; }

.symbiote-suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 1rem;
  justify-content: center;
}

.symbiote-suggestion {
  padding: 0.4rem 0.75rem;
  background: var(--symbiote-bg-secondary, var(--bg-secondary, #27272a));
  border: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  border-radius: 9999px;
  color: var(--symbiote-text-muted, var(--text-muted, #a1a1aa));
  font-size: 0.75rem;
  cursor: pointer;
  transition: all 0.15s ease;
  font-family: inherit;
}

.symbiote-suggestion:hover {
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  border-color: var(--symbiote-primary, var(--primary, #3B82F6));
  color: white;
}

/* ── Message bubbles ── */
.symbiote-msg {
  max-width: 90%;
  padding: 0.7rem 0.9rem;
  border-radius: 0.75rem;
  font-size: 0.85rem;
  line-height: 1.55;
  word-break: break-word;
}

.symbiote-msg.user {
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  color: white;
  align-self: flex-end;
  border-bottom-right-radius: 0.25rem;
}

.symbiote-msg.assistant {
  background: var(--symbiote-bg-secondary, var(--bg-secondary, #27272a));
  color: var(--symbiote-text, var(--text, #e4e4e7));
  align-self: flex-start;
  border-bottom-left-radius: 0.25rem;
}

/* ── Markdown inside assistant messages ── */
.symbiote-msg.assistant strong { color: var(--symbiote-text-primary, var(--text-primary, #fff)); }
.symbiote-msg.assistant code {
  background: rgba(255,255,255,0.1);
  padding: 0.1rem 0.3rem;
  border-radius: 3px;
  font-size: 0.8rem;
}
.symbiote-msg.assistant p { margin: 0 0 0.5em; }
.symbiote-msg.assistant p:last-child { margin-bottom: 0; }
.symbiote-msg.assistant ul, .symbiote-msg.assistant ol {
  margin: 0.3em 0 0.5em;
  padding-left: 1.4em;
}
.symbiote-msg.assistant li { margin-bottom: 0.2em; }
.symbiote-msg.assistant h1, .symbiote-msg.assistant h2, .symbiote-msg.assistant h3,
.symbiote-msg.assistant h4, .symbiote-msg.assistant h5, .symbiote-msg.assistant h6 {
  margin: 0.6em 0 0.3em;
  font-size: 0.9rem;
  color: var(--symbiote-text-primary, var(--text-primary, #fff));
}
.symbiote-msg.assistant h1 { font-size: 1rem; }
.symbiote-msg.assistant h2 { font-size: 0.95rem; }
.symbiote-msg.assistant blockquote {
  margin: 0.4em 0;
  padding: 0.3em 0.7em;
  border-left: 3px solid var(--symbiote-primary, var(--primary, #3B82F6));
  background: rgba(255,255,255,0.04);
  border-radius: 0 4px 4px 0;
}
.symbiote-msg.assistant pre {
  background: rgba(0,0,0,0.3);
  padding: 0.6em 0.75em;
  border-radius: 6px;
  overflow-x: auto;
  margin: 0.4em 0;
}
.symbiote-msg.assistant pre code {
  background: none;
  padding: 0;
  font-size: 0.78rem;
}
.symbiote-msg.assistant hr {
  border: none;
  border-top: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  margin: 0.5rem 0;
}
.symbiote-msg.assistant table {
  border-collapse: collapse;
  margin: 0.4em 0;
  font-size: 0.8rem;
  width: 100%;
}
.symbiote-msg.assistant th, .symbiote-msg.assistant td {
  border: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  padding: 0.3em 0.5em;
  text-align: left;
}
.symbiote-msg.assistant th {
  background: rgba(255,255,255,0.06);
  font-weight: 600;
}
.symbiote-msg.assistant a {
  color: var(--symbiote-primary, var(--primary, #3B82F6));
  text-decoration: underline;
}

/* ── Tool result badges ── */
.symbiote-tool-badges {
  display: flex;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin-bottom: 0.4rem;
  align-self: flex-start;
  max-width: 90%;
}

.symbiote-tool-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  padding: 0.2rem 0.5rem;
  border-radius: 9999px;
  font-size: 0.65rem;
  font-family: inherit;
  cursor: default;
  border: none;
  line-height: 1.3;
}

.symbiote-tool-badge.pending {
  background: var(--symbiote-badge-pending-bg, rgba(59, 130, 246, 0.15));
  color: var(--symbiote-badge-pending-color, #93c5fd);
  border: 1px solid var(--symbiote-badge-pending-border, rgba(59, 130, 246, 0.3));
  animation: symbiote-badge-pulse 1.5s ease-in-out infinite;
}

.symbiote-tool-badge.success {
  background: var(--symbiote-badge-success-bg, rgba(34, 197, 94, 0.15));
  color: var(--symbiote-badge-success-color, #4ade80);
  border: 1px solid var(--symbiote-badge-success-border, rgba(34, 197, 94, 0.3));
}

.symbiote-tool-badge.error {
  background: var(--symbiote-badge-error-bg, rgba(239, 68, 68, 0.15));
  color: var(--symbiote-badge-error-color, #f87171);
  border: 1px solid var(--symbiote-badge-error-border, rgba(239, 68, 68, 0.3));
}

.symbiote-tool-badge .badge-dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  flex-shrink: 0;
}

.symbiote-tool-badge.pending .badge-dot { background: var(--symbiote-badge-pending-color, #93c5fd); }
.symbiote-tool-badge.success .badge-dot { background: var(--symbiote-badge-success-color, #4ade80); }
.symbiote-tool-badge.error .badge-dot { background: var(--symbiote-badge-error-color, #f87171); }

@keyframes symbiote-badge-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* ── Follow-up suggestion badges ── */
.symbiote-followups {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-self: flex-start;
  max-width: 90%;
}

.symbiote-followup {
  padding: 0.35rem 0.7rem;
  background: transparent;
  border: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  border-radius: 9999px;
  color: var(--symbiote-text-muted, var(--text-muted, #a1a1aa));
  font-size: 0.73rem;
  cursor: pointer;
  transition: all 0.15s ease;
  font-family: inherit;
  white-space: nowrap;
}

.symbiote-followup:hover {
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  border-color: var(--symbiote-primary, var(--primary, #3B82F6));
  color: white;
}

/* ── Input ── */
.symbiote-input {
  padding: 0.875rem 1rem;
  border-top: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  display: flex;
  gap: 0.5rem;
  align-items: flex-end;
}

.symbiote-input textarea {
  flex: 1;
  resize: none;
  min-height: 48px;
  max-height: 120px;
  background: var(--symbiote-bg-secondary, var(--bg-secondary, #27272a));
  border: 1px solid var(--symbiote-border, var(--border, #3f3f46));
  border-radius: 0.5rem;
  padding: 0.6rem 0.75rem;
  color: var(--symbiote-text, var(--text, #e4e4e7));
  font-size: 0.85rem;
  font-family: inherit;
}

.symbiote-input textarea:focus {
  outline: none;
  border-color: var(--symbiote-primary, var(--primary, #3B82F6));
}

.symbiote-send {
  width: 40px;
  height: 40px;
  background: var(--symbiote-primary, var(--primary, #3B82F6));
  border: none;
  border-radius: 0.5rem;
  color: white;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.symbiote-send:hover { background: var(--symbiote-primary-hover, var(--primary-hover, #2563eb)); }
.symbiote-send:disabled { opacity: 0.5; cursor: not-allowed; }

/* ── Spinner ── */
.symbiote-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--symbiote-text-muted, var(--text-muted, #a1a1aa));
  border-top-color: var(--symbiote-primary, var(--primary, #3B82F6));
  border-radius: 50%;
  animation: symbiote-spin 0.8s linear infinite;
  margin-right: 0.4rem;
  vertical-align: middle;
}

@keyframes symbiote-spin { to { transform: rotate(360deg); } }

/* ── Mobile ── */
@media (max-width: 640px) {
  .symbiote-panel {
    width: 100%;
    right: -100%;
  }
  .symbiote-fab {
    right: 0.75rem;
  }
}

/* Hide FAB when panel is open */
.symbiote-panel.open ~ .symbiote-fab { display: none; }
`;
