"""Command-line entry point.

    python -m alumni_finder estimate            # PDL match counts per school (<= 1 credit each)
    python -m alumni_finder run [options]       # search, enrich emails, write the sheet
    python -m alumni_finder links               # manual LinkedIn alumni-search links + CSV template
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .cache import Cache
from .config import BANKS, BANKS_BY_NAME, SCHOOLS, Settings, resolve_bank, resolve_school, select
from .dedupe import Deduper
from .enrich import EmailEnricher, HunterClient
from .groups import classify
from .http import ApiError, AuthError, QuotaExceeded, RateLimiter
from .linkedin_links import build_links
from .models import EMAIL_SOURCE_LABELS, Person
from .sources.manual_csv import load_manual_csv, write_template
from .table import build_rows, write_csv

log = logging.getLogger("alumni_finder")

OUTPUT_DIR = Path("output")


@dataclass
class RunReport:
    found_per_school: dict[str, int] = field(default_factory=dict)
    manual_rows: int = 0
    filtered_by_title: int = 0
    duplicates_merged: int = 0
    new_people: int = 0
    total_people: int = 0
    failures: list[str] = field(default_factory=list)


def _split(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


def _scope(args) -> tuple[tuple, tuple]:
    schools = select(SCHOOLS, _split(args.schools), resolve_school)
    banks = select(BANKS, _split(args.banks), lambda text: resolve_bank(text))
    return schools, banks


def _cache(settings: Settings, args) -> Cache:
    if getattr(args, "no_cache", False):
        return Cache.disabled()
    return Cache(settings.cache_path, settings.cache_ttl_days * 86400, refresh=getattr(args, "refresh_cache", False))


def _pdl(settings: Settings, cache: Cache):
    from .sources.pdl import PDLSource

    return PDLSource(settings.pdl_api_key, cache=cache, limiter=RateLimiter(settings.pdl_min_interval))


def cmd_estimate(args, settings: Settings) -> int:
    schools, banks = _scope(args)
    if not settings.pdl_api_key:
        log.error("PDL_API_KEY is not set (see README 'People Data Labs setup').")
        return 1
    cache = _cache(settings, args)
    pdl = _pdl(settings, cache)
    total = 0
    print(f"{'School':<22}{'PDL matches':>12}")
    for school in schools:
        try:
            count = pdl.count(school, banks)
        except (AuthError, QuotaExceeded) as exc:
            log.error("%s", exc)
            return 1
        except ApiError as exc:
            log.error("%s: %s", school.name, exc)
            continue
        total += count
        print(f"{school.name:<22}{count:>12}")
    print(f"{'Total':<22}{total:>12}")
    print(
        f"\nA full run costs about 1 PDL credit per record returned (~{total} credits with no cap). "
        f"This estimate used {pdl.credits_spent} credit(s)"
        + (f"; {pdl.credits_remaining} remaining." if pdl.credits_remaining else ".")
    )
    return 0


def _collect_pdl(args, settings, cache, schools, banks, report: RunReport) -> list[Person]:
    if args.no_pdl:
        return []
    if not settings.pdl_api_key:
        log.warning("PDL_API_KEY is not set; skipping People Data Labs (use --manual-csv for manual finds).")
        return []
    pdl = _pdl(settings, cache)
    cap = args.max_per_school or None
    people: list[Person] = []
    for school in schools:
        found = 0
        try:
            for person in pdl.search(school, banks, max_records=cap):
                people.append(person)
                found += 1
        except AuthError:
            raise
        except QuotaExceeded as exc:
            report.found_per_school[school.name] = found
            report.failures.append(f"PDL stopped at {school.name}: {exc}")
            log.error("%s. Keeping what was found so far; cached pages make the next run resume cheaply.", exc)
            break
        except ApiError as exc:
            report.failures.append(f"PDL search for {school.name} failed: {exc}")
            log.error("PDL search for %s failed: %s", school.name, exc)
        report.found_per_school[school.name] = found
    log.info("PDL credits spent this run: %d (cached pages are free)", pdl.credits_spent)
    return people


def _title_filter(people: list[Person], keywords: list[str] | None, report: RunReport) -> list[Person]:
    if not keywords:
        return people
    lowered = [k.lower() for k in keywords]
    kept = []
    for person in people:
        # Hand-entered rows are kept: you already chose them.
        if "manual" in person.sources or any(k in person.title.lower() for k in lowered):
            kept.append(person)
    report.filtered_by_title = len(people) - len(kept)
    return kept


def _fill_groups(people: list[Person]) -> None:
    """Infer Division/Group from the title wherever they're still blank."""
    for person in people:
        if person.division and person.group:
            continue
        bank = BANKS_BY_NAME.get(person.bank)
        division, group = classify(person.title, bank_category=bank.category if bank else "")
        person.division = person.division or division
        person.group = person.group or group


def cmd_run(args, settings: Settings) -> int:
    schools, banks = _scope(args)
    report = RunReport()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    today = date.today().isoformat()

    # Connect to Google first so a Sheets setup problem fails before any credits are spent.
    writer = None
    existing: list[Person] = []
    extra_headers: list[str] = []
    if not args.no_sheet:
        from .sheets import SheetWriter, SheetsConfigError, open_sheet

        try:
            spreadsheet = open_sheet(settings)
            writer = SheetWriter(spreadsheet)
            existing, extra_headers = writer.read_existing()
        except SheetsConfigError as exc:
            log.error("%s", exc)
            return 1
        log.info("Sheet has %d existing rows", len(existing))

    cache = _cache(settings, args)
    collected: list[Person] = []
    for path in args.manual_csv or []:
        people, warnings = load_manual_csv(path)
        report.manual_rows += len(people)
        report.failures += warnings
        collected += people
    try:
        collected += _collect_pdl(args, settings, cache, schools, banks, report)
    except AuthError as exc:
        log.error("%s", exc)
        return 1
    if not collected and not existing:
        log.error("Nothing to write: no PDL results and no --manual-csv rows.")
        return 1

    collected = _title_filter(collected, _split(args.title_keywords), report)
    for person in collected:
        person.last_seen = today

    this_run = Deduper()
    this_run.add_all(collected)
    report.duplicates_merged = this_run.duplicates_merged
    everyone = Deduper()
    everyone.add_all(existing)
    before = len(everyone.people)
    everyone.add_all(this_run.people)
    report.new_people = len(everyone.people) - before
    report.total_people = len(everyone.people)
    _fill_groups(everyone.people)

    hunter = None
    if settings.hunter_api_key and not args.no_hunter:
        hunter = HunterClient(
            settings.hunter_api_key,
            cache=cache,
            limiter=RateLimiter(settings.hunter_min_interval),
            max_lookups=args.max_hunter_lookups or None,
        )
    elif not args.no_hunter:
        log.warning("HUNTER_API_KEY is not set; skipping Hunter email lookups.")
    enricher = EmailEnricher(hunter, guess_patterns=args.guess_emails)
    stats = enricher.enrich(everyone.people)

    rows = build_rows(everyone.people, extra_headers)
    csv_path = write_csv(args.output_csv, rows)
    log.info("Wrote local backup %s", csv_path)
    sheet_url = ""
    if writer:
        writer.write(everyone.people, extra_headers, SCHOOLS, BANKS, build_links(SCHOOLS, BANKS), now)
        sheet_url = writer.spreadsheet.url

    _print_report(report, everyone.people, stats, hunter, csv_path, sheet_url)
    return 2 if report.failures else 0


def _print_report(report: RunReport, people, stats, hunter, csv_path, sheet_url) -> None:
    print("\n=== Run summary ===")
    for school, n in report.found_per_school.items():
        print(f"  PDL {school:<20} {n:>5} profiles")
    if report.manual_rows:
        print(f"  Manual CSV rows            {report.manual_rows:>5}")
    if report.filtered_by_title:
        print(f"  Dropped by title filter    {report.filtered_by_title:>5}")
    print(f"  Duplicates merged          {report.duplicates_merged:>5}")
    print(f"  New people added           {report.new_people:>5}")
    print(f"  Total people               {report.total_people:>5}")
    by_source: dict[str, int] = {}
    for person in people:
        if person.email:
            label = EMAIL_SOURCE_LABELS.get(person.email_source, person.email_source)
            by_source[label] = by_source.get(label, 0) + 1
    with_email = sum(by_source.values())
    print(f"  With email                 {with_email:>5}  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_source.items())))
    print(f"  Without email              {len(people) - with_email:>5}")
    if hunter:
        print(f"  Hunter API calls           {hunter.lookups:>5}" + (f"  (stopped: {hunter.disabled_reason})" if hunter.disabled_reason else ""))
    if stats.opted_out:
        print(f"  Opted out of lookups       {stats.opted_out:>5}")
    if stats.failed:
        print(f"  Failed email lookups       {stats.failed:>5}")
    if report.failures:
        print("  Problems:")
        for failure in report.failures:
            print(f"    - {failure}")
    print(f"  Local CSV: {csv_path}")
    if sheet_url:
        print(f"  Sheet:     {sheet_url}")


def cmd_links(args, settings: Settings) -> int:
    schools, banks = _scope(args)
    links = build_links(schools, banks)
    rows = [["school", "bank", "keyword", "url"]] + [[l.school, l.bank, l.keyword, l.url] for l in links]
    out = write_csv(args.out, rows)
    template = OUTPUT_DIR / "manual_alumni_template.csv"
    if not template.exists():
        write_template(template)
    print(f"Wrote {len(links)} links to {out}")
    print(f"Fill in {template} as you browse, then: python -m alumni_finder run --manual-csv {template}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="alumni_finder", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def scope_args(p):
        p.add_argument("--schools", help="comma-separated subset, e.g. 'Colby,Bates' (default: all 11)")
        p.add_argument("--banks", help="comma-separated subset, e.g. 'Goldman Sachs,Evercore' (default: all)")

    est = sub.add_parser("estimate", help="count PDL matches per school before spending credits")
    scope_args(est)
    est.add_argument("--no-cache", action="store_true")

    run = sub.add_parser("run", help="search, enrich emails and write the Google Sheet")
    scope_args(run)
    run.add_argument("--max-per-school", type=int, default=50, help="cap PDL records per school, 0 = no cap (default 50; 1 credit each)")
    run.add_argument("--manual-csv", action="append", metavar="PATH", help="add people you found by hand (repeatable)")
    run.add_argument("--no-pdl", action="store_true", help="skip People Data Labs (e.g. manual CSV only)")
    run.add_argument("--no-hunter", action="store_true", help="skip Hunter.io email lookups")
    run.add_argument("--max-hunter-lookups", type=int, default=0, help="cap uncached Hunter calls this run (0 = no cap)")
    run.add_argument("--guess-emails", action="store_true", help="fill remaining gaps with the bank's email pattern, labeled unverified")
    run.add_argument("--title-keywords", help="keep only titles containing one of these, e.g. 'investment banking,M&A,analyst'")
    run.add_argument("--no-sheet", action="store_true", help="write only the local CSV")
    run.add_argument("--output-csv", default=str(OUTPUT_DIR / "alumni_latest.csv"))
    run.add_argument("--refresh-cache", action="store_true", help="ignore cached API responses (spends credits again)")
    run.add_argument("--no-cache", action="store_true")

    links = sub.add_parser("links", help="write LinkedIn alumni-search links for manual browsing")
    scope_args(links)
    links.add_argument("--out", default=str(OUTPUT_DIR / "linkedin_search_links.csv"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = Settings.from_env()
    try:
        return {"estimate": cmd_estimate, "run": cmd_run, "links": cmd_links}[args.command](args, settings)
    except ValueError as exc:  # e.g. unknown --schools/--banks name
        log.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
