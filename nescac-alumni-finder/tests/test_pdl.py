import pytest

from alumni_finder.cache import Cache
from alumni_finder.config import BANKS, SCHOOLS_BY_NAME
from alumni_finder.http import ApiError, AuthError, QuotaExceeded, RateLimiter
from alumni_finder.sources.pdl import PAGE_SIZE, PDLSource, _request_body, build_query, parse_person

from conftest import FakeResponse, FakeSession, pdl_record

COLBY = SCHOOLS_BY_NAME["Colby"]


def make_source(responses, cache=None):
    session = FakeSession(responses)
    source = PDLSource("key", session=session, cache=cache or Cache.disabled(), limiter=RateLimiter(0))
    return source, session


def test_query_requires_school_and_bank():
    query = build_query(COLBY, BANKS)["query"]["bool"]["must"]
    school_clauses, company_clauses = query[0]["bool"]["should"], query[1]["bool"]["should"]
    assert {"terms": {"education.school.name": ["colby college"]}} in school_clauses
    assert {"terms": {"education.school.website": ["colby.edu"]}} in school_clauses
    assert "evercore" in company_clauses[0]["terms"]["job_company_name"]
    assert "gs.com" in company_clauses[1]["terms"]["job_company_website"]


def test_trinity_query_uses_domain_only():
    should = build_query(SCHOOLS_BY_NAME["Trinity"], BANKS)["query"]["bool"]["must"][0]["bool"]["should"]
    assert should == [{"terms": {"education.school.website": ["trincoll.edu"]}}]


def test_parse_person_orders_schools_bachelors_first():
    record = pdl_record(
        education=[
            {"school": {"name": "tufts university", "website": "tufts.edu"}, "degrees": ["masters"], "end_date": "2023"},
            {"school": {"name": "harvard university"}, "degrees": [], "end_date": "2015"},
            {"school": {"name": "colby college", "website": "colby.edu"}, "degrees": ["bachelors"], "end_date": "2021-05"},
        ]
    )
    person = parse_person(record, school_hint=SCHOOLS_BY_NAME["Tufts"])
    assert person.schools == ["Colby", "Tufts"]
    assert person.grad_year == "2021"
    assert person.bank == "Goldman Sachs"
    assert person.email == "jane.doe@gs.com" and person.email_source == "pdl"
    assert person.linkedin_url == "https://www.linkedin.com/in/janedoe"


def test_masked_free_plan_email_is_not_used():
    person = parse_person(pdl_record(work_email=True, location_name=True))
    assert person.email == ""
    assert "paid plan" in person.email_note
    assert person.location == ""


def test_free_plan_record_with_every_contact_field_masked():
    # Shape seen from PDL's free plan: whole contact fields are `true`, not lists.
    record = pdl_record(work_email=True, emails=True, personal_emails=True, phone_numbers=True, location_name=True)
    person = parse_person(record)
    assert person.full_name == "Jane Doe" and person.bank == "Goldman Sachs"
    assert person.email == "" and person.email_note == "PDL has one (paid plan shows it)"


def test_free_plan_records_survive_the_cache_round_trip(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", ttl_seconds=3600)
    masked = pdl_record(work_email=True, emails=True, personal_emails=True)
    source, _ = make_source([FakeResponse(200, {"data": [masked], "total": 1})], cache=cache)
    assert len(list(source.search(COLBY, BANKS))) == 1
    replay, _ = make_source([], cache=cache)
    assert [p.full_name for p in replay.search(COLBY, BANKS)] == ["Jane Doe"]


def test_one_unreadable_record_is_skipped_not_fatal(monkeypatch):
    import alumni_finder.sources.pdl as pdl

    real = pdl.parse_person

    def flaky(record, *args, **kwargs):
        if record["id"] == "bad":
            raise ValueError("unexpected shape")
        return real(record, *args, **kwargs)

    monkeypatch.setattr(pdl, "parse_person", flaky)
    records = [pdl_record(id="bad"), pdl_record(id="good", linkedin_url="linkedin.com/in/good")]
    source, _ = make_source([FakeResponse(200, {"data": records, "total": 2})])
    assert [p.pdl_id for p in source.search(COLBY, BANKS)] == ["good"]


def test_work_email_from_a_previous_employer_is_ignored():
    person = parse_person(pdl_record(work_email="jane@bain.com", emails=[{"address": "jane.doe@gs.com", "type": "professional"}]))
    assert person.email == "jane.doe@gs.com"
    assert parse_person(pdl_record(work_email="jane@bain.com")).email == ""


def test_non_target_employer_is_skipped():
    assert parse_person(pdl_record(job_company_name="bain & company", job_company_website="bain.com")) is None


def test_search_follows_scroll_token_until_total():
    page1 = FakeResponse(200, {"data": [pdl_record(), pdl_record(id="b", linkedin_url="linkedin.com/in/b")], "total": 3, "scroll_token": "t1"})
    page2 = FakeResponse(200, {"data": [pdl_record(id="c", linkedin_url="linkedin.com/in/c")], "total": 3, "scroll_token": "t2"})
    source, session = make_source([page1, page2])
    people = list(source.search(COLBY, BANKS))
    assert [p.pdl_id for p in people] == ["pdl-jane", "b", "c"]
    assert "scroll_token" not in session.calls[0]["json"]
    assert session.calls[1]["json"]["scroll_token"] == "t1"
    assert session.calls[0]["headers"]["X-Api-Key"] == "key"
    assert session.calls[0]["url"] == "https://api.peopledatalabs.com/v5/person/search"


def test_request_body_matches_the_official_sdk_shape():
    source, session = make_source([FakeResponse(200, {"data": [], "total": 0})])
    list(source.search(COLBY, BANKS))
    body = session.calls[0]["json"]
    assert set(body) == {"query", "size", "titlecase"}
    assert body["query"] == build_query(COLBY, BANKS)
    assert "bool" in body["query"]["query"]


def test_max_records_caps_page_size():
    source, session = make_source([FakeResponse(200, {"data": [pdl_record()], "total": 500, "scroll_token": "t"})])
    assert len(list(source.search(COLBY, BANKS, max_records=1))) == 1
    assert session.calls[0]["json"]["size"] == 1
    assert len(session.calls) == 1


def test_404_means_no_matches():
    source, _ = make_source([FakeResponse(404, {"status": 404, "error": {"message": "No records were found"}})])
    assert list(source.search(COLBY, BANKS)) == []


def test_rate_limit_is_retried_using_retry_after(no_sleep):
    ok = FakeResponse(200, {"data": [pdl_record()], "total": 1})
    source, session = make_source([FakeResponse(429, {}, {"Retry-After": "7"}), ok])
    assert len(list(source.search(COLBY, BANKS))) == 1
    assert len(session.calls) == 2
    assert 7 in no_sleep


def test_server_errors_are_retried_with_backoff(no_sleep):
    ok = FakeResponse(200, {"data": [], "total": 0})
    source, session = make_source([FakeResponse(503), FakeResponse(502), ok])
    assert list(source.search(COLBY, BANKS)) == []
    assert len(session.calls) == 3
    assert no_sleep[1] > no_sleep[0]


def test_persistent_rate_limit_raises_quota_exceeded():
    source, _ = make_source([FakeResponse(429, {})] * 6)
    with pytest.raises(QuotaExceeded):
        list(source.search(COLBY, BANKS))


@pytest.mark.parametrize("status,error", [(402, QuotaExceeded), (401, AuthError), (400, ApiError)])
def test_error_statuses(status, error):
    source, _ = make_source([FakeResponse(status, {"error": {"message": "nope"}})])
    with pytest.raises(error, match="nope"):
        list(source.search(COLBY, BANKS))


def test_cached_pages_are_replayed_without_spending_credits(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", ttl_seconds=3600)
    page = FakeResponse(200, {"data": [pdl_record()], "total": 1}, {"x-call-credits-spent": "1"})
    source, _ = make_source([page], cache=cache)
    assert len(list(source.search(COLBY, BANKS))) == 1
    assert source.credits_spent == 1

    replay, session = make_source([], cache=cache)  # any HTTP call would fail the test
    assert [p.full_name for p in replay.search(COLBY, BANKS)] == ["Jane Doe"]
    assert session.calls == [] and replay.credits_spent == 0


def test_count_uses_a_single_record():
    source, session = make_source([FakeResponse(200, {"data": [pdl_record()], "total": 42})])
    assert source.count(COLBY, BANKS) == 42
    assert session.calls[0]["json"]["size"] == 1


def test_cache_keeps_only_fields_the_tool_uses(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", ttl_seconds=3600)
    record = pdl_record(
        personal_emails=["jane@gmail.com"],
        phone_numbers=["+1 555 0100"],
        emails=[{"address": "jane@gmail.com", "type": "personal"}, {"address": "jane.doe@gs.com", "type": "professional"}],
    )
    source, _ = make_source([FakeResponse(200, {"data": [record], "total": 1})], cache=cache)
    list(source.search(COLBY, BANKS))
    stored = cache.get("pdl_search", _request_body(build_query(COLBY, BANKS), PAGE_SIZE))["data"][0]
    assert "personal_emails" not in stored and "phone_numbers" not in stored
    assert stored["emails"] == [{"address": "jane.doe@gs.com", "type": "professional"}]
    assert stored["education"][0]["school"]["name"] == "colby college"


def test_raising_the_cap_later_reuses_pages_already_bought(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", ttl_seconds=3600)
    first = FakeResponse(200, {"data": [pdl_record(id=str(i), linkedin_url=f"linkedin.com/in/p{i}") for i in range(PAGE_SIZE)], "total": 60, "scroll_token": "t1"})
    source, _ = make_source([first], cache=cache)
    assert len(list(source.search(COLBY, BANKS, max_records=PAGE_SIZE))) == PAGE_SIZE

    rest = FakeResponse(200, {"data": [pdl_record(id=f"x{i}", linkedin_url=f"linkedin.com/in/x{i}") for i in range(10)], "total": 60})
    uncapped, session = make_source([rest], cache=cache)
    assert len(list(uncapped.search(COLBY, BANKS))) == 60
    assert len(session.calls) == 1 and session.calls[0]["json"]["scroll_token"] == "t1"
