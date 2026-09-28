import pytest

from alumni_finder.config import BULGE_BRACKET, ELITE_BOUTIQUE
from alumni_finder.groups import classify


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Investment Banking Analyst - Healthcare", ("Investment Banking", "Healthcare")),
        ("Associate, Leveraged Finance", ("Investment Banking", "Leveraged Finance")),
        ("Vice President, TMT M&A", ("Investment Banking", "TMT, M&A")),
        ("Technology Investment Banking Associate", ("Investment Banking", "TMT")),
        ("Analyst, Financial Institutions Group", ("", "FIG")),
        ("Investment Banking Analyst, FIG", ("Investment Banking", "FIG")),
        ("Managing Director, Head of Consumer & Retail Investment Banking", ("Investment Banking", "Consumer & Retail")),
        ("Equity Capital Markets Associate", ("Investment Banking", "Equity Capital Markets")),
        ("Debt Capital Markets Analyst", ("Investment Banking", "Debt Capital Markets")),
        ("Restructuring Vice President", ("Investment Banking", "Restructuring")),
        ("Real Estate, Gaming & Lodging Investment Banking Analyst", ("Investment Banking", "Real Estate")),
        ("Investment Banking Analyst", ("Investment Banking", "")),
        ("Equity Research Associate - Healthcare", ("Research", "Healthcare")),
        ("Fixed Income Sales", ("Sales & Trading", "Fixed Income")),
        ("Equity Derivatives Trader", ("Sales & Trading", "Equities")),
        ("Global Markets Analyst, Rates", ("Sales & Trading", "Rates")),
        ("Private Wealth Management Associate", ("Wealth Management", "")),
        ("Financial Advisor", ("Wealth Management", "")),
        ("Corporate Banking Associate, TMT Coverage", ("Corporate Banking", "TMT")),
        ("Consumer Banking Branch Manager", ("Retail Banking", "")),
        ("Software Engineer", ("Technology", "")),
        ("Technology Analyst", ("Technology", "")),
        ("Operations Analyst", ("Operations", "")),
        ("Compliance Officer", ("Risk, Compliance & Legal", "")),
        ("Analyst", ("", "")),
        ("", ("", "")),
    ],
)
def test_classify_titles(title, expected):
    assert classify(title, bank_category=BULGE_BRACKET) == expected


def test_plain_banker_title_at_a_boutique_is_investment_banking():
    assert classify("Associate", bank_category=ELITE_BOUTIQUE) == ("Investment Banking", "")
    assert classify("Associate", bank_category=BULGE_BRACKET) == ("", "")


def test_headline_is_used_only_when_the_title_says_nothing():
    assert classify("Analyst", "Healthcare Investment Banking @ Goldman Sachs") == ("Investment Banking", "Healthcare")
    assert classify("M&A Analyst", "Formerly Healthcare coverage") == ("Investment Banking", "M&A")
