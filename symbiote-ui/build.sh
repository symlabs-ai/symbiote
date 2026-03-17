#!/bin/bash
# Build symbiote-chat.js distributable (single file, no Node.js required)
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Building symbiote-chat.js..."

cat > "$DIR/dist/symbiote-chat.js" <<'HEADER'
/**
 * symbiote-chat.js — Reusable AI chat panel Web Component
 * Part of the Symbiote project (https://github.com/symlabs-ai/symbiote)
 * License: AGPL-3.0
 *
 * Usage:
 *   <script src="symbiote-chat.js"></script>
 *   <symbiote-chat name="Assistant" greeting="Hello!"></symbiote-chat>
 *   <script>
 *     document.querySelector('symbiote-chat').adapter = {
 *       async sendMessage(msg, ctx) { return { response: '...' }; },
 *       async loadHistory(key) { return { messages: [] }; },
 *       async clearHistory(key) {},
 *     };
 *   </script>
 */
(function() {
'use strict';
HEADER

# Vendor: marked.js (exposed as _symbioteMarked to avoid global conflicts)
echo "" >> "$DIR/dist/symbiote-chat.js"
echo "// ── Vendor: marked.js ──" >> "$DIR/dist/symbiote-chat.js"
echo "var _symbioteMarked;" >> "$DIR/dist/symbiote-chat.js"
echo "(function() {" >> "$DIR/dist/symbiote-chat.js"
cat "$DIR/vendor/marked.min.js" >> "$DIR/dist/symbiote-chat.js"
echo "" >> "$DIR/dist/symbiote-chat.js"
echo "  _symbioteMarked = typeof marked !== 'undefined' ? marked : (typeof module !== 'undefined' && module.exports);" >> "$DIR/dist/symbiote-chat.js"
echo "})();" >> "$DIR/dist/symbiote-chat.js"

# Source files in dependency order
for f in adapter.js styles.js template.js message-renderer.js symbiote-chat.js; do
  echo "" >> "$DIR/dist/symbiote-chat.js"
  echo "// ── src/$f ──" >> "$DIR/dist/symbiote-chat.js"
  cat "$DIR/src/$f" >> "$DIR/dist/symbiote-chat.js"
done

# Close IIFE
echo "" >> "$DIR/dist/symbiote-chat.js"
echo "})();" >> "$DIR/dist/symbiote-chat.js"

SIZE=$(wc -c < "$DIR/dist/symbiote-chat.js")
echo "Done: dist/symbiote-chat.js (${SIZE} bytes)"
