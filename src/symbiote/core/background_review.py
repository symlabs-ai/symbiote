"""BackgroundReviewEngine — daemon-thread skill review after sessions and tool batches.

Triggered in two places (Sprint 4):

* **Mid-session** by ``ChatRunner`` every ``skill_nudge_interval`` tool
  iterations. Lets the agent capture a fix or workaround the moment it works,
  not only at the end of the conversation.
* **Final** by ``SymbioteKernel.close_session`` after reflection. Looks at
  the whole transcript with the benefit of completion signals.

Both are non-blocking (daemon threads). The user gets their response /
``close_session`` return value first; the review writes to the skill library
later. Failure modes (LLM error, JSON parse, write conflict) are logged but
never raised — a flaky review must never break the foreground turn.

Design choice — single LLM call, not a tool loop
-------------------------------------------------
Hermes forks an entire AIAgent with a tool whitelist (memory + skill tools)
so the model can multi-turn: skill_list -> skill_view -> skill_manage.
Symbiote takes a simpler path for Sprint 4: ONE engineered LLM call with
``existing_skills`` listed in the prompt; the LLM returns a JSON array of
operations that ``SkillsStore`` applies sequentially. Reasons:

1. Lower cost and latency. One call vs N.
2. No reentrant ChatRunner / ToolGateway complexity, no tool whitelist
   leakage risks during the fork.
3. Easier auditing — the JSON array IS the audit trail.

If quality plateaus we can graduate to a true tool loop in a future sprint;
for now the simple loop matches the Reflection design and shares its prompt
infrastructure.

Provenance
----------
Every operation runs under ``set_current_write_origin(BACKGROUND_REVIEW)``
so ``SkillsStore.create`` tags new skills as ``agent_created=true`` with
``status=quarantine``. They become visible to the LLM only after manual
promotion via ``symbiote skills promote <name>`` (Sprint 3 CLI) or — in a
later sprint — automatic promotion after N successful loads.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from symbiote.core._review_prompts import (
    SKILL_REVIEW_PROMPT,
    build_skill_review_prompt,
    render_prompt,
)
from symbiote.core.provenance import (
    BACKGROUND_REVIEW,
    reset_current_write_origin,
    set_current_write_origin,
)
from symbiote.core.subjects import learns_from_subjects, subject_of_session
from symbiote.skills.store import (
    SkillError,
    SkillsStore,
)

if TYPE_CHECKING:
    from symbiote.core.ports import LLMPort, MessagePort, StoragePort
    from symbiote.skills.loader import SkillsLoader

_log = logging.getLogger(__name__)

# Maximum chars of session transcript we feed to the LLM. Mirrors the
# Reflection budget — large enough for typical sessions, small enough that
# we don't blow up the Haiku prompt or pay surprise tokens.
_DEFAULT_MAX_CHARS = 16_000
# Tool calls of the session shown to the reviewer: the procedure a skill is
# made of lives in them, not in the final answer ("Pronto! Salvei o resumo").
_TOOL_DIGEST_MAX_CHARS = 4_000
_TOOL_DIGEST_MAX_TURNS = 20
_TOOL_PARAMS_MAX_CHARS = 120

# Cap on how many skills we list in ``existing_skills`` for the LLM. Without
# this the prompt grew O(N) and a library of 50+ skills would dominate the
# context. Active skills win priority over quarantine when the cap bites.
_MAX_EXISTING_SKILLS_LISTED = 30


class BackgroundReviewEngine:
    """Spawns daemon threads that run a skill review on session messages.

    Construct once per kernel and reuse. Spawned threads are independent —
    a slow review on session A does not block a review on session B.
    """

    def __init__(
        self,
        *,
        llm: LLMPort,
        messages: MessagePort,
        store: SkillsStore,
        loader: SkillsLoader | None = None,
        max_active_skills: int = 20,
        max_quarantine_skills: int = 10,
        max_chars: int = _DEFAULT_MAX_CHARS,
        storage: StoragePort | None = None,
        environment: object | None = None,
    ) -> None:
        self._llm = llm
        self._messages = messages
        self._store = store
        self._loader = loader  # may be None; if set, refresh after writes
        self._max_active_skills = max_active_skills
        self._max_quarantine_skills = max_quarantine_skills
        self._max_chars = max_chars
        # S5 — optional EnvironmentManager. When set, the per-symbiote skill
        # review criteria (strict mode + extra criteria) are resolved per run
        # and folded into the evolver prompt. None → baseline prompt.
        self._environment = environment
        # Sprint 5 — optional audit sink. When provided, every spawn writes
        # a row to ``skill_review_audit`` describing the run (ok/applied/
        # skipped/ops). None disables audit silently — used in tests that
        # don't care about persistence.
        self._storage = storage
        # For tests / observability — last spawned thread.
        self._last_thread: threading.Thread | None = None
        # Per-session active thread tracker. Prevents spawn-storms when
        # ChatRunner nudges every N tool iterations and the session is
        # tool-heavy: 100 iters with nudge=10 used to fire 10 concurrent
        # daemon threads. Now spawn() returns the existing live thread
        # for a session_id instead of creating a rival.
        # The lock guards the dict itself; per-session thread joins do
        # NOT happen under it (only the dict lookup/insert).
        self._active: dict[str, threading.Thread] = {}
        self._active_lock = threading.Lock()

    # ── public API ─────────────────────────────────────────────────────

    def spawn(
        self,
        session_id: str,
        symbiote_id: str,
        *,
        limit: int = 50,
        trigger: str = "nudge",
    ) -> threading.Thread:
        """Fire a daemon thread that runs a skill review pass.

        Returns the Thread (mainly useful for tests — production callers
        usually ignore it).

        Concurrency: at most ONE live thread per session_id. If a thread
        is already running for this session (typical when ChatRunner
        nudges fire faster than the LLM responds), spawn() returns the
        in-flight thread instead of creating a rival.

        ``trigger`` is recorded in skill_review_audit so audits can tell
        mid-session nudges (``"nudge"``) apart from close_session passes
        (``"final"``). Defaults to ``"nudge"`` for back-compat.
        """
        with self._active_lock:
            existing = self._active.get(session_id)
            if existing is not None and existing.is_alive():
                return existing
            thread = threading.Thread(
                target=self._run,
                args=(session_id, symbiote_id, limit, trigger),
                daemon=True,
                name=f"skill-review-{session_id[:8]}",
            )
            self._active[session_id] = thread
        # Start outside the lock — start() itself is fast but we want the
        # critical section to stay tiny.
        thread.start()
        self._last_thread = thread
        return thread

    def active_threads(self) -> list[threading.Thread]:
        """Review threads still running (the kernel joins them on shutdown)."""
        with self._active_lock:
            return [t for t in self._active.values() if t.is_alive()]

    def spawn_final(
        self,
        session_id: str,
        symbiote_id: str,
        *,
        limit: int = 100,
    ) -> threading.Thread:
        """Variant fired by close_session — larger transcript window.

        Same semantics as ``spawn`` otherwise; trigger tagged as "final".
        """
        return self.spawn(session_id, symbiote_id, limit=limit, trigger="final")

    def run_sync(
        self,
        session_id: str,
        symbiote_id: str,
        *,
        limit: int = 50,
        trigger: str = "sync",
    ) -> dict[str, Any]:
        """Synchronous variant for tests and one-shot CLI use.

        Returns a summary dict: ``{ok, error, applied, skipped, ops}``.
        """
        return self._run(session_id, symbiote_id, limit, trigger)

    # ── internal ───────────────────────────────────────────────────────

    def _run(
        self,
        session_id: str,
        symbiote_id: str,
        limit: int,
        trigger: str = "nudge",
    ) -> dict[str, Any]:
        """Thread target. Always returns a result dict; never raises."""
        result: dict[str, Any] = {
            "ok": False,
            "error": None,
            "applied": 0,
            "skipped": 0,
            "ops": [],
        }
        if self._is_excluded(session_id, symbiote_id):
            # Not a review: no LLM call and no audit row.
            self._release(session_id)
            result["ok"] = True
            result["excluded"] = True
            return result
        try:
            messages = self._messages.get_messages(session_id, limit)
            if not messages:
                result["ok"] = True
                result["error"] = "no messages"
                return result

            template = self._resolve_review_template(symbiote_id)
            conversation = self._format_messages(messages)
            tool_digest = self._format_tool_calls(session_id)
            if tool_digest:
                conversation = f"{conversation}\n\n{tool_digest}"
            prompt = render_prompt(
                template,
                messages=conversation,
                existing_skills=self._format_existing_skills(),
            )
            response = self._llm.complete([{"role": "user", "content": prompt}])
            text = response if isinstance(response, str) else getattr(response, "content", "")

            ops = self._parse_ops(text)
            if not ops:
                result["ok"] = True
                return result

            self._apply_ops(ops, result)
            if self._loader is not None:
                try:
                    self._loader.refresh()
                except Exception as exc:
                    _log.warning("SkillsLoader.refresh() failed: %s", exc)
            result["ok"] = True
        except Exception as exc:
            _log.warning(
                "Background skill review failed (session=%s): %s",
                session_id, exc,
            )
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self._release(session_id)
            # Sprint 5 — best-effort audit row. Never blocks return, never
            # raises (audit failure logged then swallowed).
            self._write_audit(session_id, symbiote_id, trigger, result)
        return result

    def _release(self, session_id: str) -> None:
        """Release the per-session slot so the next nudge can spawn a fresh
        thread. Only removes the entry if this thread owns it — a future
        spawn may have replaced it (shouldn't happen with the is_alive()
        guard, but defend in depth)."""
        with self._active_lock:
            current = self._active.get(session_id)
            if current is threading.current_thread():
                del self._active[session_id]

    def _is_excluded(self, session_id: str, symbiote_id: str) -> bool:
        """True for the session of an end user (``subject_key``) of a
        symbiote that does not learn from those sessions
        (``learn_from_subject_sessions`` off): what one visitor did must not
        become a skill every other session gets. Needs the storage; without
        it nothing is excluded."""
        if self._storage is None:
            return False
        try:
            if subject_of_session(self._storage, session_id) is None:
                return False
            return not learns_from_subjects(self._storage, symbiote_id)
        except Exception as exc:
            # Not knowing whose session it is, do not learn from it.
            _log.warning("skill review: cannot tell the session's subject: %s", exc)
            return True

    def _write_audit(
        self,
        session_id: str,
        symbiote_id: str,
        trigger: str,
        result: dict[str, Any],
    ) -> None:
        if self._storage is None:
            return
        try:
            self._storage.execute(
                "INSERT INTO skill_review_audit "
                "(id, session_id, symbiote_id, trigger, applied, skipped, "
                "ok, error, ops_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid4()),
                    session_id,
                    symbiote_id,
                    trigger,
                    int(result.get("applied", 0)),
                    int(result.get("skipped", 0)),
                    int(bool(result.get("ok", False))),
                    result.get("error"),
                    json.dumps(result.get("ops", []), ensure_ascii=False),
                    datetime.now(tz=UTC).isoformat(),
                ),
            )
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            _log.warning("skill_review_audit write failed: %s", exc)

    def _format_messages(self, messages: list[dict]) -> str:
        """Render messages within the char budget; keep most recent."""
        lines: list[str] = []
        total = 0
        for msg in reversed(messages):
            line = f"[{msg.get('role', '?')}]: {msg.get('content', '')}"
            if total + len(line) > self._max_chars:
                break
            lines.append(line)
            total += len(line)
        lines.reverse()
        return "\n".join(lines)

    def _format_tool_calls(self, session_id: str) -> str:
        """The session's tool calls, per turn, from the persisted traces.

        Messages hold only the final answers; without the calls the reviewer
        cannot see a technique worth turning into a skill. Empty when there
        is no storage or the session ran no tools.
        """
        if self._storage is None:
            return ""
        try:
            rows = self._storage.fetch_all(
                "SELECT steps_json, stop_reason FROM execution_traces "
                "WHERE session_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, _TOOL_DIGEST_MAX_TURNS),
            )
        except Exception:  # traces are an optional enrichment
            _log.debug("background review: could not read traces", exc_info=True)
            return ""
        blocks: list[str] = []
        total = 0
        for turn_number, row in enumerate(rows, start=1):  # newest first
            try:
                steps = json.loads(row["steps_json"] or "[]")
            except (TypeError, ValueError):
                continue
            if not steps:
                continue
            lines = [f"Turn -{turn_number} (stop: {row['stop_reason'] or '?'}):"]
            for step in steps:
                params = json.dumps(
                    step.get("params") or {}, ensure_ascii=False, default=str,
                    separators=(",", ":"),
                )
                if len(params) > _TOOL_PARAMS_MAX_CHARS:
                    params = params[: _TOOL_PARAMS_MAX_CHARS - 1] + "…"
                status = "ok" if step.get("success") else "FAILED"
                lines.append(f"  - [{status}] {step.get('tool_id', '?')} {params}")
            block = "\n".join(lines)
            if total + len(block) > _TOOL_DIGEST_MAX_CHARS:
                break
            blocks.append(block)
            total += len(block)
        if not blocks:
            return ""
        blocks.reverse()  # oldest first, like the conversation
        return (
            "Tool calls executed in this session (recorded by the runtime; "
            "Turn -1 is the most recent turn that used tools):\n" + "\n".join(blocks)
        )

    def _resolve_review_template(self, symbiote_id: str) -> str:
        """Pick the review prompt template, applying per-symbiote criteria (S5).

        Falls back to the baseline ``SKILL_REVIEW_PROMPT`` when no environment
        manager is wired or the symbiote has no criteria set. Never raises —
        a criteria lookup failure must not break the review.
        """
        if self._environment is None:
            return SKILL_REVIEW_PROMPT
        getter = getattr(self._environment, "get_skill_review_criteria", None)
        if getter is None:
            return SKILL_REVIEW_PROMPT
        try:
            crit = getter(symbiote_id) or {}
        except Exception:
            return SKILL_REVIEW_PROMPT
        return build_skill_review_prompt(
            strict=bool(crit.get("strict", False)),
            extra_criteria=crit.get("extra"),
        )

    def _format_existing_skills(self) -> str:
        """List active+quarantine skills so the LLM can pick PATCH targets.

        Renders ``name :: description`` per line. Skills with status
        ``archived`` are intentionally hidden — they should not be revived
        by accident through a PATCH.

        Hard cap at ``_MAX_EXISTING_SKILLS_LISTED`` entries (default 30) so
        a library that grows to 50+ doesn't dominate the prompt. When the
        cap bites, active skills are listed first (more likely PATCH
        targets), then quarantine. A trailing "+ N more not shown" line
        signals the truncation to the LLM (and to anyone reading audit logs).
        """
        if self._loader is None:
            return "(none — skills loader not wired)"
        all_skills = self._loader.list_skills()
        active = [s for s in all_skills if s.status == "active"]
        quarantine = [s for s in all_skills if s.status == "quarantine"]
        if not active and not quarantine:
            return "(none)"

        ordered = active + quarantine
        total = len(ordered)
        truncated = ordered[:_MAX_EXISTING_SKILLS_LISTED]

        lines: list[str] = []
        for s in truncated:
            tag = "" if s.status == "active" else f" [{s.status}]"
            lines.append(f"- {s.name}{tag} :: {(s.description or '')[:120]}")
        if total > _MAX_EXISTING_SKILLS_LISTED:
            lines.append(
                f"... + {total - _MAX_EXISTING_SKILLS_LISTED} more not shown "
                f"(cap: {_MAX_EXISTING_SKILLS_LISTED}; "
                f"use 'symbiote skills list' for the full inventory)"
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_ops(text: str) -> list[dict]:
        """Parse JSON array from LLM. Tolerates markdown fences."""
        cleaned = text.strip()
        if "```" in cleaned:
            start = cleaned.find("[")
            end = cleaned.rfind("]") + 1
            if start >= 0 and end > start:
                cleaned = cleaned[start:end]
        try:
            raw = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM did not return valid JSON: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError("LLM did not return a JSON array")
        return [op for op in raw if isinstance(op, dict) and op.get("action")]

    def _apply_ops(self, ops: list[dict], result: dict[str, Any]) -> None:
        """Apply each op via SkillsStore under BACKGROUND_REVIEW provenance.

        Cap semantics (Sprint 4.1):
          - ``max_active_skills`` only bounds skills with status="active".
          - ``max_quarantine_skills`` separately bounds status="quarantine".
          - New skills land in quarantine (background review provenance),
            so a CREATE is only refused when the quarantine bucket is full.
            The active cap matters at promotion time, not creation time.

        Without the split, a library full of un-promoted quarantines would
        deadlock the loop — no new creates until a human promotes/archives.

        Errors per op are caught individually so one bad op doesn't abort
        the batch.
        """
        active_count, quarantine_count = self._count_skills_by_status()

        token = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            for op in ops:
                action = op.get("action")
                name = op.get("name", "")
                try:
                    if action == "create":
                        # New skills always land in quarantine — only that
                        # cap is relevant. The active cap protects the LLM
                        # surface and is enforced at promotion time.
                        if quarantine_count >= self._max_quarantine_skills:
                            result["skipped"] += 1
                            result["ops"].append({
                                "action": action, "name": name,
                                "ok": False,
                                "error": (
                                    f"max_quarantine_skills cap reached "
                                    f"({self._max_quarantine_skills}); "
                                    f"promote or archive existing quarantine "
                                    f"entries before creating new ones"
                                ),
                            })
                            continue
                        out = self._store.create(name, op.get("content", ""))
                        quarantine_count += 1
                    elif action in ("patch", "write_file", "edit") and not self._is_agent_skill(name):
                        # Background review only refines skills it created.
                        # A human/curated skill changed from an untrusted
                        # transcript would reach the prompt with no review.
                        result["skipped"] += 1
                        result["ops"].append({
                            "action": action, "name": name, "ok": False,
                            "error": "background review may only change agent-created skills",
                        })
                        continue
                    elif action == "patch":
                        out = self._store.patch(
                            name,
                            op.get("old_string", ""),
                            op.get("new_string", ""),
                            file_path=op.get("file_path"),
                            replace_all=bool(op.get("replace_all", False)),
                        )
                    elif action == "write_file":
                        out = self._store.write_file(
                            name,
                            op.get("file_path", ""),
                            op.get("file_content", ""),
                        )
                    elif action == "edit":
                        out = self._store.edit(name, op.get("content", ""))
                    elif action == "delete":
                        # Deletes are powerful and almost never the right
                        # answer for a background review. Refuse and log.
                        result["skipped"] += 1
                        result["ops"].append({
                            "action": action, "name": name,
                            "ok": False, "error": "delete is disabled in background review",
                        })
                        continue
                    else:
                        result["skipped"] += 1
                        result["ops"].append({
                            "action": str(action), "name": name,
                            "ok": False, "error": f"unknown action {action!r}",
                        })
                        continue

                    result["applied"] += 1
                    result["ops"].append({
                        "action": action, "name": out.name, "ok": True,
                        "message": out.message,
                    })
                except SkillError as exc:
                    result["skipped"] += 1
                    result["ops"].append({
                        "action": str(action), "name": name,
                        "ok": False, "error": str(exc),
                    })
                except Exception as exc:
                    _log.warning(
                        "Unexpected error applying op action=%s name=%s: %s",
                        action, name, exc,
                    )
                    result["skipped"] += 1
                    result["ops"].append({
                        "action": str(action), "name": name,
                        "ok": False, "error": f"{type(exc).__name__}: {exc}",
                    })
        finally:
            reset_current_write_origin(token)

    def _is_agent_skill(self, name: str) -> bool:
        find = getattr(self._store, "find_skill_dir", None)
        skill_dir = find(name) if find is not None else None
        if skill_dir is None:
            return True  # let the store report "not found"
        from symbiote.skills import usage

        meta = usage.read_meta(skill_dir) or {}
        return bool(meta.get("agent_created"))

    def _count_skills_by_status(self) -> tuple[int, int]:
        """Return (active_count, quarantine_count) for cap enforcement.

        Archived doesn't count toward either — it's invisible to the loader
        and out of circulation. Active and quarantine are tracked separately
        so a stalled promotion queue can't deadlock new creates.
        """
        counter = getattr(self._store, "count_by_status", None)
        if counter is not None:
            # From disk: skills the loader cannot parse still count.
            counts = counter()
            return (counts.get("active", 0), counts.get("quarantine", 0))
        if self._loader is None:
            return (0, 0)
        active = quarantine = 0
        for s in self._loader.list_skills():
            if s.status == "active":
                active += 1
            elif s.status == "quarantine":
                quarantine += 1
        return (active, quarantine)


__all__ = ["BackgroundReviewEngine"]
