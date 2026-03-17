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
