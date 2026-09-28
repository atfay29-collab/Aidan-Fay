import json
import sys
from pathlib import Path

import gspread
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Replays queued responses and records every request."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Retries and throttling never actually wait in tests."""
    slept = []
    monkeypatch.setattr("alumni_finder.http.time.sleep", lambda s: slept.append(s))
    return slept


def pdl_record(**overrides):
    record = {
        "id": "pdl-jane",
        "full_name": "Jane Doe",
        "first_name": "Jane",
        "last_name": "Doe",
        "job_title": "Investment Banking Analyst",
        "job_company_name": "goldman sachs",
        "job_company_website": "goldmansachs.com",
        "linkedin_url": "linkedin.com/in/janedoe",
        "location_name": "New York, New York, United States",
        "work_email": "jane.doe@gs.com",
        "education": [
            {
                "school": {"name": "colby college", "website": "colby.edu"},
                "degrees": ["bachelors"],
                "end_date": "2021",
            }
        ],
    }
    record.update(overrides)
    return record


class FakeWorksheet:
    _next_id = 100

    def __init__(self, title, values=None):
        FakeWorksheet._next_id += 1
        self.id = FakeWorksheet._next_id
        self.title = title
        self.values = values or []
        self.updates = []

    def get_all_values(self):
        return self.values

    def clear(self):
        self.values = []

    def resize(self, rows=None, cols=None):
        pass

    def update(self, values, range_name, value_input_option):
        self.values = values
        self.updates.append((range_name, str(value_input_option)))


class FakeSpreadsheet:
    url = "https://docs.google.com/spreadsheets/d/fake"

    def __init__(self, tabs=None, filter_views=()):
        self.tabs = {t.title: t for t in (tabs or [])}
        self.filter_views = list(filter_views)
        self.batches = []

    def worksheet(self, title):
        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet(title)
        return self.tabs[title]

    def del_worksheet(self, ws):
        del self.tabs[ws.title]

    def fetch_sheet_metadata(self, params=None):
        return {"sheets": [{"properties": {"sheetId": 1, "title": "Alumni"}, "filterViews": self.filter_views}]}

    def batch_update(self, body):
        self.batches.append(body)
