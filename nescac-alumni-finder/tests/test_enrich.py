import dataclasses

import pytest

from alumni_finder import enrich
from alumni_finder.cache import Cache
from alumni_finder.config import BANKS_BY_NAME
from alumni_finder.enrich import EmailEnricher, HunterClient, apply_pattern
from alumni_finder.http import AuthError, QuotaExceeded, RateLimiter
from alumni_finder.models import Person

from conftest import FakeResponse, FakeSession


def person(first="Jane", last="Doe", bank="Goldman Sachs", **kw):
    return Person(full_name=f"{first} {last}", first_name=first, last_name=last, bank=bank, schools=["Colby"], **kw)


def hunter(responses, **kw):
    session = FakeSession(responses)
    return HunterClient("key", session=session, cache=kw.pop("cache", Cache.disabled()), limiter=RateLimiter(0), **kw), session


def found(email="jane.doe@gs.com", score=94, status="valid"):
    return FakeResponse(200, {"data": {"email": email, "score": score, "verification": {"status": status}}})


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("{first}.{last}", "jane.oneil@gs.com"),
        ("{f}{last}", "joneil@gs.com"),
        ("{first}", "jane@gs.com"),
        ("{first}_{middle}", ""),  # unknown placeholder
        ("", ""),
    ],
)
def test_apply_pattern(pattern, expected):
    assert apply_pattern(pattern, "Jane", "O'Neil", "gs.com") == expected


def test_find_email_sends_name_and_domain():
    client, session = hunter([found()])
    result = client.find_email("Jane", "Doe", "gs.com")
    assert result.email == "jane.doe@gs.com" and result.score == 94
    assert session.calls[0]["params"] == {"domain": "gs.com", "first_name": "Jane", "last_name": "Doe", "api_key": "key"}


def test_hunter_result_is_cached(tmp_path):
    cache = Cache(tmp_path / "c.sqlite3", 3600)
    client, _ = hunter([found()], cache=cache)
    client.find_email("Jane", "Doe", "gs.com")
    again, session = hunter([], cache=cache)
    assert again.find_email("Jane", "Doe", "gs.com").email == "jane.doe@gs.com"
    assert session.calls == [] and again.lookups == 0


def test_enricher_uses_hunter_and_records_confidence():
    client, _ = hunter([found()])
    p = person()
    EmailEnricher(client).enrich([p])
    assert (p.email, p.email_source, p.email_confidence) == ("jane.doe@gs.com", "hunter", "94 (valid)")


def test_people_with_emails_are_not_looked_up_again():
    client, session = hunter([])
    p = person()
    p.set_email("jane@gs.com", "pdl")
    EmailEnricher(client, guess_patterns=True).enrich([p])
    assert session.calls == []


def test_opted_out_person_gets_no_lookup_result_and_no_guess():
    client, _ = hunter([FakeResponse(451, {"errors": [{"id": "claimed_email"}]})])
    p = person()
    stats = EmailEnricher(client, guess_patterns=True).enrich([p])
    assert p.email == "" and p.email_opted_out and stats.opted_out == 1


def test_pattern_guess_is_opt_in_and_labeled(monkeypatch):
    bank = dataclasses.replace(BANKS_BY_NAME["Goldman Sachs"], email_pattern="{first}.{last}")
    monkeypatch.setitem(enrich.BANKS_BY_NAME, "Goldman Sachs", bank)

    p = person()
    EmailEnricher(None).enrich([p])
    assert p.email == "" and p.email_note == "Not looked up"

    p = person()
    EmailEnricher(None, guess_patterns=True).enrich([p])
    assert p.email == "jane.doe@gs.com" and p.email_source == "pattern"
    assert p.email_confidence.startswith("unverified")


def test_pattern_comes_from_hunter_when_not_configured():
    not_found = FakeResponse(200, {"data": {"email": None, "score": None}})
    pattern = FakeResponse(200, {"data": {"pattern": "{f}{last}"}})
    client, session = hunter([not_found, pattern])
    p = person()
    EmailEnricher(client, guess_patterns=True).enrich([p])
    assert p.email == "jdoe@gs.com" and p.email_source == "pattern"
    assert session.calls[1]["url"].endswith("/domain-search")


def test_quota_exhaustion_stops_hunter_but_not_the_run():
    client, session = hunter([FakeResponse(429, {"errors": [{"details": "usage limit"}]})] * 5)
    people = [person(), person("John", "Smith")]
    EmailEnricher(client).enrich(people)
    assert len(session.calls) == 5  # retried for the first person, then Hunter was switched off
    assert not client.available
    assert [p.email_note for p in people] == ["Not looked up", "Not looked up"]


def test_note_says_not_found_only_after_a_real_lookup():
    client, _ = hunter([FakeResponse(200, {"data": {"email": None}})])
    p = person()
    stats = EmailEnricher(client).enrich([p])
    assert p.email_note == "Not found" and stats.not_found == 1


def test_lookup_budget_is_enforced():
    client, session = hunter([found()], max_lookups=1)
    people = [person(), person("John", "Smith")]
    EmailEnricher(client).enrich(people)
    assert len(session.calls) == 1
    assert people[0].email and not people[1].email
    assert "budget" in client.disabled_reason


def test_bad_api_key_raises_auth_error():
    client, _ = hunter([FakeResponse(401, {"errors": [{"details": "No user found for the API key supplied"}]})])
    with pytest.raises(AuthError):
        client.find_email("Jane", "Doe", "gs.com")


def test_missing_key_is_rejected_up_front():
    with pytest.raises(AuthError):
        HunterClient("")


def test_quota_exceeded_is_raised_directly_from_client():
    client, _ = hunter([FakeResponse(402, {"errors": [{"details": "Upgrade your plan"}]})])
    with pytest.raises(QuotaExceeded):
        client.find_email("Jane", "Doe", "gs.com")
