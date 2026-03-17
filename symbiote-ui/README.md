# symbiote-ui

Reusable AI chat panel Web Component for applications built on the [Symbiote](../README.md) kernel.

Drop a single `<symbiote-chat>` tag into any web page and wire it to your backend via an adapter object. No build step, no framework dependency — just vanilla JS with Shadow DOM isolation.

## Quick Start

```html
<script src="symbiote-chat.js"></script>

<symbiote-chat
  name="Clark"
  avatar="/img/clark.png"
  placeholder="Ask me anything..."
  greeting="Hi! I'm Clark."
  greeting-sub="I can help you manage your content."
  session-key="/current-page"
></symbiote-chat>

<script>
  const chat = document.querySelector('symbiote-chat');

  chat.adapter = {
    async sendMessage(message, context) {
      const resp = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, context }),
      });
      return resp.json();
      // Expected: { response: string, suggestions?: string[], tool_results?: ToolResult[] }
    },

    async loadHistory(sessionKey) {
      const resp = await fetch(`/api/history?key=${encodeURIComponent(sessionKey)}`);
      return resp.json();
      // Expected: { messages: [{role: 'user'|'assistant', content: string}] }
    },

    async clearHistory(sessionKey) {
      await fetch(`/api/clear?key=${encodeURIComponent(sessionKey)}`, { method: 'POST' });
    },
  };
</script>
```

## Installation

Copy `dist/symbiote-chat.js` (~68KB, includes marked.js for markdown rendering) into your project's static files and load it via `<script>`.

No npm, no build tools, no Node.js required.

## Attributes

| Attribute | Default | Description |
|-----------|---------|-------------|
| `name` | `"Assistant"` | Assistant display name |
| `avatar` | — | Avatar image URL (shows initials if omitted) |
| `placeholder` | `"Type a message..."` | Input textarea placeholder |
| `greeting` | `"Hi! I'm {name}."` | Empty state greeting |
| `greeting-sub` | `"How can I help you?"` | Empty state subtitle |
| `session-key` | `""` | Session identifier (passed to adapter) |
| `width` | `"420px"` | Panel width |
| `fab-label` | Same as `name` | Text on the floating action button |

## Properties (JS)

| Property | Type | Description |
|----------|------|-------------|
| `adapter` | `Object` | **Required.** `{ sendMessage, loadHistory, clearHistory }` |
| `initialSuggestions` | `Array` | `[{label, message}]` for empty state buttons |
| `contextProvider` | `Function` | `() => string` returning page context (called on each send) |

## Methods

| Method | Description |
|--------|-------------|
| `open()` | Open the chat panel |
| `close()` | Close the chat panel |
| `toggle()` | Toggle open/close |
| `sendMessage(text)` | Programmatically send a message |
| `clearMessages()` | Clear UI and call `adapter.clearHistory()` |
| `addMessage(role, content)` | Inject a message without going through the adapter |

## Events

| Event | Detail | Description |
|-------|--------|-------------|
| `symbiote-open` | — | Panel opened |
| `symbiote-close` | — | Panel closed |
| `symbiote-message-sent` | `{message}` | User sent a message |
| `symbiote-message-received` | `{response, suggestions, tool_results}` | Assistant responded |
| `symbiote-error` | `{error}` | Adapter call failed |

## Theming

The component inherits CSS custom properties from the host page. Set these on `:root` or on the `<symbiote-chat>` element:

```css
:root {
  --symbiote-primary: #3B82F6;
  --symbiote-primary-hover: #2563eb;
  --symbiote-bg: #18181b;
  --symbiote-bg-secondary: #27272a;
  --symbiote-border: #3f3f46;
  --symbiote-text: #e4e4e7;
  --symbiote-text-muted: #a1a1aa;
  --symbiote-text-primary: #fff;
  --symbiote-fab-bottom: 1.5rem;
  --symbiote-fab-right: 1.25rem;
  --symbiote-width: 420px;
}
```

The component also falls back to generic variable names (`--primary`, `--bg`, etc.) so it works out of the box with common dark themes.

## Tool Result Badges

When `adapter.sendMessage()` returns `tool_results`, the component renders colored badges above the response:

- **Green** badge for successful tool calls
- **Red** badge for failed tool calls
- Hover tooltip shows the result/error details

## Building from Source

The distributable is a single concatenated file. Rebuild after editing source files:

```bash
./build.sh
```

No Node.js required — it's just `cat`.

## Examples

Open `examples/basic.html` in a browser to see the component in action with a mock adapter.

## Architecture

```
src/
  adapter.js            # Adapter interface + NoopAdapter
  styles.js             # CSS (all themeable via custom properties)
  template.js           # Shadow DOM HTML template + SVG icons
  message-renderer.js   # Markdown rendering (marked.js + fallback)
  symbiote-chat.js      # Web Component class (SymbioteChat)
vendor/
  marked.min.js         # Vendored marked.js (~40KB)
dist/
  symbiote-chat.js      # Built distributable (single file)
```
