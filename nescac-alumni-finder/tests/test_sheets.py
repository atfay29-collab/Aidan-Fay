from alumni_finder.config import BANKS, SCHOOLS
from alumni_finder.linkedin_links import build_links
from alumni_finder.models import Person
from alumni_finder.sheets import LINKS_TAB, MASTER_TAB, SUMMARY_TAB, SheetWriter
from alumni_finder.table import COLUMNS

from conftest import FakeSpreadsheet, FakeWorksheet


def jane(**kw):
    return Person(full_name="Jane Doe", first_name="Jane", last_name="Doe", bank="Goldman Sachs", schools=["Colby"], **kw)


def write(spreadsheet, people, extras=()):
    SheetWriter(spreadsheet).write(people, list(extras), SCHOOLS, BANKS, build_links(SCHOOLS, BANKS), "2026-09-28 10:00")


def test_read_existing_returns_people_and_user_added_columns():
    header = COLUMNS + ["Coffee Chat"]
    row = [""] * len(header)
    row[header.index("School")] = "Colby"
    row[header.index("Bank")] = "Evercore"
    row[header.index("Full Name")] = "Sam Lee"
    row[header.index("Status")] = "Emailed"
    row[header.index("Coffee Chat")] = "10/3"
    sheet = FakeSpreadsheet([FakeWorksheet(MASTER_TAB, [header, row, [""] * len(header)])])
    people, extras = SheetWriter(sheet).read_existing()
    assert extras == ["Coffee Chat"]
    assert len(people) == 1
    assert people[0].user_fields == {"Status": "Emailed", "Notes": "", "Coffee Chat": "10/3"}


def test_read_existing_on_a_new_spreadsheet():
    assert SheetWriter(FakeSpreadsheet([FakeWorksheet("Sheet1")])).read_existing() == ([], [])


def test_write_creates_all_tabs_and_removes_blank_default_sheet():
    sheet = FakeSpreadsheet([FakeWorksheet("Sheet1")])
    write(sheet, [jane()])
    assert set(sheet.tabs) == {MASTER_TAB, SUMMARY_TAB, LINKS_TAB}


def test_master_tab_is_written_raw_so_values_never_run_as_formulas():
    sheet = FakeSpreadsheet()
    write(sheet, [jane(title='=IMPORTXML("http://evil")')])
    master = sheet.tabs[MASTER_TAB]
    assert master.updates == [("A1", "RAW")]
    assert master.values[1][COLUMNS.index("Title")] == '=IMPORTXML("http://evil")'


def test_summary_counts_school_by_bank_with_live_formulas():
    sheet = FakeSpreadsheet()
    write(sheet, [jane()])
    summary = sheet.tabs[SUMMARY_TAB]
    assert summary.updates == [("A1", "USER_ENTERED")]
    header, amherst = summary.values[0], summary.values[1]
    assert header[1] == "Goldman Sachs" and header[-2:] == ["Total", "With Email"]
    assert amherst[0] == "Amherst"
    assert amherst[1] == "=COUNTIFS('Alumni'!$A:$A,$A2,'Alumni'!$B:$B,B$1)"
    assert amherst[-2] == "=SUM(B2:P2)"  # 15 bank columns: B..P
    assert amherst[-1] == "=COUNTIFS('Alumni'!$A:$A,$A2,'Alumni'!$F:$F,\"<>\")"
    assert summary.values[len(SCHOOLS) + 1][1] == "=SUM(B2:B12)"


def test_filter_views_are_rebuilt_per_school_and_bank():
    old = [{"filterViewId": 7, "title": "By school: Bates"}, {"filterViewId": 8, "title": "My own view"}]
    sheet = FakeSpreadsheet(filter_views=old)
    people = [jane(), Person("Sam Lee", "Sam", "Lee", "Evercore", ["Bates"])]
    write(sheet, people)
    requests = sheet.batches[0]["requests"]
    assert {"deleteFilterView": {"filterId": 7}} in requests
    assert {"deleteFilterView": {"filterId": 8}} not in requests  # user's own views are left alone
    titles = [r["addFilterView"]["filter"]["title"] for r in requests if "addFilterView" in r]
    assert titles == ["By school: Bates", "By school: Colby", "By bank: Evercore", "By bank: Goldman Sachs"]
    colby = next(r["addFilterView"]["filter"] for r in requests if r.get("addFilterView", {}).get("filter", {}).get("title") == "By school: Colby")
    assert colby["filterSpecs"][0]["filterCriteria"]["condition"]["values"] == [{"userEnteredValue": "Colby"}]
    assert any("setBasicFilter" in r for r in requests)


def test_links_tab_lists_every_school_bank_keyword():
    sheet = FakeSpreadsheet()
    write(sheet, [jane()])
    links = sheet.tabs[LINKS_TAB].values
    keywords = sum(len(b.linkedin_keywords) for b in BANKS)
    assert len(links) == 1 + len(SCHOOLS) * keywords
    assert links[1][3].startswith("https://www.linkedin.com/school/amherst-college/people/?keywords=")


def test_expired_oauth_sign_in_is_cleared_and_retried(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from google.auth.exceptions import RefreshError

    from alumni_finder import sheets

    token = tmp_path / "authorized_user.json"
    token.write_text("{}")
    settings = SimpleNamespace(google_auth_mode="oauth", google_oauth_token_file=token, spreadsheet_id="abc")
    fake = FakeSpreadsheet()
    attempts = []

    def open_spreadsheet(client, settings):
        attempts.append(token.exists())
        if len(attempts) == 1:
            raise RefreshError("invalid_grant: Token has been expired or revoked.")
        return fake

    monkeypatch.setattr(sheets, "connect", lambda settings: object())
    monkeypatch.setattr(sheets, "open_spreadsheet", open_spreadsheet)
    assert sheets.open_sheet(settings) is fake
    assert attempts == [True, False]  # second attempt ran after the stale token was deleted


def test_unreachable_spreadsheet_gives_a_setup_hint(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from alumni_finder import sheets

    def open_spreadsheet(client, settings):
        raise PermissionError

    monkeypatch.setattr(sheets, "connect", lambda settings: object())
    monkeypatch.setattr(sheets, "open_spreadsheet", open_spreadsheet)
    with pytest.raises(sheets.SheetsConfigError, match="SPREADSHEET_ID"):
        sheets.open_sheet(SimpleNamespace(google_auth_mode="oauth", spreadsheet_id="abc"))
