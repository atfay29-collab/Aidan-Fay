import csv

import pytest

from alumni_finder import cli
from alumni_finder.config import SCHOOLS_BY_NAME
from alumni_finder.http import QuotaExceeded
from alumni_finder.sources.pdl import parse_person

from conftest import pdl_record


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("PDL_API_KEY", "HUNTER_API_KEY", "SPREADSHEET_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    return tmp_path


def read_csv(path):
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


class FakePDL:
    """Stands in for PDLSource: returns canned records per school."""

    def __init__(self, by_school, fail_at=None):
        self.by_school = by_school
        self.fail_at = fail_at
        self.credits_spent = 0

    def search(self, school, banks, max_records=None):
        if school.name == self.fail_at:
            raise QuotaExceeded("PDL: out of credits")
        for record in self.by_school.get(school.name, []):
            yield parse_person(record, banks, school_hint=school)


def test_manual_only_run_writes_deduplicated_csv(env):
    manual = env / "manual.csv"
    manual.write_text(
        "school,bank,full_name,email\n"
        "Colby,Goldman Sachs,Jane Doe,jane.doe@gs.com\n"
        "Colby,GS,Jane Doe,\n"
        "Bates,Evercore,Sam Lee,\n"
    )
    code = cli.main(["run", "--no-sheet", "--no-pdl", "--manual-csv", str(manual), "--output-csv", "out.csv"])
    assert code == 0
    rows = read_csv(env / "out.csv")
    assert [(r["School"], r["Bank"], r["Full Name"], r["Email"]) for r in rows] == [
        ("Bates", "Evercore", "Sam Lee", ""),
        ("Colby", "Goldman Sachs", "Jane Doe", "jane.doe@gs.com"),
    ]


def test_run_merges_a_person_found_under_two_schools(env, monkeypatch):
    shared = pdl_record(
        education=[
            {"school": {"name": "colby college", "website": "colby.edu"}, "degrees": ["bachelors"], "end_date": "2020"},
            {"school": {"name": "tufts university", "website": "tufts.edu"}, "degrees": ["masters"], "end_date": "2022"},
        ]
    )
    fake = FakePDL({"Colby": [shared], "Tufts": [shared]})
    monkeypatch.setenv("PDL_API_KEY", "k")
    monkeypatch.setattr(cli, "_pdl", lambda settings, cache: fake)
    code = cli.main(["run", "--no-sheet", "--no-hunter", "--schools", "Colby,Tufts", "--output-csv", "out.csv"])
    assert code == 0
    rows = read_csv(env / "out.csv")
    assert len(rows) == 1
    assert rows[0]["School"] == "Colby" and rows[0]["Also Attended"] == "Tufts"


def test_quota_error_keeps_partial_results_and_reports_failure(env, monkeypatch, capsys):
    fake = FakePDL({"Amherst": [pdl_record()]}, fail_at="Bates")
    monkeypatch.setenv("PDL_API_KEY", "k")
    monkeypatch.setattr(cli, "_pdl", lambda settings, cache: fake)
    code = cli.main(["run", "--no-sheet", "--no-hunter", "--output-csv", "out.csv"])
    assert code == 2
    assert len(read_csv(env / "out.csv")) == 1
    assert "PDL stopped at Bates" in capsys.readouterr().out


def test_title_filter_drops_non_banking_roles(env, monkeypatch):
    records = [pdl_record(), pdl_record(id="t", full_name="Tom Teller", first_name="Tom", last_name="Teller",
                                        linkedin_url="linkedin.com/in/tom", job_title="Branch Manager")]
    monkeypatch.setenv("PDL_API_KEY", "k")
    monkeypatch.setattr(cli, "_pdl", lambda settings, cache: FakePDL({"Colby": records}))
    cli.main(["run", "--no-sheet", "--no-hunter", "--schools", "Colby", "--title-keywords", "investment banking",
              "--output-csv", "out.csv"])
    assert [r["Full Name"] for r in read_csv(env / "out.csv")] == ["Jane Doe"]


def test_nothing_to_do_without_keys_or_csv(env):
    assert cli.main(["run", "--no-sheet"]) == 1


def test_unknown_school_is_a_clean_error(env):
    assert cli.main(["links", "--schools", "Harvard"]) == 1


def test_links_command_writes_links_and_template(env):
    assert cli.main(["links", "--schools", "Colby"]) == 0
    links = read_csv(env / "output" / "linkedin_search_links.csv")
    assert {r["school"] for r in links} == {"Colby"}
    assert (env / "output" / "manual_alumni_template.csv").exists()
    assert SCHOOLS_BY_NAME["Colby"].linkedin_slug in links[0]["url"]


def test_rerun_against_the_sheet_keeps_user_edits_and_adds_new_people(env, monkeypatch):
    from alumni_finder import sheets

    from conftest import FakeSpreadsheet

    spreadsheet = FakeSpreadsheet()
    monkeypatch.setattr(sheets, "connect", lambda settings: None)
    monkeypatch.setattr(sheets, "open_spreadsheet", lambda client, settings: spreadsheet)
    monkeypatch.setenv("PDL_API_KEY", "k")

    monkeypatch.setattr(cli, "_pdl", lambda settings, cache: FakePDL({"Colby": [pdl_record(work_email=True)]}))
    assert cli.main(["run", "--no-hunter", "--schools", "Colby", "--output-csv", "out.csv"]) == 0
    master = spreadsheet.tabs["Alumni"]
    header = master.values[0]
    assert [r[header.index("Full Name")] for r in master.values[1:]] == ["Jane Doe"]

    # You fill in tracking columns, add your own column, and type an email you found.
    header.append("Coffee Chat")
    row = master.values[1] + ["10/3"]
    row[header.index("Status")] = "Emailed"
    row[header.index("Email")] = "jane@gs.com"
    master.values = [header, row]

    new_hire = pdl_record(id="s", full_name="Sam Lee", first_name="Sam", last_name="Lee",
                          linkedin_url="linkedin.com/in/samlee", work_email="sam.lee@gs.com")
    monkeypatch.setattr(cli, "_pdl", lambda settings, cache: FakePDL({"Colby": [pdl_record(), new_hire]}))
    assert cli.main(["run", "--no-hunter", "--schools", "Colby", "--output-csv", "out.csv"]) == 0

    header = master.values[0]
    rows = {r[header.index("Full Name")]: dict(zip(header, r)) for r in master.values[1:]}
    assert set(rows) == {"Jane Doe", "Sam Lee"}
    jane = rows["Jane Doe"]
    assert (jane["Status"], jane["Coffee Chat"]) == ("Emailed", "10/3")
    # The address you typed outranks the one PDL now returns.
    assert (jane["Email"], jane["Email Source"]) == ("jane@gs.com", "Manual")
    assert rows["Sam Lee"]["Email"] == "sam.lee@gs.com"


def test_group_column_is_filled_from_titles_including_manual_rows(env):
    manual = env / "manual.csv"
    manual.write_text(
        "school,bank,full_name,title,group\n"
        "Colby,Goldman Sachs,Jane Doe,Investment Banking Analyst - Healthcare,\n"
        "Colby,Evercore,Sam Lee,Associate,Restructuring\n"
    )
    assert cli.main(["run", "--no-sheet", "--no-pdl", "--no-hunter", "--manual-csv", str(manual), "--output-csv", "out.csv"]) == 0
    rows = {r["Full Name"]: r for r in read_csv(env / "out.csv")}
    assert (rows["Jane Doe"]["Division"], rows["Jane Doe"]["Group"]) == ("Investment Banking", "Healthcare")
    # A group you supply wins; division is still inferred (boutique + banker title).
    assert (rows["Sam Lee"]["Division"], rows["Sam Lee"]["Group"]) == ("Investment Banking", "Restructuring")


def test_links_with_custom_out_path_still_writes_the_template(env):
    assert cli.main(["links", "--banks", "elite boutiques", "--out", str(env / "elsewhere" / "links.csv")]) == 0
    assert (env / "output" / "manual_alumni_template.csv").exists()
    banks = {r["bank"] for r in read_csv(env / "elsewhere" / "links.csv")}
    assert "Evercore" in banks and "Goldman Sachs" not in banks
