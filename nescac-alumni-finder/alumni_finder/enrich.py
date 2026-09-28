"""Work-email enrichment: Hunter.io Email Finder, then (opt-in) pattern guesses.

LinkedIn never exposes emails, so this step finds each person's address from
their name and their bank's email domain.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

import requests

from .cache import Cache
from .config import BANKS_BY_NAME, Bank
from .http import ApiError, AuthError, QuotaExceeded, RateLimiter, error_message, request_with_retries
from .models import NOTE_NOT_FOUND, NOTE_NOT_LOOKED_UP, NOTE_OPTED_OUT, NOTE_PDL_MASKED, Person
from .normalize import email_local_part, is_valid_email

log = logging.getLogger(__name__)

HUNTER_BASE = "https://api.hunter.io/v2"


@dataclass
class FinderResult:
    email: str = ""
    score: int | None = None
    verification: str = ""
    opted_out: bool = False


class HunterClient:
    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        cache: Cache | None = None,
        limiter: RateLimiter | None = None,
        max_lookups: int | None = None,
        max_retries: int = 4,
    ):
        if not api_key:
            raise AuthError("HUNTER_API_KEY is not set")
        self.api_key = api_key
        self.session = session or requests.Session()
        self.cache = cache or Cache.disabled()
        self.limiter = limiter or RateLimiter(0.25)
        self.max_lookups = max_lookups
        self.max_retries = max_retries
        self.lookups = 0  # uncached API calls made this run
        self.disabled_reason = ""

    @property
    def available(self) -> bool:
        return not self.disabled_reason

    def _get(self, endpoint: str, params: dict) -> dict | None:
        """GET an endpoint. Returns the JSON body, or None for a 451 opt-out."""
        if self.max_lookups is not None and self.lookups >= self.max_lookups:
            self.disabled_reason = f"lookup budget of {self.max_lookups} reached"
            raise QuotaExceeded(f"Hunter: {self.disabled_reason}")
        self.lookups += 1
        response = request_with_retries(
            self.session,
            "GET",
            f"{HUNTER_BASE}/{endpoint}",
            params=params,
            # Header rather than query string, so the key never appears in URLs or error messages.
            headers={"X-API-KEY": self.api_key},
            limiter=self.limiter,
            max_retries=self.max_retries,
            provider="Hunter",
        )
        status = response.status_code
        if status == 200:
            return response.json()
        if status == 451:
            # The person asked Hunter to stop processing their data.
            return None
        if status == 401:
            self.disabled_reason = "invalid API key"
            raise AuthError(f"Hunter rejected the API key: {error_message(response)}", status)
        if status in (402, 403, 429):
            # 429 that survives retries means the monthly quota is used up.
            self.disabled_reason = f"quota or rate limit ({status}: {error_message(response)})"
            raise QuotaExceeded(f"Hunter: {self.disabled_reason}", status)
        raise ApiError(f"Hunter {endpoint} failed ({status}): {error_message(response)}", status)

    def find_email(self, first_name: str, last_name: str, domain: str) -> FinderResult:
        key = {"first": first_name.lower(), "last": last_name.lower(), "domain": domain}
        cached = self.cache.get("hunter_finder", key)
        if cached is not None:
            return FinderResult(**cached)
        body = self._get("email-finder", {"domain": domain, "first_name": first_name, "last_name": last_name})
        if body is None:
            result = FinderResult(opted_out=True)
        else:
            data = body.get("data") or {}
            verification = data.get("verification") or {}
            email = data.get("email") or ""
            result = FinderResult(
                email=email if is_valid_email(email) else "",
                score=data.get("score"),
                verification=verification.get("status") or "",
            )
        self.cache.set("hunter_finder", key, result.__dict__)
        return result

    def domain_pattern(self, domain: str) -> str:
        cached = self.cache.get("hunter_pattern", domain)
        if cached is not None:
            return cached
        body = self._get("domain-search", {"domain": domain, "limit": 1}) or {}
        pattern = (body.get("data") or {}).get("pattern") or ""
        self.cache.set("hunter_pattern", domain, pattern)
        return pattern


def apply_pattern(pattern: str, first_name: str, last_name: str, domain: str) -> str:
    """'{first}.{last}' + 'Jane', "O'Neil" + 'gs.com' -> 'jane.oneil@gs.com'."""
    first = email_local_part(first_name)
    last = email_local_part(last_name)
    if not (pattern and first and last):
        return ""
    local = (
        pattern.replace("{first}", first)
        .replace("{last}", last)
        .replace("{f}", first[0])
        .replace("{l}", last[0])
    )
    if "{" in local or "}" in local:
        return ""  # a placeholder we don't understand
    email = f"{local}@{domain}"
    return email if is_valid_email(email) else ""


@dataclass
class EnrichStats:
    found: dict[str, int] = field(default_factory=dict)
    not_found: int = 0
    opted_out: int = 0
    failed: int = 0
    skipped_no_name: int = 0

    def add(self, source: str) -> None:
        self.found[source] = self.found.get(source, 0) + 1


class EmailEnricher:
    def __init__(self, hunter: HunterClient | None, guess_patterns: bool = False):
        self.hunter = hunter
        self.guess_patterns = guess_patterns
        self.stats = EnrichStats()

    def enrich(self, people: Iterable[Person]) -> EnrichStats:
        for person in people:
            if person.email or person.email_opted_out:
                continue
            bank = BANKS_BY_NAME.get(person.bank)
            if bank is None:
                continue  # a row for a bank no longer in config
            if not (person.first_name and person.last_name):
                self.stats.skipped_no_name += 1
                continue
            self._enrich_one(person, bank)
        return self.stats

    def _hunter_ready(self) -> bool:
        return self.hunter is not None and self.hunter.available

    def _enrich_one(self, person: Person, bank: Bank) -> None:
        domain = bank.lookup_domain
        tried = False
        if self._hunter_ready():
            try:
                result = self.hunter.find_email(person.first_name, person.last_name, domain)
            except (QuotaExceeded, AuthError) as exc:
                log.error("%s. Skipping Hunter lookups for the rest of this run.", exc)
            except ApiError as exc:
                log.warning("Hunter lookup failed for %s: %s", person.full_name, exc)
                self.stats.failed += 1
            else:
                tried = True
                if result.opted_out:
                    # Respect the opt-out: no lookup result and no guessed address either.
                    person.email_opted_out = True
                    person.email_note = NOTE_OPTED_OUT
                    self.stats.opted_out += 1
                    return
                if result.email:
                    confidence = str(result.score) if result.score is not None else ""
                    if result.verification:
                        confidence = f"{confidence} ({result.verification})".strip()
                    person.set_email(result.email, "hunter", confidence)
                    self.stats.add("hunter")
                    return

        if self.guess_patterns:
            pattern = bank.email_pattern or ""
            if not pattern and self._hunter_ready():
                try:
                    pattern = self.hunter.domain_pattern(domain)
                except (QuotaExceeded, AuthError) as exc:
                    log.error("%s. Skipping Hunter lookups for the rest of this run.", exc)
                except ApiError as exc:
                    log.warning("Hunter pattern lookup failed for %s: %s", domain, exc)
            tried = tried or bool(pattern)
            email = apply_pattern(pattern, person.first_name, person.last_name, domain)
            if email:
                person.set_email(email, "pattern", f"unverified: {pattern}")
                self.stats.add("pattern")
                return

        if person.email_note != NOTE_PDL_MASKED:  # that note is more useful than "not found"
            person.email_note = NOTE_NOT_FOUND if tried else NOTE_NOT_LOOKED_UP
        if tried:
            self.stats.not_found += 1
