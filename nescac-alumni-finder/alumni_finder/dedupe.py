"""De-duplication: one row per real person across searches, sources and runs.

Two records are the same person when any of these hold:
  1. same LinkedIn profile URL (normalized),
  2. same People Data Labs record id,
  3. same normalized name (first + last, ignoring middle names, case and
     accents) at the same bank, as long as the two records don't carry
     different LinkedIn URLs or PDL ids (which would prove they're different
     people who share a name).

The same person turns up more than once when they attended two NESCAC
schools (one search per school), when a manual CSV row repeats a PDL
result, or on a re-run against the rows already in the sheet.
"""
from __future__ import annotations

from .models import Person

_SCALAR_FIELDS = ("full_name", "first_name", "last_name", "bank", "title", "linkedin_url", "location", "grad_year", "pdl_id")


def _conflicts(a: Person, b: Person) -> bool:
    if a.linkedin_key and b.linkedin_key and a.linkedin_key != b.linkedin_key:
        return True
    return bool(a.pdl_id and b.pdl_id and a.pdl_id != b.pdl_id)


def merge(old: Person, new: Person) -> Person:
    """Combine two records of one person. `new` is the fresher observation."""
    for name in _SCALAR_FIELDS:
        value = getattr(new, name)
        if value:
            setattr(old, name, value)
    for school in new.schools:
        if school not in old.schools:
            old.schools.append(school)
    # Keep the better email; on a tie prefer the fresher one.
    if new.email and new.email_rank >= old.email_rank:
        old.email, old.email_source, old.email_confidence = new.email, new.email_source, new.email_confidence
        old.email_note = ""
    elif not old.email and new.email_note:
        old.email_note = new.email_note
    old.email_opted_out = old.email_opted_out or new.email_opted_out
    old.sources |= new.sources
    old.last_seen = max(old.last_seen, new.last_seen)
    for key, value in new.user_fields.items():
        if value and not old.user_fields.get(key):
            old.user_fields[key] = value
    return old


class Deduper:
    def __init__(self) -> None:
        self.people: list[Person] = []
        self.duplicates_merged = 0
        self._by_linkedin: dict[str, Person] = {}
        self._by_pdl: dict[str, Person] = {}
        self._by_name_bank: dict[str, list[Person]] = {}

    def _find(self, person: Person) -> Person | None:
        if person.linkedin_key and person.linkedin_key in self._by_linkedin:
            return self._by_linkedin[person.linkedin_key]
        if person.pdl_id and person.pdl_id in self._by_pdl:
            return self._by_pdl[person.pdl_id]
        if person.name_key:
            for candidate in self._by_name_bank.get(f"{person.name_key}|{person.bank}", []):
                if not _conflicts(candidate, person):
                    return candidate
        return None

    def _index(self, person: Person) -> None:
        if person.linkedin_key:
            self._by_linkedin[person.linkedin_key] = person
        if person.pdl_id:
            self._by_pdl[person.pdl_id] = person
        if person.name_key:
            bucket = self._by_name_bank.setdefault(f"{person.name_key}|{person.bank}", [])
            if person not in bucket:
                bucket.append(person)

    def add(self, person: Person) -> Person:
        existing = self._find(person)
        if existing is None:
            self.people.append(person)
            self._index(person)
            return person
        self.duplicates_merged += 1
        old_bank = existing.bank
        merged = merge(existing, person)
        if merged.bank != old_bank:  # they changed banks; move the name index entry
            old_bucket = self._by_name_bank.get(f"{merged.name_key}|{old_bank}", [])
            if merged in old_bucket:
                old_bucket.remove(merged)
        self._index(merged)
        return merged

    def add_all(self, people) -> None:
        for person in people:
            self.add(person)
