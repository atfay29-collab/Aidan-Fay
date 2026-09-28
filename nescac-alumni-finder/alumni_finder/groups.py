"""Infer a person's division and group at the bank from their job title.

No data source has a structured "group" field, but bankers usually put it in
their title or headline: "Investment Banking Analyst - Healthcare",
"Associate, Leveraged Finance", "VP, TMT M&A". Titles that are just "Analyst"
stay blank; fill those in by hand from LinkedIn and the tool will keep them.

Edit the tables below to add groups or catch other spellings.
"""
from __future__ import annotations

import re

from .config import ELITE_BOUTIQUE

IB = "Investment Banking"
ST = "Sales & Trading"
RESEARCH = "Research"
WEALTH = "Wealth Management"
AM = "Asset Management"
CORPORATE = "Corporate Banking"
RETAIL = "Retail Banking"
TECH = "Technology"
OPS = "Operations"
CONTROL = "Risk, Compliance & Legal"
HR = "Human Resources"

# Checked in order; the first division with a matching pattern wins.
DIVISION_RULES: list[tuple[str, list[str]]] = [
    (RESEARCH, [r"\bresearch\b"]),
    (WEALTH, [r"wealth", r"financial advis[oe]r", r"private bank", r"private client", r"\bpwm\b"]),
    (AM, [r"asset management", r"investment management", r"portfolio manag"]),
    (RETAIL, [r"\bbranch\b", r"\bteller\b", r"consumer bank", r"retail bank", r"relationship banker",
              r"personal banker", r"\bmortgage"]),
    (CORPORATE, [r"corporate bank", r"commercial bank", r"treasury services", r"transaction banking"]),
    (IB, [r"investment bank", r"\bibd\b", r"corporate finance", r"\bm\s?&\s?a\b", r"mergers", r"capital markets",
          r"leveraged finance", r"\blev ?fin\b", r"restructuring", r"\bcoverage\b", r"financial sponsors?",
          r"\badvisory\b", r"\bgeneralist\b", r"\becm\b", r"\bdcm\b", r"public finance"]),
    (ST, [r"sales (&|and) trading", r"\btrad(er|ing)\b", r"global markets", r"(?<!capital )\bmarkets\b",
          r"\bsales\b", r"\bstructur(er|ing)\b", r"prime (brokerage|services)", r"\bficc\b", r"fixed income"]),
    (TECH, [r"software", r"engineer", r"developer", r"data scien", r"\bcyber", r"\bdevops\b",
            r"information technology", r"\btechnology (analyst|associate|vice president|manager|lead|architect)\b"]),
    (OPS, [r"\boperations\b", r"middle office", r"back office", r"settlement", r"facilities",
           r"corporate real estate", r"procurement"]),
    (CONTROL, [r"\brisk\b", r"compliance", r"\blegal\b", r"counsel", r"\baudit", r"controller", r"regulatory",
               r"\bkyc\b", r"\baml\b"]),
    (HR, [r"human resources", r"recruit", r"\btalent\b", r"\bcampus\b"]),
]

# Investment-banking product groups.
PRODUCT_GROUPS: list[tuple[str, list[str]]] = [
    ("M&A", [r"\bm\s?&\s?a\b", r"mergers"]),
    ("Leveraged Finance", [r"leveraged finance", r"\blev ?fin\b"]),
    ("Equity Capital Markets", [r"equity capital markets", r"\becm\b"]),
    ("Debt Capital Markets", [r"debt capital markets", r"\bdcm\b", r"investment grade"]),
    ("Restructuring", [r"restructuring", r"liability management"]),
    ("Financial Sponsors", [r"financial sponsors?", r"sponsor coverage"]),
    ("Shareholder Advisory", [r"shareholder advisory", r"activism"]),
    ("Private Capital Advisory", [r"private (capital|funds) (advisory|group)", r"secondar(y|ies) advisory"]),
    ("Public Finance", [r"public finance", r"municipal"]),
    ("Capital Markets", [r"capital markets"]),
]

# Industry coverage groups (also used for research and corporate-banking coverage).
SECTOR_GROUPS: list[tuple[str, list[str]]] = [
    ("TMT", [r"\btmt\b", r"telecom", r"\bmedia\b", r"internet", r"software", r"semiconductor"]),
    ("Healthcare", [r"health ?care", r"life sciences", r"biotech", r"pharma", r"medtech"]),
    ("FIG", [r"\bfig\b", r"financial institutions", r"fintech", r"insurance"]),
    ("Industrials", [r"industrials?", r"aerospace", r"transportation"]),
    ("Consumer & Retail", [r"consumer", r"\bretail\b"]),
    ("Energy & Power", [r"\benergy\b", r"\bpower\b", r"utilities", r"oil (&|and) gas", r"natural resources",
                        r"renewable", r"\bmining\b", r"\bmetals\b"]),
    ("Infrastructure", [r"infrastructure"]),
    ("Real Estate", [r"real estate", r"\breits?\b", r"lodging", r"gaming"]),
]

# Sales & trading desks.
DESK_GROUPS: list[tuple[str, list[str]]] = [
    ("Prime Brokerage", [r"prime (brokerage|services)"]),
    ("Securitized Products", [r"securiti[sz]ed", r"structured products?", r"\bmbs\b", r"\babs\b"]),
    ("Equities", [r"equit(y|ies)"]),
    ("Credit", [r"\bcredit\b"]),
    ("Rates", [r"\brates\b"]),
    ("FX", [r"\bfx\b", r"foreign exchange", r"currenc"]),
    ("Commodities", [r"commodit"]),
    ("Derivatives", [r"derivative"]),
    ("Fixed Income", [r"fixed income", r"\bficc\b"]),
]

# Divisions where an industry keyword doesn't describe a banking group
# ("Technology Analyst" in IT, "Consumer Banking" at a branch).
_NO_GROUP = {TECH, OPS, CONTROL, HR, RETAIL, WEALTH, AM}
# A bare "technology" means the TMT group only in a clearly banking/research
# title; on its own ("Analyst, Technology") it is usually the IT division.
_TECH_WORD = re.compile(r"\btech(nology)?\b")
_TECH_MEANS_TMT = {IB, RESEARCH, CORPORATE}
_BANKER_LADDER = re.compile(
    r"\b(analyst|associate|vice president|vp|director|managing director|md|partner|principal|senior advisor)\b"
)


def _first(rules: list[tuple[str, list[str]]], text: str) -> str:
    for label, patterns in rules:
        if any(re.search(p, text) for p in patterns):
            return label
    return ""


def _normalize(text: str) -> str:
    text = (text or "").lower().replace("&amp;", "&")
    return re.sub(r"\s+", " ", text)


def classify(title: str, *more_text: str, bank_category: str = "") -> tuple[str, str]:
    """Return (division, group), either of which may be ''.

    The title decides first; extra text (e.g. a profile headline) is only
    consulted when the title alone gives nothing.
    """
    for text in (title, *more_text):
        text = _normalize(text)
        if not text:
            continue
        division = _first(DIVISION_RULES, text)
        if division == ST:
            group = _first(DESK_GROUPS, text)
        elif division in _NO_GROUP:
            group = ""
        else:
            sector = _first(SECTOR_GROUPS, text)
            if not sector and division in _TECH_MEANS_TMT and _TECH_WORD.search(text):
                sector = "TMT"
            product = _first(PRODUCT_GROUPS, text) if division in (IB, "") else ""
            group = ", ".join(g for g in (sector, product) if g)
            if not division and product:
                division = IB
        if not division and bank_category == ELITE_BOUTIQUE and _BANKER_LADDER.search(text):
            # Boutiques are advisory firms: a plain "Associate" there is a banker.
            division = IB
        if division or group:
            return division, group
    return "", ""
