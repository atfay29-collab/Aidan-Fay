"""Links into LinkedIn's alumni tool, one per school x bank keyword.

These are ordinary URLs for you to open by hand while logged in to LinkedIn.
The tool never requests them itself: LinkedIn's User Agreement bans bots and
automated access even on your own account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from urllib.parse import quote

from .config import Bank, School


@dataclass(frozen=True)
class SearchLink:
    school: str
    bank: str
    keyword: str
    url: str


def alumni_search_url(school: School, keyword: str) -> str:
    return f"https://www.linkedin.com/school/{school.linkedin_slug}/people/?keywords={quote(keyword)}"


def build_links(schools: Sequence[School], banks: Sequence[Bank]) -> list[SearchLink]:
    return [
        SearchLink(school.name, bank.name, keyword, alumni_search_url(school, keyword))
        for school in schools
        for bank in banks
        for keyword in bank.linkedin_keywords
    ]
