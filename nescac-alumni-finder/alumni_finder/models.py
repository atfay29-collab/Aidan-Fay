"""The Person record shared by every source, the enricher and the sheet writer."""
from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import linkedin_display_url, name_key, normalize_linkedin_url

# Email provenance, best first. An email is only replaced by one from a
# higher-ranked source, so a manually entered address is never overwritten.
EMAIL_SOURCE_LABELS = {
    "manual": "Manual",
    "pdl": "People Data Labs",
    "hunter": "Hunter.io",
    "pattern": "Pattern guess (unverified)",
}
EMAIL_SOURCE_RANK = {"manual": 4, "pdl": 3, "hunter": 2, "pattern": 1, "": 0}

SOURCE_LABELS = {"pdl": "People Data Labs", "manual": "Manual CSV", "sheet": "Sheet"}

# Shown in the Email Source column when there is no email.
NOTE_PDL_MASKED = "PDL has one (paid plan shows it)"
NOTE_NOT_FOUND = "Not found"
NOTE_NOT_LOOKED_UP = "Not looked up"
NOTE_OPTED_OUT = "Opted out of email lookups"


def email_source_from_label(label: str) -> str:
    label = (label or "").strip()
    for key, text in EMAIL_SOURCE_LABELS.items():
        if label.lower() == text.lower() or label.lower() == key:
            return key
    return ""


@dataclass
class Person:
    full_name: str
    first_name: str
    last_name: str
    bank: str  # Bank.name, or whatever an existing sheet row says
    schools: list[str]  # School.name values, primary school first
    title: str = ""
    linkedin_url: str = ""
    location: str = ""
    grad_year: str = ""
    email: str = ""
    email_source: str = ""  # key of EMAIL_SOURCE_LABELS
    email_confidence: str = ""
    email_note: str = ""  # why there is no email, when there isn't one
    email_opted_out: bool = False  # person asked data providers to stop processing their data
    pdl_id: str = ""
    sources: set[str] = field(default_factory=set)
    last_seen: str = ""
    # Columns the tool never overwrites (Status, Notes, anything the user adds).
    user_fields: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.linkedin_url = linkedin_display_url(self.linkedin_url) if self.linkedin_url else ""

    @property
    def primary_school(self) -> str:
        return self.schools[0] if self.schools else ""

    @property
    def also_attended(self) -> list[str]:
        return self.schools[1:]

    @property
    def linkedin_key(self) -> str:
        return normalize_linkedin_url(self.linkedin_url)

    @property
    def name_key(self) -> str:
        return name_key(self.first_name, self.last_name, self.full_name)

    @property
    def email_rank(self) -> int:
        return EMAIL_SOURCE_RANK.get(self.email_source, 0) if self.email else 0

    def set_email(self, email: str, source: str, confidence: str = "") -> None:
        self.email = email.strip().lower()
        self.email_source = source
        self.email_confidence = confidence
        self.email_note = ""
