"""SemanticRecallProvider — keyword-based memory recall with scoring.

MVP implementation that tokenizes queries into keywords and scores
memory entries by keyword overlap, importance, and recency.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from symbiote.core.models import MemoryEntry
from symbiote.core.ports import StoragePort

# Common English stop words to filter out
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "must", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "about",
    "that", "this", "it", "its", "and", "or", "but", "not", "no", "so",
    "if", "then", "than", "too", "very", "just", "how", "what", "which",
    "who", "whom", "when", "where", "why", "all", "each", "every", "both",
    "few", "more", "most", "other", "some", "such", "only", "own", "same",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
})

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def tokenize(text: str) -> set[str]:
    """Extract meaningful keywords from text, filtering stop words."""
    words = _WORD_RE.findall(text.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 1}


def score_entry(
    entry_keywords: set[str],
    query_keywords: set[str],
    importance: float,
    recency_weight: float,
) -> float:
    """Score a memory entry against query keywords.

    Score = (overlap_ratio * 0.5) + (importance * 0.3) + (recency * 0.2)
    """
    if not query_keywords:
        return importance

    overlap = len(entry_keywords & query_keywords)
    overlap_ratio = overlap / len(query_keywords)

    return (overlap_ratio * 0.5) + (importance * 0.3) + (recency_weight * 0.2)


class SemanticRecallProvider:
    """Keyword-based semantic recall for memory entries.

    Improves on basic SQL LIKE by:
    1. Tokenizing query into meaningful keywords
    2. Scoring entries by keyword overlap ratio
    3. Weighting by importance and recency
    4. Matching across tags as well as content
    """

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    def recall(
        self,
        query: str,
        session_id: str | None = None,
        limit: int = 5,
        scope: str | None = None,
    ) -> list[MemoryEntry]:
        """Recall relevant memories using keyword-based scoring.

        Returns entries sorted by relevance score (highest first).
        """
        query_keywords = tokenize(query)
        if not query_keywords:
            return []

        # Fetch candidate entries (broader than final result)
        candidates = self._fetch_candidates(
            query_keywords, session_id, scope, fetch_limit=limit * 5
        )

        # Score and rank
        now = datetime.now(tz=UTC)
        scored: list[tuple[float, MemoryEntry]] = []

        for entry in candidates:
            entry_keywords = tokenize(entry.content)
            # Also include tags as keywords
            for tag in entry.tags:
                entry_keywords |= tokenize(tag)

            recency_days = (now - entry.last_used_at).total_seconds() / 86400
            recency_weight = max(0.0, 1.0 - (recency_days / 30))

            # Require at least one keyword overlap
            if not (entry_keywords & query_keywords):
                continue
            s = score_entry(entry_keywords, query_keywords, entry.importance, recency_weight)
            if s > 0.1:  # minimum threshold
                scored.append((s, entry))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Update last_used_at for returned entries
        result = [entry for _, entry in scored[:limit]]
        now_iso = now.isoformat()
        for entry in result:
            self._storage.execute(
                "UPDATE memory_entries SET last_used_at = ? WHERE id = ?",
                (now_iso, entry.id),
            )

        return result

    def _fetch_candidates(
        self,
        keywords: set[str],
        session_id: str | None,
        scope: str | None,
        fetch_limit: int,
    ) -> list[MemoryEntry]:
        """Fetch candidate entries using OR-based keyword matching."""
        from symbiote.memory.store import MemoryStore

        # Build OR conditions for each keyword
        conditions = ["is_active = 1"]
        params: list = []

        # Match any keyword in content or tags
        keyword_clauses = []
        for kw in list(keywords)[:10]:  # cap at 10 keywords to avoid huge queries
            # Escape LIKE metacharacters to prevent false positives
            safe_kw = kw.replace("%", r"\%").replace("_", r"\_")
            keyword_clauses.append(
                "(content LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{safe_kw}%", f"%{safe_kw}%"])

        if keyword_clauses:
            conditions.append(f"({' OR '.join(keyword_clauses)})")

        if session_id:
            # Include session-specific AND global entries
            conditions.append("(session_id = ? OR session_id IS NULL)")
            params.append(session_id)

        if scope:
            conditions.append("scope = ?")
            params.append(scope)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM memory_entries WHERE {where} "
            f"ORDER BY importance DESC LIMIT ?"
        )
        params.append(fetch_limit)

        rows = self._storage.fetch_all(sql, tuple(params))
        return [MemoryStore._row_to_entry(r) for r in rows]
