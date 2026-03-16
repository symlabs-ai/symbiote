#!/bin/bash
set -eu

# ============================================================================
# Smoke Test — Symbiote CLI (cycle-01)
# Exercises the full product lifecycle via real CLI commands.
# No mocks — uses mock LLM provider (built-in, not an external mock).
# ============================================================================

DB="/tmp/symbiote-smoke-$$.db"
trap 'rm -f "$DB"' EXIT

CLI="symbiote --db-path $DB --llm mock"

echo "══════════════════════════════════════════════"
echo " SMOKE TEST — Symbiote CLI"
echo "══════════════════════════════════════════════"

# 1. Create symbiote
echo ""
echo "── 1. Create Symbiote ──"
CREATE_OUT=$($CLI create --name "SmokeBot" --role "tester" --persona-json '{"tone": "direct"}')
echo "$CREATE_OUT"
SYM_ID=$(echo "$CREATE_OUT" | grep -oP '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
echo "  → Symbiote ID: $SYM_ID"

# 2. List symbiotes
echo ""
echo "── 2. List Symbiotes ──"
$CLI list

# 3. Start session
echo ""
echo "── 3. Start Session ──"
SESSION_OUT=$($CLI session start "$SYM_ID" --goal "Smoke test cycle-01")
echo "$SESSION_OUT"
SESS_ID=$(echo "$SESSION_OUT" | grep -oP '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
echo "  → Session ID: $SESS_ID"

# 4. Chat (Value Track: chat)
echo ""
echo "── 4. CHAT ──"
$CLI chat "$SESS_ID" "Hello, how does memory work?"

# 5. Learn (Value Track: learn)
echo ""
echo "── 5. LEARN ──"
$CLI learn "$SESS_ID" "User prefers concise answers" --type preference --importance 0.9

# 6. Teach (Value Track: teach)
echo ""
echo "── 6. TEACH ──"
$CLI teach "$SESS_ID" "memory"

# 7. Show (Value Track: show)
echo ""
echo "── 7. SHOW ──"
$CLI show "$SESS_ID" "answers"

# 8. Reflect (Value Track: reflect)
echo ""
echo "── 8. REFLECT ──"
$CLI reflect "$SESS_ID"

# 9. Memory search
echo ""
echo "── 9. Memory Search ──"
$CLI memory search "concise"

# 10. Export session
echo ""
echo "── 10. Export Session ──"
$CLI export session "$SESS_ID"

# 11. Close session
echo ""
echo "── 11. Close Session ──"
$CLI session close "$SESS_ID"

echo ""
echo "══════════════════════════════════════════════"
echo " SMOKE TEST — PASSED"
echo "══════════════════════════════════════════════"
