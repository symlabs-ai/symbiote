══════════════════════════════════════════════
 SMOKE TEST — Symbiote CLI
══════════════════════════════════════════════

── 1. Create Symbiote ──
Created symbiote: c5956021-6095-444c-81e5-677c03956141
  → Symbiote ID: c5956021-6095-444c-81e5-677c03956141

── 2. List Symbiotes ──
                              Symbiotes
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━┓
┃ ID                                   ┃ Name     ┃ Role   ┃ Status ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━┩
│ c5956021-6095-444c-81e5-677c03956141 │ SmokeBot │ tester │ active │
└──────────────────────────────────────┴──────────┴────────┴────────┘

── 3. Start Session ──
Started session: a0b282f8-7e47-404f-bd78-14efb3733648
  → Session ID: a0b282f8-7e47-404f-bd78-14efb3733648

── 4. CHAT ──
╭───────────────────────────────── Assistant ──────────────────────────────────╮
│ I'm a mock symbiote. Configure a real LLM provider to get real responses.    │
╰──────────────────────────────────────────────────────────────────────────────╯

── 5. LEARN ──
Learned: 381c72d9-cf5e-4631-a08d-631465c27540

── 6. TEACH ──
No knowledge or memories found for: memory

── 7. SHOW ──
                                 Show: answers

Session Messages

 • user: Hello, how does memory work?
 • assistant: I'm a mock symbiote. Configure a real LLM provider to get real
   responses.

Memories

 • [preference, importance=0.9] User prefers concise answers

── 8. REFLECT ──
                               Reflection Result
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Key      ┃ Value                                                             ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Messages │ 2                                                                 │
│ Roles    │ {'assistant': 1, 'user': 1}                                       │
│ Summary  │ Hello, how does memory work?                                      │
│          │ I'm a mock symbiote. Configure a real LLM provider to get real    │
│          │ responses.                                                        │
└──────────┴───────────────────────────────────────────────────────────────────┘

── 9. Memory Search ──
                             Memory Search Results
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ID                         ┃ Type       ┃ Scope  ┃ Content                   ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 381c72d9-cf5e-4631-a08d-6… │ preference │ global │ User prefers concise      │
│                            │            │        │ answers                   │
└────────────────────────────┴────────────┴────────┴───────────────────────────┘

── 10. Export Session ──
╭───────────────────────── Session Export (Markdown) ──────────────────────────╮
│ # Session Export                                                             │
│                                                                              │
│ ## Session                                                                   │
│                                                                              │
│ - **ID:** a0b282f8-7e47-404f-bd78-14efb3733648                               │
│ - **Goal:** Smoke test cycle-01                                              │
│ - **Status:** active                                                         │
│ - **Started:** 2026-03-16 20:49:37                                           │
│                                                                              │
│ ## Messages                                                                  │
│                                                                              │
│ **user** (2026-03-16 20:49:38):                                              │
│ > Hello, how does memory work?                                               │
│                                                                              │
│ **assistant** (2026-03-16 20:49:38):                                         │
│ > I'm a mock symbiote. Configure a real LLM provider to get real responses.  │
│                                                                              │
│ ## Decisions                                                                 │
│                                                                              │
│ No decisions found.                                                          │
│                                                                              │
╰──────────────────────────────────────────────────────────────────────────────╯

── 11. Close Session ──
╭─────────────────────────────── Session Closed ───────────────────────────────╮
│ Session a0b282f8-7e47-404f-bd78-14efb3733648 closed.                         │
│ Summary: Hello, how does memory work?                                        │
│ I'm a mock symbiote. Configure a real LLM provider to get real responses.    │
╰──────────────────────────────────────────────────────────────────────────────╯

══════════════════════════════════════════════
 SMOKE TEST — PASSED
══════════════════════════════════════════════
