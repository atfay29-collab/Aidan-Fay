"""People Data Labs Person Search API source.

Runs one Elasticsearch query per school:

    (attended <school>) AND (current employer is one of the target banks)

Results are paged with PDL's scroll token. Each page costs one credit per
record returned, so every page is cached (see cache.py) and replayed on later
runs instead of being bought again.
"""
from __future__ import annotations

import logging
from typing import Iterator, Sequence

import requests

from ..cache import Cache
from ..config import BANKS, SCHOOLS, Bank, School, match_school_entry, resolve_bank
from ..http import ApiError, AuthError, QuotaExceeded, RateLimiter, error_message, request_with_retries
from ..models import NOTE_PDL_MASKED, Person
from ..normalize import is_valid_email, normalize_domain

log = logging.getLogger(__name__)

PDL_SEARCH_URL = "https://api.peopledatalabs.com/v5/person/search"
# Fixed page size (PDL allows up to 100). Keeping it constant means a later run
# with a bigger --max-per-school replays the pages already bought from the
# cache instead of re-requesting them at a different size and paying again.
PAGE_SIZE = 50


def build_query(school: School, banks: Sequence[Bank]) -> dict:
    school_clauses = []
    if school.pdl_names:
        school_clauses.append({"terms": {"education.school.name": list(school.pdl_names)}})
    if school.domains:
        school_clauses.append({"terms": {"education.school.website": list(school.domains)}})
    company_names = sorted({n for bank in banks for n in bank.pdl_names})
    company_domains = sorted({d for bank in banks for d in bank.domains})
    company_clauses = [
        {"terms": {"job_company_name": company_names}},
        {"terms": {"job_company_website": company_domains}},
    ]
    # A bool with only `should` clauses requires at least one of them to match,
    # so this reads: (any school clause) AND (any company clause).
    return {
        "query": {
            "bool": {
                "must": [
                    {"bool": {"should": school_clauses}},
                    {"bool": {"should": company_clauses}},
                ]
            }
        }
    }


def _request_body(query: dict, size: int, scroll_token: str | None = None) -> dict:
    # The `query` parameter holds a full Elasticsearch request body, which
    # itself has a top-level "query" key (same shape the official SDK sends).
    body = {"query": query, "size": size, "titlecase": True}
    if scroll_token:
        body["scroll_token"] = scroll_token
    return body


_KEPT_FIELDS = (
    "id", "full_name", "first_name", "last_name", "job_title", "job_company_name",
    "job_company_website", "linkedin_url", "location_name", "work_email", "emails",
)


def _slim(record: dict) -> dict:
    """Keep only the fields this tool uses, so the local cache doesn't hold
    personal emails, phone numbers or full work histories."""
    slim = {k: record[k] for k in _KEPT_FIELDS if k in record}
    if isinstance(slim.get("emails"), list):
        slim["emails"] = [e for e in slim["emails"] if not (isinstance(e, dict) and e.get("type") == "personal")]
    slim["education"] = [
        {
            "school": {k: (e.get("school") or {}).get(k) for k in ("name", "website", "domain")},
            "degrees": e.get("degrees") or [],
            "end_date": e.get("end_date"),
        }
        for e in _list(record.get("education"))
        if isinstance(e, dict)
    ]
    return slim


def _list(value) -> list:
    # Free-plan records can carry `true` where a list of contact entries would be.
    return value if isinstance(value, list) else []


def _text(value) -> str:
    # On PDL's free plan, contact and granular-location fields come back as
    # true/false ("we have this") instead of the value. Treat those as empty.
    return value.strip() if isinstance(value, str) else ""


def _education_sort_key(entry: dict) -> tuple:
    degrees = " ".join(d for d in (entry.get("degrees") or []) if isinstance(d, str)).lower()
    is_bachelors = "bachelor" in degrees
    end = _text(entry.get("end_date")) or "9999"
    return (not is_bachelors, end)


def _work_email(record: dict, bank: Bank) -> tuple[str, bool]:
    """Return (email at the bank's domain or '', whether PDL has a masked email)."""
    allowed = set(bank.email_domains) | set(bank.domains)
    masked = record.get("work_email") is True or record.get("emails") is True
    candidates = [_text(record.get("work_email"))]
    for item in _list(record.get("emails")):
        if isinstance(item, dict):
            masked = masked or item.get("address") is True
            candidates.append(_text(item.get("address")))
    for email in candidates:
        # A work email at a previous employer is useless for outreach.
        if is_valid_email(email) and normalize_domain(email) in allowed:
            return email.lower(), masked
    return "", masked


def parse_person(
    record: dict,
    banks: Sequence[Bank] = BANKS,
    schools: Sequence[School] = SCHOOLS,
    school_hint: School | None = None,
) -> Person | None:
    """Convert one PDL person record to a Person, or None if it isn't in scope."""
    bank = resolve_bank(
        _text(record.get("job_company_name")),
        _text(record.get("job_company_website")),
        tuple(banks),
    )
    if bank is None:
        log.debug("Skipping %s: employer %r is not a target bank", record.get("id"), record.get("job_company_name"))
        return None

    matches: list[tuple[School, dict]] = []
    for entry in _list(record.get("education")):
        if not isinstance(entry, dict):
            continue
        school = match_school_entry(entry.get("school") or {}, tuple(schools))
        if school:
            matches.append((school, entry))
    matches.sort(key=lambda m: _education_sort_key(m[1]))
    ordered: list[str] = []
    for school, _ in matches:
        if school.name not in ordered:
            ordered.append(school.name)
    if not ordered:
        if school_hint is None:
            return None
        ordered = [school_hint.name]
    grad_year = ""
    for school, entry in matches:
        if school.name == ordered[0]:
            grad_year = _text(entry.get("end_date"))[:4]
            break

    first = _text(record.get("first_name"))
    last = _text(record.get("last_name"))
    full = _text(record.get("full_name")) or " ".join(p for p in (first, last) if p)
    person = Person(
        full_name=full,
        first_name=first,
        last_name=last,
        bank=bank.name,
        schools=ordered,
        title=_text(record.get("job_title")),
        linkedin_url=_text(record.get("linkedin_url")),
        location=_text(record.get("location_name")),
        grad_year=grad_year,
        pdl_id=_text(record.get("id")),
        sources={"pdl"},
    )
    email, masked = _work_email(record, bank)
    if email:
        person.set_email(email, "pdl", "on file")
    elif masked:
        person.email_note = NOTE_PDL_MASKED
    return person


class PDLSource:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        cache: Cache | None = None,
        limiter: RateLimiter | None = None,
        max_retries: int = 5,
    ):
        if not api_key:
            raise AuthError("PDL_API_KEY is not set")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.cache = cache or Cache.disabled()
        self.limiter = limiter or RateLimiter(6.5)
        self.max_retries = max_retries
        self.credits_spent = 0
        self.credits_remaining: str | None = None

    def _fetch_page(self, body: dict) -> dict:
        cached = self.cache.get("pdl_search", body)
        if cached is not None:
            return cached
        response = request_with_retries(
            self.session,
            "POST",
            PDL_SEARCH_URL,
            json=body,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
            limiter=self.limiter,
            max_retries=self.max_retries,
            provider="PDL",
        )
        self._track_credits(response)
        status = response.status_code
        if status == 200:
            page = response.json()
        elif status == 404:
            # PDL answers 404 when nothing (or nothing more) matches.
            page = {"data": [], "total": 0, "scroll_token": None}
        elif status in (401, 403):
            raise AuthError(f"PDL rejected the API key ({status}): {error_message(response)}", status)
        elif status == 402:
            raise QuotaExceeded(f"PDL: out of credits ({error_message(response)})", status)
        elif status == 429:
            raise QuotaExceeded("PDL: still rate limited after retries; try again later", status)
        else:
            raise ApiError(f"PDL search failed ({status}): {error_message(response)}", status)
        page = {
            "data": [_slim(r) for r in page.get("data") or []],
            "total": page.get("total", 0),
            "scroll_token": page.get("scroll_token"),
        }
        self.cache.set("pdl_search", body, page)
        return page

    def _track_credits(self, response: requests.Response) -> None:
        spent = response.headers.get("x-call-credits-spent")
        if spent and spent.isdigit():
            self.credits_spent += int(spent)
        remaining = response.headers.get("x-totallimit-remaining")
        if remaining:
            self.credits_remaining = remaining

    def count(self, school: School, banks: Sequence[Bank]) -> int:
        """Total matches for one school. Costs at most one credit."""
        page = self._fetch_page(_request_body(build_query(school, banks), size=1))
        return int(page.get("total") or 0)

    def search(
        self,
        school: School,
        banks: Sequence[Bank],
        max_records: int | None = None,
    ) -> Iterator[Person]:
        query = build_query(school, banks)
        fetched = 0
        total = None
        scroll_token = None
        while True:
            size = PAGE_SIZE if max_records is None else min(PAGE_SIZE, max_records - fetched)
            if size <= 0:
                break
            page = self._fetch_page(_request_body(query, size, scroll_token))
            records = page["data"]
            if total is None:
                total = int(page.get("total") or 0)
                log.info("PDL: %s -> %d matching profiles", school.name, total)
            for record in records:
                try:
                    person = parse_person(record, banks, school_hint=school)
                except Exception as exc:  # one malformed record must not sink the whole run
                    log.warning("Skipping PDL record %s that couldn't be read: %r", record.get("id"), exc)
                    continue
                if person:
                    yield person
            fetched += len(records)
            scroll_token = page.get("scroll_token")
            if not records or not scroll_token or fetched >= total:
                break
