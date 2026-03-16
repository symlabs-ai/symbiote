"""ExportService — export sessions, memories, and decisions as Markdown."""

from __future__ import annotations

import json
from datetime import datetime

from symbiote.core.ports import StoragePort


class ExportService:
    """Exports data from storage as formatted Markdown strings."""

    def __init__(self, storage: StoragePort) -> None:
        self._storage = storage

    # ── public API ─────────────────────────────────────────────────────

    def export_session(self, session_id: str) -> str:
        """Export a session as Markdown with header, messages, decisions, and summary."""
        session = self._storage.fetch_one(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        )

        lines: list[str] = []

        # Header
        lines.append("# Session Export")
        lines.append("")
        lines.append("## Session")
        lines.append("")
        if session:
            lines.append(f"- **ID:** {session['id']}")
            if session.get("goal"):
                lines.append(f"- **Goal:** {session['goal']}")
            lines.append(f"- **Status:** {session['status']}")
            if session.get("started_at"):
                started = datetime.fromisoformat(session["started_at"])
                lines.append(f"- **Started:** {started.strftime('%Y-%m-%d %H:%M:%S')}")
            if session.get("ended_at"):
                ended = datetime.fromisoformat(session["ended_at"])
                lines.append(f"- **Ended:** {ended.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # Messages
        messages = self._storage.fetch_all(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )

        lines.append("## Messages")
        lines.append("")
        if messages:
            for msg in messages:
                created = datetime.fromisoformat(msg["created_at"])
                ts = created.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"**{msg['role']}** ({ts}):")
                lines.append(f"> {msg['content']}")
                lines.append("")
        else:
            lines.append("No messages found.")
            lines.append("")

        # Decisions
        decisions = self._storage.fetch_all(
            "SELECT * FROM decisions WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )

        lines.append("## Decisions")
        lines.append("")
        if decisions:
            for dec in decisions:
                lines.append(f"### {dec['title']}")
                lines.append("")
                if dec.get("description"):
                    lines.append(dec["description"])
                    lines.append("")
                tags = json.loads(dec.get("tags_json") or "[]")
                if tags:
                    lines.append(f"**Tags:** {', '.join(tags)}")
                created = datetime.fromisoformat(dec["created_at"])
                lines.append(f"**Date:** {created.strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append("")
        else:
            lines.append("No decisions found.")
            lines.append("")

        # Summary
        if session and session.get("summary"):
            lines.append("## Summary")
            lines.append("")
            lines.append(session["summary"])
            lines.append("")

        return "\n".join(lines)

    def export_memory(self, symbiote_id: str) -> str:
        """Export long-term memories as Markdown, grouped by type."""
        entries = self._storage.fetch_all(
            "SELECT * FROM memory_entries "
            "WHERE symbiote_id = ? AND is_active = 1 "
            "ORDER BY type, importance DESC, created_at DESC",
            (symbiote_id,),
        )

        lines: list[str] = []
        lines.append("# Memory Export")
        lines.append("")

        if not entries:
            lines.append("No memories found.")
            lines.append("")
            return "\n".join(lines)

        # Group by type
        grouped: dict[str, list[dict]] = {}
        for entry in entries:
            mtype = entry["type"]
            grouped.setdefault(mtype, []).append(entry)

        for mtype, items in grouped.items():
            lines.append(f"## {mtype}")
            lines.append("")
            for item in items:
                lines.append(f"- **{item['content']}**")
                lines.append(f"  - Importance: {item['importance']}")
                tags = json.loads(item.get("tags_json") or "[]")
                if tags:
                    lines.append(f"  - Tags: {', '.join(tags)}")
                created = datetime.fromisoformat(item["created_at"])
                lines.append(f"  - Created: {created.strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append("")

        return "\n".join(lines)

    def export_decisions(self, session_id: str) -> str:
        """Export decisions for a session as Markdown."""
        decisions = self._storage.fetch_all(
            "SELECT * FROM decisions WHERE session_id = ? ORDER BY created_at ASC",
            (session_id,),
        )

        lines: list[str] = []
        lines.append("# Decisions Export")
        lines.append("")

        if not decisions:
            lines.append("No decisions found.")
            lines.append("")
            return "\n".join(lines)

        lines.append("## Decisions")
        lines.append("")

        for dec in decisions:
            lines.append(f"### {dec['title']}")
            lines.append("")
            if dec.get("description"):
                lines.append(dec["description"])
                lines.append("")
            tags = json.loads(dec.get("tags_json") or "[]")
            if tags:
                lines.append(f"**Tags:** {', '.join(tags)}")
            created = datetime.fromisoformat(dec["created_at"])
            lines.append(f"**Date:** {created.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append("")

        return "\n".join(lines)
