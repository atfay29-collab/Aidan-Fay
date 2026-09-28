"""Search scope (schools and banks) and runtime settings.

Everything a user is likely to tweak lives here: which schools and banks are
in scope, the company/school names People Data Labs uses for them, the email
domains used for enrichment, and the LinkedIn slugs used to build the manual
alumni-search links.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .normalize import normalize_domain, norm_text

BULGE_BRACKET = "Bulge Bracket"
ELITE_BOUTIQUE = "Elite Boutique"


@dataclass(frozen=True)
class School:
    name: str  # display name written to the sheet, e.g. "Colby"
    full_name: str  # "Colby College"
    # Canonical lowercase names as they appear in PDL's education.school.name.
    # Leave empty when the name is ambiguous and match on domain only.
    pdl_names: tuple[str, ...]
    domains: tuple[str, ...]  # school website domains (education.school.website)
    linkedin_slug: str  # linkedin.com/school/<slug>/people/
    aliases: tuple[str, ...] = ()  # extra spellings accepted in the manual CSV


@dataclass(frozen=True)
class Bank:
    name: str  # display name written to the sheet
    category: str  # BULGE_BRACKET or ELITE_BOUTIQUE
    # Lowercase company names as they appear in PDL's job_company_name.
    pdl_names: tuple[str, ...]
    domains: tuple[str, ...]  # company website domains (job_company_website)
    # Work-email domains. The first one is used for Hunter/pattern lookups;
    # the rest are accepted when validating emails that came from a data source.
    email_domains: tuple[str, ...]
    # Keywords used for LinkedIn's alumni search (one link per keyword).
    linkedin_keywords: tuple[str, ...]
    # Optional known address pattern, e.g. "{first}.{last}". Used only with
    # --guess-emails; when unset, the pattern is looked up via Hunter.
    email_pattern: str | None = None
    aliases: tuple[str, ...] = ()  # extra spellings accepted in the manual CSV

    @property
    def lookup_domain(self) -> str:
        return self.email_domains[0]


SCHOOLS: tuple[School, ...] = (
    School("Amherst", "Amherst College", ("amherst college",), ("amherst.edu",), "amherst-college"),
    School("Bates", "Bates College", ("bates college",), ("bates.edu",), "bates-college"),
    School("Bowdoin", "Bowdoin College", ("bowdoin college",), ("bowdoin.edu",), "bowdoin-college"),
    School("Colby", "Colby College", ("colby college",), ("colby.edu",), "colby-college"),
    School(
        "Connecticut College",
        "Connecticut College",
        ("connecticut college",),
        ("conncoll.edu",),
        "connecticut-college",
        aliases=("conn college", "conncoll"),
    ),
    School("Hamilton", "Hamilton College", ("hamilton college",), ("hamilton.edu",), "hamilton-college"),
    School("Middlebury", "Middlebury College", ("middlebury college",), ("middlebury.edu",), "middlebury-college"),
    # "trinity college" also names Trinity College Dublin, Trinity College
    # Cambridge, etc., so Trinity (Hartford) is matched on its domain only.
    School(
        "Trinity",
        "Trinity College (Hartford)",
        (),
        ("trincoll.edu",),
        "trinity-college-hartford",
        aliases=("trinity college", "trinity college hartford", "trinity college-hartford", "trinity ct"),
    ),
    School("Tufts", "Tufts University", ("tufts university",), ("tufts.edu",), "tufts-university"),
    School("Wesleyan", "Wesleyan University", ("wesleyan university",), ("wesleyan.edu",), "wesleyan-university"),
    School("Williams", "Williams College", ("williams college",), ("williams.edu",), "williams-college"),
)

BANKS: tuple[Bank, ...] = (
    # --- Bulge bracket ---
    Bank(
        "Goldman Sachs",
        BULGE_BRACKET,
        ("goldman sachs", "goldman sachs & co. llc", "goldman sachs asset management"),
        ("goldmansachs.com", "gs.com"),
        ("gs.com", "goldmansachs.com"),
        ("Goldman Sachs",),
        aliases=("gs", "goldman"),
    ),
    Bank(
        "Morgan Stanley",
        BULGE_BRACKET,
        ("morgan stanley",),
        ("morganstanley.com", "ms.com"),
        ("morganstanley.com", "ms.com"),
        ("Morgan Stanley",),
        aliases=("ms",),
    ),
    Bank(
        "J.P. Morgan",
        BULGE_BRACKET,
        ("jpmorgan chase & co.", "j.p. morgan", "jpmorgan chase", "jp morgan", "jpmorgan", "j.p. morgan chase & co."),
        ("jpmorganchase.com", "jpmorgan.com"),
        ("jpmorgan.com", "jpmchase.com", "chase.com"),
        ("J.P. Morgan",),
        aliases=("jpm", "jpmorgan chase", "jp morgan chase"),
    ),
    Bank(
        "Bank of America",
        BULGE_BRACKET,
        ("bank of america", "bofa securities", "bank of america merrill lynch"),
        ("bankofamerica.com", "bofa.com", "bofasecurities.com"),
        ("bofa.com", "bankofamerica.com", "baml.com"),
        ("Bank of America",),
        aliases=("bofa", "baml", "bank of america securities"),
    ),
    Bank(
        "Citi",
        BULGE_BRACKET,
        ("citi", "citigroup"),
        ("citi.com", "citigroup.com"),
        ("citi.com",),
        ("Citi",),
        aliases=("citigroup", "citibank"),
    ),
    Bank(
        "Barclays",
        BULGE_BRACKET,
        ("barclays", "barclays investment bank"),
        ("barclays.com", "barclays.co.uk", "home.barclays"),
        ("barclays.com",),
        ("Barclays",),
    ),
    Bank(
        "UBS / Credit Suisse",
        BULGE_BRACKET,
        ("ubs", "credit suisse"),
        ("ubs.com", "credit-suisse.com"),
        ("ubs.com", "credit-suisse.com"),
        ("UBS", "Credit Suisse"),
        aliases=("ubs", "credit suisse", "cs"),
    ),
    # --- Elite boutiques ---
    Bank("Evercore", ELITE_BOUTIQUE, ("evercore",), ("evercore.com",), ("evercore.com",), ("Evercore",)),
    Bank("Lazard", ELITE_BOUTIQUE, ("lazard",), ("lazard.com",), ("lazard.com",), ("Lazard",)),
    Bank(
        "Centerview Partners",
        ELITE_BOUTIQUE,
        ("centerview partners",),
        ("centerview.com",),
        ("centerview.com",),
        ("Centerview Partners",),
        aliases=("centerview",),
    ),
    Bank(
        "Moelis & Company",
        ELITE_BOUTIQUE,
        ("moelis & company",),
        ("moelis.com",),
        ("moelis.com",),
        ("Moelis",),
        aliases=("moelis", "moelis & co"),
    ),
    Bank(
        "PJT Partners",
        ELITE_BOUTIQUE,
        ("pjt partners",),
        ("pjtpartners.com",),
        ("pjtpartners.com",),
        ("PJT Partners",),
        aliases=("pjt",),
    ),
    Bank(
        "Perella Weinberg Partners",
        ELITE_BOUTIQUE,
        ("perella weinberg partners",),
        ("pwpartners.com",),
        ("pwpartners.com",),
        ("Perella Weinberg",),
        aliases=("pwp", "perella weinberg", "perella"),
    ),
    Bank(
        "Houlihan Lokey",
        ELITE_BOUTIQUE,
        ("houlihan lokey",),
        ("hl.com",),
        ("hl.com",),
        ("Houlihan Lokey",),
        aliases=("hl", "houlihan"),
    ),
    Bank(
        "Qatalyst Partners",
        ELITE_BOUTIQUE,
        ("qatalyst partners",),
        ("qatalyst.com",),
        ("qatalyst.com",),
        ("Qatalyst",),
        aliases=("qatalyst",),
    ),
)

SCHOOLS_BY_NAME = {s.name: s for s in SCHOOLS}
BANKS_BY_NAME = {b.name: b for b in BANKS}


def _school_labels(school: School) -> set[str]:
    labels = {school.name, school.full_name, *school.pdl_names, *school.aliases}
    return {norm_text(label) for label in labels}


def _bank_labels(bank: Bank) -> set[str]:
    labels = {bank.name, *bank.pdl_names, *bank.aliases, *bank.linkedin_keywords}
    return {norm_text(label) for label in labels}


def resolve_school(text: str, schools: tuple[School, ...] = SCHOOLS) -> School | None:
    """Map free text (a display name, full name, alias or domain) to a School."""
    key = norm_text(text)
    domain = normalize_domain(text)
    for school in schools:
        if key in _school_labels(school) or domain in school.domains:
            return school
    return None


def match_school_entry(school_obj: dict, schools: tuple[School, ...] = SCHOOLS) -> School | None:
    """Match a PDL ``education[].school`` object against the configured schools."""
    domains = {normalize_domain(school_obj.get(k) or "") for k in ("website", "domain")}
    domains.discard("")
    name = norm_text(school_obj.get("name") or "")
    for school in schools:
        if domains & set(school.domains):
            return school
        if name and name in {norm_text(n) for n in school.pdl_names}:
            return school
    return None


def resolve_bank(
    company_name: str = "",
    website: str = "",
    banks: tuple[Bank, ...] = BANKS,
) -> Bank | None:
    """Map a company name and/or website to one of the target banks.

    Website domain wins; then an exact name match; then a prefix match so that
    divisions like "Goldman Sachs Asset Management" roll up to the parent.
    """
    domain = normalize_domain(website)
    if domain:
        for bank in banks:
            if any(domain == d or domain.endswith("." + d) for d in bank.domains):
                return bank
    name = norm_text(company_name)
    if not name:
        return None
    for bank in banks:
        if name in _bank_labels(bank):
            return bank
    for bank in banks:
        # Divisions roll up to the parent ("goldman sachs asset management").
        # Only canonical names are used here, not short aliases, so "centerview"
        # can't pull in Centerview Capital; the trailing space keeps "citi" from
        # matching "citizens bank".
        canonical = {norm_text(n) for n in (bank.name, *bank.pdl_names)}
        if any(name.startswith(label + " ") for label in canonical):
            return bank
    return None


def select(items: tuple, wanted: list[str] | None, resolver) -> tuple:
    """Filter SCHOOLS/BANKS by user-supplied names (CLI --schools/--banks)."""
    if not wanted:
        return items
    chosen = []
    for text in wanted:
        item = resolver(text)
        if item is None:
            raise ValueError(f"Unknown name: {text!r}")
        if item not in chosen:
            chosen.append(item)
    # Keep the canonical order from config.
    return tuple(i for i in items if i in chosen)


@dataclass(frozen=True)
class Settings:
    pdl_api_key: str
    hunter_api_key: str
    google_auth_mode: str  # "service_account" or "oauth"
    google_service_account_file: Path
    google_oauth_client_file: Path
    google_oauth_token_file: Path
    spreadsheet_id: str
    spreadsheet_title: str
    cache_path: Path
    cache_ttl_days: float
    pdl_min_interval: float
    hunter_min_interval: float

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            from dotenv import find_dotenv, load_dotenv

            # Only the .env in the directory you run from; real env vars win.
            load_dotenv(find_dotenv(usecwd=True))
        except ImportError:  # python-dotenv is optional
            pass
        env = os.environ.get
        return cls(
            pdl_api_key=env("PDL_API_KEY", "").strip(),
            hunter_api_key=env("HUNTER_API_KEY", "").strip(),
            google_auth_mode=env("GOOGLE_AUTH_MODE", "service_account").strip().lower(),
            google_service_account_file=Path(env("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials/service_account.json")),
            google_oauth_client_file=Path(env("GOOGLE_OAUTH_CLIENT_FILE", "credentials/oauth_client.json")),
            google_oauth_token_file=Path(env("GOOGLE_OAUTH_TOKEN_FILE", "credentials/authorized_user.json")),
            spreadsheet_id=env("SPREADSHEET_ID", "").strip(),
            spreadsheet_title=env("SPREADSHEET_TITLE", "NESCAC Alumni in Investment Banking"),
            cache_path=Path(env("CACHE_PATH", ".cache/alumni_finder.sqlite3")),
            cache_ttl_days=float(env("CACHE_TTL_DAYS", "30")),
            # PDL's Search API is limited to ~10 requests/minute on most plans.
            pdl_min_interval=float(env("PDL_MIN_SECONDS_BETWEEN_CALLS", "6.5")),
            # Hunter allows 15 requests/second; stay well under it.
            hunter_min_interval=float(env("HUNTER_MIN_SECONDS_BETWEEN_CALLS", "0.25")),
        )
