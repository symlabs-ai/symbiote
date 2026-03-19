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
        data.messages.forEach(msg => {
          // Tool badges from history (if the message carries tool_results)
          if (msg.tool_results && msg.tool_results.length) {
            this._addToolBadges(msg.tool_results);
          }
          this._addMessage(msg.role, msg.content);
        });
        // Extract suggestions from last assistant message
        const lastAssistant = [...data.messages].reverse().find(m => m.role === 'assistant');
        if (lastAssistant) {
          const suggestions = this._extractSuggestions(lastAssistant.content);
          if (suggestions.length) this._addFollowups(suggestions);
        }
      }
    } catch (e) {
      console.error('[symbiote-chat] History load error:', e);
    }
  }

  _extractSuggestions(text) {
    if (!text) return [];
    const match = text.match(/:::suggestions\s*\n([\s\S]*?)(?:\n:::|\s*$)/);
    if (!match) return [];
    return match[1]
      .split('\n')
      .map(line => line.replace(/^[\s*•\-]+/, '').trim())
      .filter(Boolean);
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
    if (this._adapter.sendMessageStream) {
      return this._doSendStream(message);
    }
    return this._doSendSync(message);
  }

  async _doSendSync(message) {
    const sendBtn = this._ref('sendBtn');
    if (sendBtn) sendBtn.disabled = true;

    const loadingHtml = this._adapter.loadingContent
      ? this._adapter.loadingContent()
      : '<span class="symbiote-spinner"></span> ...';
    const loadingId = this._addMessage('assistant', loadingHtml, true);

    try {
      const context = this._contextProvider ? this._contextProvider() : '';
      const data = await this._adapter.sendMessage(message, context);

      this._removeMessage(loadingId);

      // Tool badges
      if (data.tool_results && data.tool_results.length) {
        this._addToolBadges(data.tool_results);
      }

      // Assistant message (apply textFilter if provided)
      const filteredResponse = this._applyTextFilter(data.response);
      const msgId = this._addMessage('assistant', filteredResponse);
      this._fireMessageRendered(msgId, data.response);

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

  async _doSendStream(message) {
    const sendBtn = this._ref('sendBtn');
    if (sendBtn) sendBtn.disabled = true;

    // Badges container (shown above the assistant message, populated by tool_start/done)
    const badgesWrap = document.createElement('div');
    badgesWrap.className = 'symbiote-tool-badges';
    const container = this._getMessagesContainer();

    // Streaming assistant message bubble (starts empty)
    const msgId = 'symbiote-msg-' + (++this._msgCounter);
    const msgDiv = document.createElement('div');
    msgDiv.className = 'symbiote-msg assistant';
    msgDiv.id = msgId;
    msgDiv.setAttribute('part', 'message');
    const loadingHtml = this._adapter.loadingContent
      ? this._adapter.loadingContent()
      : '<span class="symbiote-spinner"></span>';
    msgDiv.innerHTML = loadingHtml;

    container.appendChild(badgesWrap);
    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;

    let accumulated = '';
    let renderTimer = null;
    const throttleMs = 100;

    const scheduleRender = () => {
      if (renderTimer) return;
      renderTimer = setTimeout(() => {
        renderTimer = null;
        msgDiv.innerHTML = symbioteFormatMarkdown(this._applyTextFilter(accumulated));
        container.scrollTop = container.scrollHeight;
      }, throttleMs);
    };

    try {
      const context = this._contextProvider ? this._contextProvider() : '';
      const endpoint = await this._adapter.sendMessageStream(message, context);

      const fetchHeaders = { 'Content-Type': 'application/json', ...(endpoint.headers || {}) };
      const fetchBody = endpoint.body !== undefined
        ? (typeof endpoint.body === 'string' ? endpoint.body : JSON.stringify(endpoint.body))
        : JSON.stringify({ message, context });

      const resp = await fetch(endpoint.url, { method: 'POST', headers: fetchHeaders, body: fetchBody });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line

        let eventType = null;
        for (const line of lines) {
          if (line.startsWith('event:')) {
            eventType = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            const raw = line.slice(5).trim();
            if (!raw) continue;
            try {
              const data = JSON.parse(raw);
              this._handleSSE(eventType || 'text_delta', data, badgesWrap, msgDiv, () => {
                accumulated += (data.text || '');
                scheduleRender();
              });
            } catch (_) { /* ignore malformed JSON */ }
            eventType = null;
          }
        }
      }

      // Flush any pending render
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      if (accumulated) {
        msgDiv.innerHTML = symbioteFormatMarkdown(this._applyTextFilter(accumulated));
        this._fireMessageRendered(msgId, accumulated);
      } else {
        msgDiv.remove();
      }
      if (!badgesWrap.children.length) badgesWrap.remove();
      container.scrollTop = container.scrollHeight;

    } catch (e) {
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      msgDiv.innerHTML = symbioteFormatMarkdown(accumulated || 'Connection error.');
      this.dispatchEvent(new CustomEvent('symbiote-error', { detail: { error: e } }));
    } finally {
      if (sendBtn) sendBtn.disabled = false;
    }

    this.dispatchEvent(new CustomEvent('symbiote-message-sent', { detail: { message } }));
  }

  _handleSSE(event, data, badgesWrap, msgDiv, onTextDelta) {
    const container = this._getMessagesContainer();

    switch (event) {
      case 'text_delta':
        onTextDelta();
        break;

      case 'tool_start':
        this._addToolBadgePending(badgesWrap, data.tool_id, data.name, data.detail);
        break;

      case 'tool_done':
        this._updateToolBadge(badgesWrap, data.tool_id, data.name, data.success, data.result_count, data.error);
        break;

      case 'response_done': {
        // Final text override (if the server sends full text at end)
        if (data.text) {
          msgDiv.innerHTML = symbioteFormatMarkdown(this._applyTextFilter(data.text));
        }
        // Final tool_results (for adapters that also send sync-style results)
        if (data.tool_results && data.tool_results.length) {
          data.tool_results.forEach(tr => {
            const existing = badgesWrap.querySelector(`[data-tool-id="${tr.tool_id}"]`);
            if (!existing) {
              this._updateToolBadge(badgesWrap, tr.tool_id, tr.tool_id, tr.success, null, tr.error);
            }
          });
        }
        // Follow-up suggestions
        if (data.suggestions && data.suggestions.length) {
          this._addFollowups(data.suggestions);
        }
        this.dispatchEvent(new CustomEvent('symbiote-message-received', {
          detail: { response: data.text, suggestions: data.suggestions, tool_results: data.tool_results }
        }));
        break;
      }

      case 'error':
        msgDiv.innerHTML = symbioteFormatMarkdown(data.message || 'An error occurred.');
        this.dispatchEvent(new CustomEvent('symbiote-error', { detail: { error: new Error(data.message) } }));
        break;
    }

    if (container) container.scrollTop = container.scrollHeight;
  }

  _addToolBadgePending(wrap, toolId, name, detail) {
    const badge = document.createElement('span');
    badge.className = 'symbiote-tool-badge pending';
    badge.setAttribute('data-tool-id', toolId);
    const label = name || toolId.replace(/_/g, ' ');
    badge.title = detail || label;
    badge.innerHTML = `<span class="badge-dot"></span>${label}`;
    wrap.appendChild(badge);
  }

  _updateToolBadge(wrap, toolId, name, success, resultCount, error) {
    let badge = wrap.querySelector(`[data-tool-id="${toolId}"]`);
    if (!badge) {
      // Badge wasn't created via tool_start — create it now
      badge = document.createElement('span');
      badge.setAttribute('data-tool-id', toolId);
      wrap.appendChild(badge);
    }
    const status = success ? 'success' : 'error';
    badge.className = `symbiote-tool-badge ${status}`;
    const label = name || toolId.replace(/_/g, ' ');
    const suffix = success && resultCount != null ? ` (${resultCount})` : '';
    badge.title = success ? `${label}${suffix}` : (error || 'Error');
    badge.innerHTML = `<span class="badge-dot"></span>${label}${suffix}`;
  }

  // ── Adapter hooks ──

  _applyTextFilter(text) {
    if (this._adapter.textFilter) {
      try { return this._adapter.textFilter(text); } catch (_) {}
    }
    return text;
  }

  _fireMessageRendered(msgId, rawText) {
    if (!msgId) return;
    const el = this._shadow.getElementById(msgId);
    if (!el) return;
    if (this._adapter.onMessageRendered) {
      try { this._adapter.onMessageRendered(el, rawText); } catch (_) {}
    }
    this.dispatchEvent(new CustomEvent('symbiote-message-rendered', {
      detail: { element: el, rawText }
    }));
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
      div.innerHTML = symbioteFormatMarkdown(this._applyTextFilter(content));
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
