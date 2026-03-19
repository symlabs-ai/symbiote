/**
 * Symbiote Chat Adapter Interface
 *
 * The host application must provide an adapter object implementing these methods
 * to connect the <symbiote-chat> component to its backend.
 *
 * @typedef {Object} SymbioteChatAdapter
 * @property {(message: string, context: string) => Promise<ChatResponse>} sendMessage
 * @property {(sessionKey: string) => Promise<HistoryResponse>} loadHistory
 * @property {(sessionKey: string) => Promise<void>} clearHistory
 */

/**
 * @typedef {Object} ChatResponse
 * @property {string} response - Markdown text from the assistant
 * @property {string[]} [suggestions] - Follow-up suggestion texts
 * @property {ToolResult[]} [tool_results] - Tool execution results
 */

/**
 * @typedef {Object} ToolResult
 * @property {string} tool_id - Tool identifier
 * @property {boolean} success - Whether the tool call succeeded
 * @property {*} [output] - Tool output (any JSON-serializable value)
 * @property {string} [error] - Error message if failed
 */

/**
 * @typedef {Object} HistoryResponse
 * @property {Array<{role: string, content: string}>} messages
 */

/**
 * @typedef {Object} StreamEndpoint
 * @property {string} url - POST endpoint that returns SSE stream
 * @property {Object} [headers] - Extra headers for the fetch request
 * @property {*} [body] - Custom body (default: component builds JSON with message + context)
 */

/**
 * Optional streaming support.
 * If the adapter implements sendMessageStream, the component uses SSE streaming
 * instead of the sync sendMessage call.
 *
 * SSE events expected from the endpoint:
 *   text_delta    { text }
 *   tool_start    { tool_id, name, detail }
 *   tool_done     { tool_id, name, success, result_count, error }
 *   response_done { text, suggestions, tool_results }
 *   error         { message }
 *
 * @property {(message: string, context: string) => Promise<StreamEndpoint>} [sendMessageStream]
 *
 * Optional hooks (all are optional, work with both sync and streaming modes):
 *
 * @property {(text: string) => string} [textFilter]
 *   Called before every markdown render during streaming and on final render.
 *   Use to strip protocol artifacts (e.g. :::suggestions blocks) from the displayed text.
 *   Example: (text) => text.replace(/\n*:::suggestions[\s\S]*$/, '')
 *
 * @property {(element: HTMLElement, rawText: string) => void} [onMessageRendered]
 *   Called after each assistant message is rendered (both sync and stream final).
 *   The element is the .symbiote-msg DOM node inside Shadow DOM.
 *   Use to inject custom action buttons (e.g. "Insert in Editor", "Replace").
 *
 * @property {() => string} [loadingContent]
 *   Returns custom HTML for the loading indicator shown while waiting for response.
 *   Default: spinner + "..."
 *   Example: () => '<span class="my-pulse">Newsing...</span>'
 */

/**
 * NoopAdapter — used when no adapter is set.
 * Logs warnings so the developer knows they need to wire the adapter.
 */
const NoopAdapter = {
  async sendMessage(_message, _context) {
    console.warn('[symbiote-chat] No adapter set. Provide an adapter via element.adapter = { sendMessage, loadHistory, clearHistory }');
    return { response: 'Adapter not configured.' };
  },
  async loadHistory(_sessionKey) {
    return { messages: [] };
  },
  async clearHistory(_sessionKey) {},
};
