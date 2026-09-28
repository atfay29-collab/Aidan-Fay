"""People Data Labs Person Search API source.

Runs one Elasticsearch query per school:

    (attended <school>) AND (current employer is one of the target banks)

Results are paged with PDL's scroll token. Each record returned costs one
credit, so every record bought is cached (see cache.py) and replayed on later
runs; a run only pays for records past the ones it already has.
"""
from __future__ import annotations

import logging
from typing import Iterator, Sequence

import requests

from ..cache import Cache
from ..config import BANKS, SCHOOLS, Bank, School, match_school_entry, resolve_bank
from ..groups import classify
from ..http import ApiError, AuthError, QuotaExceeded, RateLimiter, error_message, request_with_retries
from ..models import NOTE_PDL_MASKED, Person
from ..normalize import is_valid_email, normalize_domain

log = logging.getLogger(__name__)

PDL_SEARCH_URL = "https://api.peopledatalabs.com/v5/person/search"
# Fixed page size (PDL allows up to 100). Keeping it constant means a later run
# with a bigger --max-per-school replays the pages already bought from the
# cache instead of re-requesting them at a different size and paying again.
PAGE_SIZE = 50
# Records already bought are kept at least this long, so a search that runs
# out of free credits can resume next month without re-buying them.
PROGRESS_TTL_SECONDS = 180 * 86400


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
    "headline", "job_summary",
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
    person.division, person.group = classify(
        person.title,
        _text(record.get("headline")),
        _text(record.get("job_summary")),
        bank_category=bank.category,
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

    def _post(self, body: dict) -> dict:
        """One Person Search request. Returns {data, total, scroll_token}."""
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
        return {
            "data": [_slim(r) for r in page.get("data") or []],
            "total": page.get("total", 0),
            "scroll_token": page.get("scroll_token"),
        }

    def _track_credits(self, response: requests.Response) -> None:
        spent = response.headers.get("x-call-credits-spent")
        if spent and spent.isdigit():
            self.credits_spent += int(spent)
        remaining = response.headers.get("x-totallimit-remaining")
        if remaining:
            self.credits_remaining = remaining

    # Every record bought for a school's query is kept, in order, under one
    # cache entry keyed by the query alone (not the page size). A later run
    # replays them and only pays for records beyond what it already has,
    # whatever --max-per-school was before.

    def _load_progress(self, query: dict) -> dict:
        ttl = max(self.cache.ttl_seconds, PROGRESS_TTL_SECONDS)
        state = self.cache.get("pdl_progress", query, ttl_seconds=ttl)
        if state is None:
            state = self._progress_from_old_cache(query) or {
                "total": None,
                "records": [],
                "scroll_token": None,
                "done": False,
            }
        return state

    def _progress_from_old_cache(self, query: dict) -> dict | None:
        """Pick up a first page cached by earlier versions, which keyed pages by size."""
        for size in range(PAGE_SIZE, 1, -1):  # size 1 is the estimate command's probe
            page = self.cache.get("pdl_search", _request_body(query, size))
            if page is not None:
                records = list(page.get("data") or [])
                total = int(page.get("total") or 0)
                token = page.get("scroll_token")
                return {
                    "total": total,
                    "records": records,
                    "scroll_token": token,
                    "done": not records or not token or len(records) >= total,
                }
        return None

    def _fetch_more(self, query: dict, size: int, state: dict) -> dict:
        offset = len(state["records"])
        token = state["scroll_token"]
        if token:
            try:
                page = self._post_sized(_request_body(query, size, token))
            except (AuthError, QuotaExceeded):
                raise
            except ApiError as exc:
                if exc.status != 400:
                    raise
                page = None  # PDL rejected the token
            short_of_total = state["total"] is not None and offset < state["total"]
            if page is not None and (page["data"] or not short_of_total):
                return page
            # Scroll tokens expire (PDL may reject them or just return nothing).
            # Resume by position instead of re-buying the records already cached.
            log.warning("PDL scroll token expired; resuming at record %d by offset", offset)
        body = _request_body(query, size)
        if offset:
            body["from"] = offset
        return self._post_sized(body)

    def _post_sized(self, body: dict) -> dict:
        """POST; if PDL refuses a full page for lack of credits, retry once with what it says is left."""
        try:
            return self._post(body)
        except QuotaExceeded as exc:
            left = self._credits_left()
            if exc.status != 402 or not left or left >= body["size"]:
                raise
            log.info("PDL reports %d credit(s) left; trying a request that size", left)
            return self._post({**body, "size": left})

    def _credits_left(self) -> int | None:
        value = self.credits_remaining
        return int(value) if value and value.isdigit() else None

    def count(self, school: School, banks: Sequence[Bank]) -> int:
        """Total matches for one school. Free if this school was searched before, else at most one credit."""
        query = build_query(school, banks)
        state = self._load_progress(query)
        if state["total"] is not None:
            return int(state["total"])
        body = _request_body(query, size=1)
        page = self.cache.get("pdl_search", body)
        if page is None:
            page = self._post(body)
            self.cache.set("pdl_search", body, page)
        return int(page.get("total") or 0)

    def search(
        self,
        school: School,
        banks: Sequence[Bank],
        max_records: int | None = None,
    ) -> Iterator[Person]:
        query = build_query(school, banks)
        state = self._load_progress(query)
        cap = max_records if max_records is not None else float("inf")
        announced = False

        def announce():
            nonlocal announced
            if not announced and state["total"] is not None:
                log.info("PDL: %s -> %d matching profiles", school.name, state["total"])
                announced = True

        announce()
        used = 0
        for record in state["records"]:  # already paid for
            if used >= cap:
                return
            used += 1
            person = self._parse(record, banks, school)
            if person:
                yield person

        while used < cap and not state["done"]:
            # PDL's "remaining" header doesn't track search credits reliably,
            # so don't stop on it up front; PDL answers 402 when truly out.
            size = int(min(PAGE_SIZE, cap - used))
            page = self._fetch_more(query, size, state)
            records = page["data"]
            if state["total"] is None:
                state["total"] = int(page.get("total") or 0)
            state["records"].extend(records)
            state["scroll_token"] = page.get("scroll_token")
            state["done"] = not records or not state["scroll_token"] or len(state["records"]) >= state["total"]
            self.cache.set("pdl_progress", query, state)
            announce()
            for record in records:
                used += 1
                person = self._parse(record, banks, school)
                if person:
                    yield person

    @staticmethod
    def _parse(record: dict, banks: Sequence[Bank], school: School) -> Person | None:
        try:
            return parse_person(record, banks, school_hint=school)
        except Exception as exc:  # one malformed record must not sink the whole run
            log.warning("Skipping PDL record %s that couldn't be read: %r", record.get("id"), exc)
            return None
