"""GitLab issue adapter with an injected read-only transport."""

from __future__ import annotations

from typing import Any


class GitLabIssueAdapter:
    """Normalise issue rows returned by an injected GitLab read transport."""

    def __init__(self, transport: Any | None = None) -> None:
        self._transport = transport

    def fetch(
        self,
        *,
        project_id: str,
        period_start_utc: str,
        period_end_utc: str,
        base_url: str,
        credential: str,
    ) -> list[dict[str, Any]]:
        if self._transport is None:
            raise RuntimeError("GitLab read transport is not wired")
        rows = self._transport.read_issues(
            project_id=project_id,
            period_start_utc=period_start_utc,
            period_end_utc=period_end_utc,
            base_url=base_url,
            credential=credential,
        )
        return [self._normalise(row) for row in rows]

    @staticmethod
    def _normalise(row: dict[str, Any]) -> dict[str, Any]:
        text_parts = [row.get("title", ""), row.get("description", "")]
        text_parts.extend(note.get("body", "") for note in row.get("notes", []) if isinstance(note, dict))
        return {
            "source_id": str(row.get("source_id", row.get("id", ""))),
            "locator": str(row.get("locator", row.get("web_url", ""))),
            "fetched_at": row.get("fetched_at", ""),
            "text": "\n".join(part for part in text_parts if isinstance(part, str) and part),
            "events": row.get("events", []),
            "open_at_period_start": row.get("open_at_period_start"),
        }
