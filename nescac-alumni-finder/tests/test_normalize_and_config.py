import pytest

from alumni_finder.config import BANKS, SCHOOLS, match_school_entry, resolve_bank, resolve_school, select, select_banks
from alumni_finder.normalize import name_key, normalize_domain, normalize_linkedin_url, split_name


def test_split_name_drops_middle_names_and_suffixes():
    assert split_name("Jane Q. Doe, CFA") == ("Jane", "Doe")
    assert split_name("John Smith Jr.") == ("John", "Smith")
    assert split_name("Madonna") == ("Madonna", "")


def test_name_key_ignores_accents_case_and_middle_names():
    assert name_key("José", "García") == name_key("jose", "garcia")
    assert name_key("", "", "John A. Smith") == name_key("John", "Smith")
    assert name_key("", "", "Cher") == ""


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/in/JaneDoe/?trk=public",
        "linkedin.com/in/janedoe",
        "http://uk.linkedin.com/in/janedoe/",
    ],
)
def test_linkedin_urls_normalize_to_one_key(url):
    assert normalize_linkedin_url(url) == "linkedin.com/in/janedoe"


def test_non_profile_urls_are_rejected():
    assert normalize_linkedin_url("https://www.linkedin.com/company/goldman-sachs") == ""
    assert normalize_linkedin_url("https://example.com/in/janedoe") == ""


def test_normalize_domain():
    assert normalize_domain("https://www.GoldmanSachs.com/careers") == "goldmansachs.com"
    assert normalize_domain("jane@gs.com") == "gs.com"
    assert normalize_domain("Goldman Sachs") == ""


@pytest.mark.parametrize(
    "company,website,expected",
    [
        ("Goldman Sachs Asset Management", "", "Goldman Sachs"),
        ("", "https://careers.jpmorgan.com", "J.P. Morgan"),
        ("Credit Suisse", "", "UBS / Credit Suisse"),
        ("moelis & co", "", "Moelis & Company"),
        ("Bank of America Merrill Lynch", "", "Bank of America"),
        ("Citizens Bank", "", None),  # "citi" must not prefix-match "citizens"
        ("Merrill Lynch", "", None),  # wealth management, not BofA Securities
        ("Centerview Capital", "", None),  # a PE firm, not Centerview Partners
    ],
)
def test_resolve_bank(company, website, expected):
    bank = resolve_bank(company, website)
    assert (bank.name if bank else None) == expected


def test_resolve_school_accepts_aliases_and_domains():
    assert resolve_school("colby college").name == "Colby"
    assert resolve_school("Conn College").name == "Connecticut College"
    assert resolve_school("trincoll.edu").name == "Trinity"
    assert resolve_school("UMass Amherst") is None


def test_trinity_is_matched_by_domain_not_ambiguous_name():
    assert match_school_entry({"name": "trinity college", "website": "trincoll.edu"}).name == "Trinity"
    assert match_school_entry({"name": "trinity college", "website": "tcd.ie"}) is None
    assert match_school_entry({"name": "trinity college"}) is None


def test_scope_covers_all_requested_schools_and_banks():
    assert len(SCHOOLS) == 11
    assert len(BANKS) == 15


def test_select_keeps_config_order_and_rejects_unknown_names():
    chosen = select(SCHOOLS, ["Williams", "amherst college"], resolve_school)
    assert [s.name for s in chosen] == ["Amherst", "Williams"]
    with pytest.raises(ValueError):
        select(SCHOOLS, ["Harvard"], resolve_school)


def test_bank_category_shortcuts():
    boutiques = [b.name for b in select_banks(["elite boutiques"])]
    assert boutiques == [
        "Evercore", "Lazard", "Centerview Partners", "Moelis & Company", "PJT Partners",
        "Perella Weinberg Partners", "Houlihan Lokey", "Qatalyst Partners",
    ]
    assert len(select_banks(["Bulge Bracket"])) == 7
    assert [b.name for b in select_banks(["Evercore", "gs"])] == ["Goldman Sachs", "Evercore"]
    assert select_banks(None) == BANKS
    with pytest.raises(ValueError):
        select_banks(["Jefferies"])
