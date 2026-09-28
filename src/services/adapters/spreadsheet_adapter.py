"""Spreadsheet progress adapter with an injected read-only transport."""

from __future__ import annotations

from typing import Any


class SpreadsheetProgressAdapter:
    """Normalise spreadsheet rows returned by an injected read transport."""

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
            raise RuntimeError("Spreadsheet read transport is not wired")
        rows = self._transport.read_rows(
            project_id=project_id,
            period_start_utc=period_start_utc,
            period_end_utc=period_end_utc,
            base_url=base_url,
            credential=credential,
        )
        return [self._normalise(row) for row in rows]

    @staticmethod
    def _normalise(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": str(row.get("source_id", row.get("row_id", ""))),
            "locator": str(row.get("locator", "")),
            "fetched_at": row.get("fetched_at", ""),
            "text": row.get("note", ""),
            "events": row.get("events", []),
            "open_at_period_start": row.get("open_at_period_start"),
        }
