"""Manual CSV source: people you found yourself on LinkedIn's alumni tool.

Nothing here touches LinkedIn. You browse the alumni search links (see
linkedin_links.py) in your own browser, note who you find, and list them in a
CSV. This module validates the rows and hands them to the same
de-duplication and email-enrichment pipeline as the API results.

Columns (header names are case-insensitive; only school, bank and a name are required):
    school, bank, full_name (or first_name + last_name), title, linkedin_url,
    email, location, grad_year
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from ..config import resolve_bank, resolve_school
from ..models import Person
from ..normalize import is_valid_email, split_name

log = logging.getLogger(__name__)

TEMPLATE_HEADERS = ["school", "bank", "full_name", "title", "linkedin_url", "email", "location", "grad_year"]

_HEADER_ALIASES = {
    "name": "full_name",
    "full name": "full_name",
    "first name": "first_name",
    "last name": "last_name",
    "linkedin": "linkedin_url",
    "linkedin url": "linkedin_url",
    "profile": "linkedin_url",
    "company": "bank",
    "employer": "bank",
    "college": "school",
    "class_year": "grad_year",
    "class year": "grad_year",
    "year": "grad_year",
    "grad year": "grad_year",
}


def _canonical_header(header: str) -> str:
    key = (header or "").strip().lower()
    return _HEADER_ALIASES.get(key, key.replace(" ", "_"))


def load_manual_csv(path: str | Path) -> tuple[list[Person], list[str]]:
    """Parse a manual CSV. Returns (people, warnings); bad rows are skipped, not fatal."""
    people: list[Person] = []
    warnings: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            return [], [f"{path}: file is empty"]
        for line_no, raw in enumerate(reader, start=2):
            row = {_canonical_header(k): (v or "").strip() for k, v in raw.items() if k}
            if not any(row.values()):
                continue
            where = f"{path}:{line_no}"
            school = resolve_school(row.get("school", ""))
            if school is None:
                warnings.append(f"{where}: unknown school {row.get('school')!r}; row skipped")
                continue
            bank = resolve_bank(row.get("bank", ""))
            if bank is None:
                warnings.append(f"{where}: unknown bank {row.get('bank')!r}; row skipped")
                continue
            first, last = row.get("first_name", ""), row.get("last_name", "")
            full = row.get("full_name", "") or " ".join(p for p in (first, last) if p)
            if not (first and last):
                first, last = split_name(full)
            if not full:
                warnings.append(f"{where}: no name; row skipped")
                continue
            person = Person(
                full_name=full,
                first_name=first,
                last_name=last,
                bank=bank.name,
                schools=[school.name],
                title=row.get("title", ""),
                linkedin_url=row.get("linkedin_url", ""),
                location=row.get("location", ""),
                grad_year=row.get("grad_year", ""),
                sources={"manual"},
            )
            email = row.get("email", "")
            if email:
                if is_valid_email(email):
                    person.set_email(email, "manual", "provided")
                else:
                    warnings.append(f"{where}: ignoring invalid email {email!r}")
            people.append(person)
    for warning in warnings:
        log.warning(warning)
    return people, warnings


def write_template(path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerow(TEMPLATE_HEADERS)
