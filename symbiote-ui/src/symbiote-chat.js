/**
 * <symbiote-chat> — Reusable AI chat panel Web Component
 *
 * Usage:
 *   <symbiote-chat name="Clark" avatar="/img/clark.png" placeholder="Ask me..."
 *                  greeting="Hi!" greeting-sub="How can I help?"
 *                  session-key="/current-page"></symbiote-chat>
 *   <script>
 *     document.querySelector('symbiote-chat').adapter = { sendMessage, loadHistory, clearHistory };
 *   </script>
 */

class SymbioteChat extends HTMLElement {

  static get observedAttributes() {
    return ['name', 'avatar', 'placeholder', 'greeting', 'greeting-sub', 'session-key', 'width', 'fab-label'];
  }

  constructor() {
    super();
    this._adapter = NoopAdapter;
    this._initialSuggestions = [];
    this._contextProvider = null;
    this._isOpen = false;
    this._historyLoaded = false;
    this._msgCounter = 0;
    this._shadow = this.attachShadow({ mode: 'open' });
  }

  // ── Properties ──

  get adapter() { return this._adapter; }
  set adapter(val) { this._adapter = val || NoopAdapter; }

  get initialSuggestions() { return this._initialSuggestions; }
  set initialSuggestions(val) {
    this._initialSuggestions = val || [];
    this._renderInitialSuggestions();
  }

  get contextProvider() { return this._contextProvider; }
  set contextProvider(fn) { this._contextProvider = typeof fn === 'function' ? fn : null; }

  // ── Lifecycle ──

  connectedCallback() {
    this._render();
    this._bindEvents();
  }

  attributeChangedCallback(_name, _old, _new) {
    if (this._shadow.innerHTML && _old !== _new) {
      this._render();
      this._bindEvents();
    }
  }

  // ── Public methods ──

  open() {
    this._setOpen(true);
  }

  close() {
    this._setOpen(false);
  }

  toggle() {
    this._setOpen(!this._isOpen);
  }

  async sendMessage(text) {
    if (!text || !text.trim()) return;
    this._hideEmpty();
    this._addMessage('user', text.trim());
    await this._doSend(text.trim());
  }

  clearMessages() {
    const container = this._ref('messages');
    if (!container) return;
    const empty = this._ref('empty');
    container.innerHTML = '';
    if (empty) {
      container.appendChild(empty);
      empty.style.display = '';
    }
    this._historyLoaded = false;
    const key = this.getAttribute('session-key') || '';
    this._adapter.clearHistory(key).catch(() => {});
  }

  addMessage(role, content) {
    this._hideEmpty();
    this._addMessage(role, content);
  }

  // ── Rendering ──

  _render() {
    const name = this.getAttribute('name') || 'Assistant';
    const avatar = this.getAttribute('avatar') || '';
    const placeholder = this.getAttribute('placeholder') || 'Type a message...';
    const greeting = this.getAttribute('greeting') || `Hi! I'm ${name}.`;
    const greetingSub = this.getAttribute('greeting-sub') || 'How can I help you?';
    const fabLabel = this.getAttribute('fab-label') || name;
    const width = this.getAttribute('width') || '420px';

    const avatarHtml = buildAvatarHtml(name, avatar);

    this._shadow.innerHTML = `
      <style>
        :host { --symbiote-width: ${width}; }
        ${SYMBIOTE_STYLES}
      </style>
      ${buildTemplate({ name, avatarHtml, placeholder, greeting, greetingSub, fabLabel })}
    `;
  }

  _bindEvents() {
    // FAB click
    const fab = this._shadow.querySelector('.symbiote-fab');
    if (fab) fab.addEventListener('click', () => this.toggle());

    // Close / clear buttons + overlay
    this._shadow.querySelectorAll('[data-action="close"]').forEach(el => {
      el.addEventListener('click', () => this.close());
    });
    this._shadow.querySelectorAll('[data-action="clear"]').forEach(el => {
      el.addEventListener('click', () => this.clearMessages());
    });

    // Send button
    const sendBtn = this._ref('sendBtn');
    if (sendBtn) sendBtn.addEventListener('click', () => this._onSend());

    // Enter to send
    const input = this._ref('input');
    if (input) {
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this._onSend();
        }
      });
    }
  }

  _ref(name) {
    return this._shadow.querySelector(`[data-ref="${name}"]`);
  }

  // ── Panel state ──

  _setOpen(open) {
    this._isOpen = open;
    const panel = this._shadow.querySelector('.symbiote-panel');
    const overlay = this._shadow.querySelector('.symbiote-overlay');

    if (open) {
      panel?.classList.add('open');
      overlay?.classList.add('open');
      this._ref('input')?.focus();
      if (!this._historyLoaded) {
        this._historyLoaded = true;
        this._loadHistory();
      }
      this.dispatchEvent(new CustomEvent('symbiote-open'));
    } else {
      panel?.classList.remove('open');
      overlay?.classList.remove('open');
      this.dispatchEvent(new CustomEvent('symbiote-close'));
    }
  }

  // ── History ──

  async _loadHistory() {
    const key = this.getAttribute('session-key') || '';
    try {
      const data = await this._adapter.loadHistory(key);
      if (data.messages && data.messages.length > 0) {
        this._hideEmpty();
        data.messages.forEach(msg => this._addMessage(msg.role, msg.content));
      }
    } catch (e) {
      console.error('[symbiote-chat] History load error:', e);
    }
  }

  // ── Send flow ──

  async _onSend() {
    const input = this._ref('input');
    const message = (input?.value || '').trim();
    if (!message) return;

    this._hideEmpty();
    this._addMessage('user', message);
    input.value = '';

    await this._doSend(message);
  }

  async _doSend(message) {
    const sendBtn = this._ref('sendBtn');
    if (sendBtn) sendBtn.disabled = true;

    const loadingId = this._addMessage('assistant', '<span class="symbiote-spinner"></span> ...', true);

    try {
      const context = this._contextProvider ? this._contextProvider() : '';
      const data = await this._adapter.sendMessage(message, context);

      this._removeMessage(loadingId);

      // Tool badges
      if (data.tool_results && data.tool_results.length) {
        this._addToolBadges(data.tool_results);
      }

      // Assistant message
      this._addMessage('assistant', data.response);

      // Follow-up suggestions
      if (data.suggestions && data.suggestions.length) {
        this._addFollowups(data.suggestions);
      }

      this.dispatchEvent(new CustomEvent('symbiote-message-received', {
        detail: { response: data.response, suggestions: data.suggestions, tool_results: data.tool_results }
      }));

    } catch (e) {
      this._removeMessage(loadingId);
      this._addMessage('assistant', 'Connection error.');
      this.dispatchEvent(new CustomEvent('symbiote-error', { detail: { error: e } }));
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }

    this.dispatchEvent(new CustomEvent('symbiote-message-sent', { detail: { message } }));
  }

  // ── DOM helpers ──

  _hideEmpty() {
    const empty = this._ref('empty');
    if (empty) empty.style.display = 'none';
  }

  _getMessagesContainer() {
    return this._shadow.querySelector('.symbiote-messages');
  }

  _addMessage(role, content, isHtml = false) {
    const container = this._getMessagesContainer();
    if (!container) return null;

    const id = 'symbiote-msg-' + (++this._msgCounter);
    const div = document.createElement('div');
    div.className = `symbiote-msg ${role}`;
    div.id = id;
    div.setAttribute('part', 'message');

    if (isHtml) {
      div.innerHTML = content;
    } else if (role === 'user') {
      div.textContent = content;
    } else {
      div.innerHTML = symbioteFormatMarkdown(content);
    }

    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return id;
  }

  _removeMessage(id) {
    if (!id) return;
    const el = this._shadow.getElementById(id);
    if (el) el.remove();
  }

  _addToolBadges(results) {
    const container = this._getMessagesContainer();
    if (!container) return;

    const wrap = document.createElement('div');
    wrap.className = 'symbiote-tool-badges';

    results.forEach(tr => {
      const badge = document.createElement('span');
      const status = tr.success ? 'success' : 'error';
      badge.className = `symbiote-tool-badge ${status}`;
      const label = tr.tool_id.replace(/_/g, ' ');
      const detail = tr.success
        ? (typeof tr.output === 'string' ? tr.output.substring(0, 100) : JSON.stringify(tr.output || '').substring(0, 100))
        : (tr.error || 'Error');
      badge.title = detail;
      badge.innerHTML = `<span class="badge-dot"></span>${label}`;
      wrap.appendChild(badge);
    });

    container.appendChild(wrap);
    container.scrollTop = container.scrollHeight;
  }

  _addFollowups(suggestions) {
    const container = this._getMessagesContainer();
    if (!container) return;

    // Remove previous
    const old = this._shadow.querySelector('.symbiote-followups');
    if (old) old.remove();

    const wrap = document.createElement('div');
    wrap.className = 'symbiote-followups';

    suggestions.forEach(text => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'symbiote-followup';
      btn.textContent = text;
      btn.addEventListener('click', () => {
        wrap.remove();
        this.sendMessage(text);
      });
      wrap.appendChild(btn);
    });

    container.appendChild(wrap);
    container.scrollTop = container.scrollHeight;
  }

  _renderInitialSuggestions() {
    const container = this._ref('initialSuggestions');
    if (!container) return;
    container.innerHTML = '';

    this._initialSuggestions.forEach(item => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'symbiote-suggestion';
      btn.textContent = item.label || item.message;
      btn.addEventListener('click', () => this.sendMessage(item.message));
      container.appendChild(btn);
    });
  }
}

// Register the custom element
if (!customElements.get('symbiote-chat')) {
  customElements.define('symbiote-chat', SymbioteChat);
}
