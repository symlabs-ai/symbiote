"""Engineered prompts for LLM-based reflection, compression, and (future) skill review.

These prompts are the core "intellectual property" of the self-improvement
loop. They're modular blocks so the same anti-patterns / signals language
can be reused across Reflection (close_session), Consolidator compression
(in-session token overflow), and a future background Skill Review.

Adapted from Hermes (~/dev/research/hermes-agent/agent/background_review.py)
with Symbiote-specific output schema (PATCH vs CREATE on MemoryEntry) and
PT-BR/EN dual-language examples so the prompts work for both Symlabs
products (PT-heavy) and embedded clients with EN traffic.

Interpolation
-------------
Use ``render_prompt(template, **values)`` — NOT ``str.format()``. The prompts
contain JSON schemas with literal ``{`` / ``}`` braces; ``format()`` requires
each of those to be doubled, and forgetting an escape produces a ``KeyError``
silently (the Reflection path then falls back to keyword and the LLM mode
never works for that symbiote). ``render_prompt`` does plain ``str.replace``
on the named placeholders only — no escaping required, no fragility.
"""

from __future__ import annotations

_SIGNALS_BLOCK = """\
Capture (any one warrants an entry). The language of the source conversation
is irrelevant — match on intent, not surface tokens.

- User corrected your style/tone/format/verbosity -> type=preference
    EN: 'stop doing X', 'too verbose', 'just answer', 'don't format like this',
        'I hate when you Y', explicit 'remember this'
    PT-BR: 'para de fazer X', 'tá muito longo', 'responde direto', 'sem
        rodeio', 'não me chame de senhor', 'odeio quando você Y', 'lembra
        disso pra próxima'

- User stated a durable rule that applies to all future sessions -> type=constraint
  (ONLY when truly absolute — frustrated 'sempre'/'nunca'/'always'/'never'
  in the heat of correction is preference, not constraint)
    EN: 'always run linter before commit', 'never push to main on Fridays'
    PT-BR: 'sempre rode o linter antes do commit', 'nunca dê push na sexta'

- Non-trivial fix, workaround, or debugging path emerged -> type=procedural
    EN: 'when X fails, do Y first', 'the trick is to flush cache before retry'
    PT-BR: 'quando X falhar, faz Y antes', 'o macete é limpar o cache antes
        de tentar de novo'

- A decision was made with explicit reasoning -> type=decision
    EN: 'we'll use pytest because of coverage integration'
    PT-BR: 'vamos usar pytest porque integra melhor com coverage'

- A long-term fact about the user/project/environment -> type=factual
  (stack, role, goal, deadlines, organizational context)
    EN: 'I lead the platform team at $company', 'the migration ships in Q3'
    PT-BR: 'lidero o time de plataforma na $empresa', 'a migração entrega
        no Q3'
"""

_ANTI_PATTERNS_BLOCK = """\
Do NOT capture (these turn into self-imposed constraints that bite later when
the environment changes):

- Environment-dependent failures: missing binaries, unconfigured credentials,
  'command not found' / 'comando não encontrado', uninstalled packages, fresh-
  install errors. The user can fix these — they are not durable rules.
  Capture the FIX as procedural, never the failure itself.

- Negative claims about tools/features: 'X tool is broken', 'Y does not work',
  'X está quebrado', 'Y não funciona', 'cannot use Z' / 'não dá pra usar Z'.
  These harden into refusals the agent cites against itself for months after
  the actual problem was fixed.

- Transient errors that resolved on retry. The lesson is the retry pattern,
  not the original failure.

- One-off task narratives. 'Analyze this PR' / 'analisa esse PR',
  'summarize today's news' / 'resume as notícias de hoje' — these are
  single-instance work, not a class of work that warrants a memory.
"""

_HIERARCHY_BLOCK = """\
Preference order -- pick the earliest that fits:
1. PATCH an existing memory. The list of currently-relevant memories is
   provided below with their ids. If new content refines, contradicts, or
   reinforces one of them, return action=patch with target_id.
2. CREATE new only when nothing existing covers it.

'Nothing to save' is a valid output but should NOT be the default. Sessions
with a real correction, a non-trivial fix, or an explicit user preference
typically produce at least one entry.
"""

_OUTPUT_SCHEMA = """\
Output ONLY a JSON array (no prose, no markdown fence). Empty array `[]` if
genuinely nothing surfaced. Schema per entry:
{
  "action": "create" | "patch",
  "target_id": "<existing memory id, REQUIRED for patch, OMIT for create>",
  "type": "preference" | "constraint" | "procedural" | "decision" | "factual",
  "content": "<one declarative sentence, max 200 chars>",
  "importance": <float 0.0-1.0>,
  "tags": ["<topic1>", "<topic2>", ...],
  "reasoning": "<which signal fired; max 80 chars>"
}

FIELD REQUIREMENTS (enforced by parser, not advisory):
- `tags` MUST be a non-empty list of 1-5 short topic tags (lowercase,
  hyphen-separated, no spaces). Tags are how future recall scores relevance —
  an entry without tags is dead weight. If you cannot think of a tag better
  than the type itself, you probably should not be creating the entry.
  Good: ["python", "testing", "ci"], ["onboarding", "tone"], ["docker", "cache"]
  Bad:  [], [""], ["preference"]  (just the type), ["misc"], ["general"]
- `content` MUST be a single declarative sentence in the same language the
  user used (PT-BR stays PT-BR, EN stays EN). Do NOT translate.
- `reasoning` is required so the audit log can review borderline calls.
"""


REFLECTION_PROMPT = f"""\
Review the conversation below and decide what is worth remembering for
future sessions with this user/symbiote.

{_SIGNALS_BLOCK}
{_ANTI_PATTERNS_BLOCK}
{_HIERARCHY_BLOCK}
{_OUTPUT_SCHEMA}

Existing relevant memories (consider for PATCH):
{{existing_memories}}

Conversation:
{{messages}}

JSON array:"""
# NOTE: the `{{ }}` above are deliberate — they're rendered to literal
# `{messages}` / `{existing_memories}` by the f-string, which then become
# named placeholders for ``render_prompt`` to replace. The interior of
# _OUTPUT_SCHEMA uses literal `{` / `}` for the JSON schema; render_prompt
# does not touch them.


_SKILL_HIERARCHY_BLOCK = """\
Preference order — pick the earliest action that fits. Skills are CLASS-LEVEL
playbooks, not session diaries; aim for a small library of well-shaped
umbrella skills, not a flat list of one-shot entries.

1. PATCH an existing skill. The list of skills currently available is below
   with their names + descriptions. If the new lesson refines, contradicts,
   or extends one of them, return action=patch with name=<that skill>,
   old_string=<text to find in SKILL.md>, new_string=<replacement>.
2. WRITE A SUPPORT FILE under an existing skill if the lesson is a worked
   example, reproduction recipe, API reference, or starter template — too
   specific to bloat the SKILL.md body but worth keeping under the umbrella.
   Use action=write_file with file_path starting in references/, templates/,
   or scripts/.
3. CREATE a NEW class-level skill only if NOTHING existing covers the
   territory. The name MUST be at the class level — generic enough that a
   future session in the same domain would naturally pick it up.
   Good names: 'nfe-extraction', 'symvault-workflow', 'helpdesk-triage'
   Bad names: 'fix-pr-123', 'debug-session-2026-05', 'audit-x-today',
              'todays-news', 'helper'  (too generic-bad means meaningless)

'Nothing to save' is valid — when the session ran smoothly with no
correction and no new technique surfaced. But it should NOT be the default.
Most sessions with a real fix, workaround, or user-style correction
produce at least one PATCH or WRITE_FILE.
"""

_SKILL_OUTPUT_SCHEMA = """\
Output ONLY a JSON array (no prose, no markdown fence). Empty array `[]` if
nothing surfaced. Schema per entry:

For action=create:
{
  "action": "create",
  "name": "<class-level kebab-case name>",
  "content": "<full SKILL.md body, INCLUDING YAML frontmatter (--- name: ... description: ... ---)>",
  "reasoning": "<which signal fired; max 80 chars>"
}

For action=patch:
{
  "action": "patch",
  "name": "<existing skill name>",
  "old_string": "<exact text in SKILL.md (or file_path) to replace>",
  "new_string": "<replacement text>",
  "file_path": "<optional, defaults to SKILL.md>",
  "reasoning": "<which signal fired; max 80 chars>"
}

For action=write_file:
{
  "action": "write_file",
  "name": "<existing skill name>",
  "file_path": "references/<topic>.md | templates/<n>.<ext> | scripts/<n>.<ext>",
  "file_content": "<full file content>",
  "reasoning": "<which signal fired; max 80 chars>"
}
"""


SKILL_REVIEW_PROMPT = f"""\
Review the conversation below and decide whether to update the skill library.
Skills are reusable procedural playbooks the next session loads automatically
when its query matches the skill's description. Be ACTIVE — most sessions
with a real correction or non-trivial technique produce at least one update.

{_SIGNALS_BLOCK}
{_ANTI_PATTERNS_BLOCK}
{_SKILL_HIERARCHY_BLOCK}
{_SKILL_OUTPUT_SCHEMA}

Skills currently available (consider for PATCH or WRITE_FILE):
{{existing_skills}}

Conversation:
{{messages}}

JSON array:"""


COMPRESSION_PROMPT = """\
Compress the following conversation messages into a concise narrative summary
(at most 300 words) that preserves:
- decisions made (with their reasoning when stated)
- files, tools, or systems touched
- unresolved questions or open threads
- key context that the next turn or session would need to continue

Drop greetings, acknowledgments, and noise. Do NOT extract preferences,
constraints, or "facts about the user" -- that is the Reflection engine's
job and happens separately on session close.

Output ONLY the summary as plain prose, in the same language the conversation
used (PT-BR stays PT-BR, EN stays EN — do NOT translate). No JSON, no bullet
lists unless they materially help. No preamble like "Here is the summary"
or "Aqui está o resumo".

Messages:
{messages}

Summary:"""


def render_prompt(template: str, **values: str) -> str:
    """Substitute named placeholders in a prompt template via ``str.replace``.

    Prefer this over ``str.format`` for prompts that embed JSON schemas or
    other literal braces. ``format`` requires every literal ``{`` / ``}`` to
    be doubled and silently raises ``KeyError`` when a placeholder name
    collides with a JSON key — both fragile and easy to break in review.

    Placeholders must be wrapped in single braces in the template, e.g.
    ``"{messages}"``, and passed as kwargs. Unknown placeholders are left as
    literal text (no error) so partial renders are visible to callers.
    """
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


__all__ = [
    "COMPRESSION_PROMPT",
    "REFLECTION_PROMPT",
    "SKILL_REVIEW_PROMPT",
    "_ANTI_PATTERNS_BLOCK",
    "_HIERARCHY_BLOCK",
    "_OUTPUT_SCHEMA",
    "_SIGNALS_BLOCK",
    "_SKILL_HIERARCHY_BLOCK",
    "_SKILL_OUTPUT_SCHEMA",
    "render_prompt",
]
