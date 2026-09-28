"""Read-only source adapter implementations."""

from src.services.adapters.gitlab_issue_adapter import GitLabIssueAdapter
from src.services.adapters.spreadsheet_adapter import SpreadsheetProgressAdapter

__all__ = ["GitLabIssueAdapter", "SpreadsheetProgressAdapter"]
