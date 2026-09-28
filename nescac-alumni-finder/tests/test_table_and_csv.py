from alumni_finder.models import Person
from alumni_finder.sources.manual_csv import load_manual_csv
from alumni_finder.models import NOTE_OPTED_OUT
from alumni_finder.table import COLUMNS, build_rows, headers_with_extras, person_to_row, row_to_person


def make(name, school, bank, **kw):
    first, last = name.split()
    return Person(full_name=name, first_name=first, last_name=last, bank=bank, schools=[school], **kw)


def test_rows_are_grouped_by_school_then_bank_then_group_then_last_name():
    people = [
        make("Zed Adams", "Colby", "Lazard"),
        make("Amy Young", "Bates", "Evercore"),
        make("Bob Brown", "Colby", "Evercore", group="M&A"),
        make("Al Able", "Colby", "Evercore"),
        make("Cy Cole", "Colby", "Evercore", group="Healthcare"),
    ]
    rows = build_rows(people)
    header = rows[0]
    assert header == COLUMNS
    cols = [header.index(c) for c in ("School", "Bank", "Group", "Full Name")]
    assert [tuple(r[i] for i in cols) for r in rows[1:]] == [
        ("Bates", "Evercore", "", "Amy Young"),
        ("Colby", "Evercore", "Healthcare", "Cy Cole"),
        ("Colby", "Evercore", "M&A", "Bob Brown"),
        ("Colby", "Evercore", "", "Al Able"),  # blank group sorts last within the bank
        ("Colby", "Lazard", "", "Zed Adams"),
    ]


def test_row_round_trip_keeps_user_columns_and_extras():
    p = make("Jane Doe", "Colby", "Goldman Sachs", pdl_id="abc", linkedin_url="linkedin.com/in/janedoe", sources={"pdl", "manual"})
    p.schools.append("Tufts")
    p.set_email("jane.doe@gs.com", "hunter", "94 (valid)")
    p.user_fields = {"Status": "Emailed", "Notes": "met at info session", "Coffee Chat": "10/3"}
    headers = headers_with_extras(["Coffee Chat"])
    row = person_to_row(p, headers)
    back = row_to_person(headers, row)
    assert back.schools == ["Colby", "Tufts"]
    assert back.email_source == "hunter" and back.email_confidence == "94 (valid)"
    assert back.pdl_id == "abc" and back.sources == {"pdl", "manual"}
    assert back.user_fields == {"Status": "Emailed", "Notes": "met at info session", "Coffee Chat": "10/3"}


def test_email_typed_into_the_sheet_counts_as_manual():
    headers = COLUMNS
    row = [""] * len(headers)
    row[headers.index("Full Name")] = "Jane Doe"
    row[headers.index("Bank")] = "Evercore"
    row[headers.index("Email")] = "jane@evercore.com"
    assert row_to_person(headers, row).email_source == "manual"


def test_opt_out_is_remembered_from_the_sheet():
    p = make("Jane Doe", "Colby", "Evercore")
    p.email_note = NOTE_OPTED_OUT
    back = row_to_person(COLUMNS, person_to_row(p, COLUMNS))
    assert back.email_opted_out


def test_manual_csv(tmp_path):
    path = tmp_path / "manual.csv"
    path.write_text(
        "School,Bank,Name,LinkedIn,Email,Class Year\n"
        "Colby College,Goldman,Jane Q. Doe,https://www.linkedin.com/in/janedoe,jane.doe@gs.com,2021\n"
        "Harvard,Evercore,John Smith,,,\n"
        "Bates,PJT,Sam Lee,,not-an-email,2019\n"
        ",,,,,\n"
    )
    people, warnings = load_manual_csv(path)
    assert [(p.primary_school, p.bank, p.first_name, p.last_name) for p in people] == [
        ("Colby", "Goldman Sachs", "Jane", "Doe"),
        ("Bates", "PJT Partners", "Sam", "Lee"),
    ]
    assert people[0].email_source == "manual" and people[0].grad_year == "2021"
    assert people[1].email == ""
    assert any("unknown school 'Harvard'" in w for w in warnings)
    assert any("invalid email" in w for w in warnings)


def test_hand_typed_school_and_bank_names_are_normalized():
    row = [""] * len(COLUMNS)
    row[COLUMNS.index("School")] = "colby college"
    row[COLUMNS.index("Bank")] = "GS"
    row[COLUMNS.index("Also Attended")] = "Tufts University"
    row[COLUMNS.index("Full Name")] = "Jane Doe"
    p = row_to_person(COLUMNS, row)
    assert (p.schools, p.bank) == (["Colby", "Tufts"], "Goldman Sachs")
