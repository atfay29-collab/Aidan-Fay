"""Row layout shared by the Google Sheet and the local CSV backup."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable, Sequence

from .config import BANKS_BY_NAME, resolve_bank, resolve_school
from .models import EMAIL_SOURCE_LABELS, NOTE_OPTED_OUT, SOURCE_LABELS, Person, email_source_from_label
from .normalize import split_name

SCHOOL, BANK, BANK_TYPE, NAME, TITLE, EMAIL, EMAIL_SOURCE, EMAIL_CONFIDENCE = (
    "School",
    "Bank",
    "Bank Type",
    "Full Name",
    "Title",
    "Email",
    "Email Source",
    "Email Confidence",
)
LINKEDIN, LOCATION, GRAD_YEAR, ALSO_ATTENDED, FOUND_VIA, LAST_SEEN = (
    "LinkedIn URL",
    "Location",
    "Grad Year",
    "Also Attended",
    "Found Via",
    "Last Seen",
)
STATUS, NOTES, SOURCE_ID = "Status", "Notes", "Source ID"

# Columns the tool fills in. On every run they are rewritten from fresh data.
TOOL_COLUMNS = [
    SCHOOL, BANK, BANK_TYPE, NAME, TITLE, EMAIL, EMAIL_SOURCE, EMAIL_CONFIDENCE,
    LINKEDIN, LOCATION, GRAD_YEAR, ALSO_ATTENDED, FOUND_VIA, LAST_SEEN,
]
# Columns for your own tracking. The tool never overwrites them, and any
# extra column you add to the sheet is carried along the same way.
USER_COLUMNS = [STATUS, NOTES]
HIDDEN_COLUMNS = [SOURCE_ID]
COLUMNS = TOOL_COLUMNS + USER_COLUMNS + HIDDEN_COLUMNS



def headers_with_extras(extra_headers: Sequence[str] = ()) -> list[str]:
    return COLUMNS + [h for h in extra_headers if h and h not in COLUMNS]


def person_to_row(person: Person, headers: Sequence[str]) -> list[str]:
    bank = BANKS_BY_NAME.get(person.bank)
    if person.email:
        email_source = EMAIL_SOURCE_LABELS.get(person.email_source, person.email_source)
    else:
        email_source = person.email_note
    source_id = f"pdl:{person.pdl_id}" if person.pdl_id else ""
    values = {
        SCHOOL: person.primary_school,
        BANK: person.bank,
        BANK_TYPE: bank.category if bank else "",
        NAME: person.full_name,
        TITLE: person.title,
        EMAIL: person.email,
        EMAIL_SOURCE: email_source,
        EMAIL_CONFIDENCE: person.email_confidence if person.email else "",
        LINKEDIN: person.linkedin_url,
        LOCATION: person.location,
        GRAD_YEAR: person.grad_year,
        ALSO_ATTENDED: ", ".join(person.also_attended),
        FOUND_VIA: ", ".join(SOURCE_LABELS.get(s, s) for s in sorted(person.sources)),
        LAST_SEEN: person.last_seen,
        SOURCE_ID: source_id,
    }
    return [values[h] if h in values else person.user_fields.get(h, "") for h in headers]


def _canonical_school(text: str) -> str:
    # Rows you add by hand may say "Colby College" or "colby"; group them with "Colby".
    school = resolve_school(text)
    return school.name if school else text.strip()


def _canonical_bank(text: str) -> str:
    bank = resolve_bank(text)
    return bank.name if bank else text.strip()


def row_to_person(headers: Sequence[str], row: Sequence[str]) -> Person | None:
    """Rebuild a Person from a sheet row so re-runs can merge with it."""
    cells = {h: (row[i] if i < len(row) else "").strip() for i, h in enumerate(headers) if h}
    if not any(cells.values()):
        return None
    full = cells.get(NAME, "")
    first, last = split_name(full)
    schools = [_canonical_school(cells.get(SCHOOL, ""))]
    schools += [_canonical_school(s) for s in cells.get(ALSO_ATTENDED, "").split(",") if s.strip()]
    source_labels = {label.lower(): key for key, label in SOURCE_LABELS.items()}
    sources = {
        source_labels.get(s.strip().lower(), s.strip())
        for s in cells.get(FOUND_VIA, "").split(",")
        if s.strip()
    }
    person = Person(
        full_name=full,
        first_name=first,
        last_name=last,
        bank=_canonical_bank(cells.get(BANK, "")),
        schools=[s for s in schools if s],
        title=cells.get(TITLE, ""),
        linkedin_url=cells.get(LINKEDIN, ""),
        location=cells.get(LOCATION, ""),
        grad_year=cells.get(GRAD_YEAR, ""),
        pdl_id=cells.get(SOURCE_ID, "").removeprefix("pdl:"),
        sources=sources or {"sheet"},
        last_seen=cells.get(LAST_SEEN, ""),
    )
    email = cells.get(EMAIL, "")
    source_label = cells.get(EMAIL_SOURCE, "")
    if email:
        # An address typed into the sheet with no source is treated as manual,
        # which ranks highest, so the tool never replaces it.
        person.set_email(email, email_source_from_label(source_label) or "manual", cells.get(EMAIL_CONFIDENCE, ""))
    else:
        person.email_note = source_label
        person.email_opted_out = source_label == NOTE_OPTED_OUT
    person.user_fields = {h: v for h, v in cells.items() if h not in TOOL_COLUMNS and h not in HIDDEN_COLUMNS}
    return person


def sort_key(person: Person) -> tuple:
    """School A-Z, then bank A-Z, then last name, so rows read grouped by school then bank."""
    return (
        person.primary_school.lower() or "~",
        person.bank.lower(),
        person.last_name.lower(),
        person.first_name.lower(),
    )


def build_rows(people: Iterable[Person], extra_headers: Sequence[str] = ()) -> list[list[str]]:
    headers = headers_with_extras(extra_headers)
    ordered = sorted(people, key=sort_key)
    return [headers] + [person_to_row(p, headers) for p in ordered]


def write_csv(path: str | Path, rows: list[list[str]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(rows)
    return path
