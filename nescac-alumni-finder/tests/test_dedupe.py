from alumni_finder.dedupe import Deduper
from alumni_finder.models import Person


def person(name="Jane Doe", bank="Goldman Sachs", schools=("Colby",), **kw):
    first, last = name.split(" ", 1)[0], name.split(" ")[-1]
    return Person(full_name=name, first_name=first, last_name=last, bank=bank, schools=list(schools), **kw)


def test_same_linkedin_profile_is_merged_even_if_names_differ():
    d = Deduper()
    d.add(person("Jane Doe", linkedin_url="https://www.linkedin.com/in/janedoe/"))
    d.add(person("Janet Doe-Smith", linkedin_url="linkedin.com/in/JaneDoe"))
    assert len(d.people) == 1 and d.duplicates_merged == 1


def test_same_name_and_bank_is_merged_when_nothing_contradicts_it():
    d = Deduper()
    d.add(person("Jane Doe", sources={"manual"}))
    d.add(person("Jane Q. Doe", linkedin_url="linkedin.com/in/janedoe", sources={"pdl"}))
    assert len(d.people) == 1
    assert d.people[0].sources == {"manual", "pdl"}
    assert d.people[0].linkedin_url == "https://www.linkedin.com/in/janedoe"


def test_namesakes_with_different_profiles_stay_separate():
    d = Deduper()
    d.add(person("John Smith", bank="J.P. Morgan", linkedin_url="linkedin.com/in/john-smith-1"))
    d.add(person("John Smith", bank="J.P. Morgan", linkedin_url="linkedin.com/in/john-smith-2"))
    d.add(person("John Smith", bank="J.P. Morgan", pdl_id="x"))  # matches the first compatible one
    assert len(d.people) == 2


def test_same_name_at_different_banks_stays_separate():
    d = Deduper()
    d.add(person("Jane Doe", bank="Goldman Sachs"))
    d.add(person("Jane Doe", bank="Evercore"))
    assert len(d.people) == 2


def test_person_found_under_two_school_searches_keeps_both_schools():
    d = Deduper()
    d.add(person(schools=["Colby"], pdl_id="p1"))
    d.add(person(schools=["Bates"], pdl_id="p1"))
    assert d.people[0].schools == ["Colby", "Bates"]


def test_better_email_wins_and_manual_is_never_replaced():
    d = Deduper()
    first = person()
    first.set_email("guess@gs.com", "pattern")
    d.add(first)
    hunter = person()
    hunter.set_email("jane.doe@gs.com", "hunter", "95")
    d.add(hunter)
    assert d.people[0].email == "jane.doe@gs.com"

    manual = person()
    manual.set_email("jane@gs.com", "manual")
    d.add(manual)
    later = person()
    later.set_email("other@gs.com", "pdl")
    d.add(later)
    assert d.people[0].email == "jane@gs.com" and d.people[0].email_source == "manual"


def test_user_fields_survive_a_merge():
    d = Deduper()
    d.add(person(user_fields={"Status": "Emailed", "Notes": "coffee chat 10/3"}))
    d.add(person(title="Associate"))
    assert d.people[0].user_fields == {"Status": "Emailed", "Notes": "coffee chat 10/3"}
    assert d.people[0].title == "Associate"


def test_bank_change_moves_the_person():
    d = Deduper()
    d.add(person(bank="Goldman Sachs", linkedin_url="linkedin.com/in/janedoe"))
    d.add(person(bank="Evercore", linkedin_url="linkedin.com/in/janedoe"))
    assert len(d.people) == 1 and d.people[0].bank == "Evercore"
    d.add(person(bank="Evercore"))  # name + new bank now finds her
    assert len(d.people) == 1
