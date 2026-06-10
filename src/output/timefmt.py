"""
Shared "Updated" timestamp for every Vector page. Displayed in US Eastern
Time (the user trades on the US session); %Z renders EDT/EST correctly
across the DST switch. Data pipelines and caches stay in UTC, this is
display-only.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def updated_at_str() -> str:
    return datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M %Z")
