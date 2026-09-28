"""Google Sheets output.

Layout:
  * "Alumni": one row per person. School and Bank are columns, rows are
    sorted School -> Bank -> last name, and there is a filter view per
    school and per bank, so the same table groups either way.
  * "Summary": live school x bank count matrix (COUNTIFS on the Alumni tab).
  * "LinkedIn Search Links": manual alumni-search links for gap-filling.

Re-runs merge with what's already in the sheet: Status, Notes and any
columns you add are kept, and people found earlier are never deleted.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

import gspread
from gspread.utils import ValueInputOption, rowcol_to_a1

from .config import Bank, School
from .linkedin_links import SearchLink
from .models import Person
from .table import BANK, COLUMNS, EMAIL, HIDDEN_COLUMNS, NAME, SCHOOL, build_rows, row_to_person

log = logging.getLogger(__name__)

MASTER_TAB = "Alumni"
SUMMARY_TAB = "Summary"
LINKS_TAB = "LinkedIn Search Links"
SCHOOL_VIEW_PREFIX = "By school: "
BANK_VIEW_PREFIX = "By bank: "
LINK_HEADERS = ["School", "Bank", "Keyword", "Open while logged in to LinkedIn (manual browsing only)"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


class SheetsConfigError(RuntimeError):
    pass


def connect(settings) -> gspread.Client:
    """Authorize with a service account or an OAuth desktop client."""
    mode = settings.google_auth_mode
    if mode == "service_account":
        path = Path(settings.google_service_account_file)
        if not path.exists():
            raise SheetsConfigError(
                f"Service-account key not found at {path}. See README 'Google Sheets setup', "
                "or set GOOGLE_AUTH_MODE=oauth to use your own Google account instead."
            )
        return gspread.service_account(filename=str(path), scopes=SCOPES, http_client=gspread.BackOffHTTPClient)
    if mode == "oauth":
        client_file = Path(settings.google_oauth_client_file)
        if not client_file.exists():
            raise SheetsConfigError(f"OAuth client file not found at {client_file}. See README 'Google Sheets setup'.")
        token_file = Path(settings.google_oauth_token_file)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        # First run opens a browser for consent; the token is saved for later runs.
        return gspread.oauth(
            scopes=SCOPES,
            credentials_filename=str(client_file),
            authorized_user_filename=str(token_file),
            http_client=gspread.BackOffHTTPClient,
        )
    raise SheetsConfigError(f"GOOGLE_AUTH_MODE must be 'service_account' or 'oauth', not {mode!r}")


def open_spreadsheet(client, settings):
    if settings.spreadsheet_id:
        return client.open_by_key(settings.spreadsheet_id)
    if settings.google_auth_mode == "service_account":
        # Service accounts have no Drive storage of their own, so they can't
        # create files. Create the sheet yourself and share it with them.
        raise SheetsConfigError(
            "SPREADSHEET_ID is not set. Create a blank Google Sheet, share it (Editor) with the "
            "service account's client_email, and put the ID from its URL in .env."
        )
    spreadsheet = client.create(settings.spreadsheet_title)
    log.info("Created spreadsheet %s. Add SPREADSHEET_ID=%s to .env to reuse it.", spreadsheet.url, spreadsheet.id)
    return spreadsheet


class SheetWriter:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def _worksheet(self, title: str, rows: int, cols: int, create: bool = True):
        try:
            return self.spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            if not create:
                return None
            return self.spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    def read_existing(self) -> tuple[list[Person], list[str]]:
        """People already in the Alumni tab, plus any extra columns the user added."""
        ws = self._worksheet(MASTER_TAB, 0, 0, create=False)
        if ws is None:
            return [], []
        values = ws.get_all_values()
        if not values:
            return [], []
        headers = [h.strip() for h in values[0]]
        if NAME not in headers or BANK not in headers:
            raise SheetsConfigError(
                f"The {MASTER_TAB!r} tab exists but has no {NAME!r}/{BANK!r} header. "
                "Rename that tab or point SPREADSHEET_ID at a different sheet."
            )
        people = [p for p in (row_to_person(headers, row) for row in values[1:]) if p]
        extras = [h for h in headers if h and h not in COLUMNS]
        return people, extras

    def write(
        self,
        people: Sequence[Person],
        extra_headers: Sequence[str],
        schools: Sequence[School],
        banks: Sequence[Bank],
        links: Sequence[SearchLink],
        updated_at: str,
    ) -> None:
        rows = build_rows(people, extra_headers)
        headers = rows[0]
        master = self._write_master(rows)
        summary = self._write_summary(schools, banks, headers, updated_at)
        links_ws = self._write_links(links)
        tabs = [(master, len(headers)), (summary, len(banks) + 3), (links_ws, len(LINK_HEADERS))]
        self._format(tabs, headers, people)
        self._drop_default_sheet()

    def _write_master(self, rows: list[list[str]]):
        ws = self._worksheet(MASTER_TAB, rows=len(rows) + 100, cols=len(rows[0]))
        ws.clear()
        ws.resize(rows=max(len(rows) + 100, 200), cols=len(rows[0]))
        # RAW so a scraped value like "=HYPERLINK(...)" is stored as text, never run as a formula.
        ws.update(values=rows, range_name="A1", value_input_option=ValueInputOption.raw)
        return ws

    def _write_summary(self, schools, banks, headers, updated_at):
        ws = self._worksheet(SUMMARY_TAB, rows=len(schools) + 10, cols=len(banks) + 4)
        school_col = _column_letter(headers.index(SCHOOL))
        bank_col = _column_letter(headers.index(BANK))
        email_col = _column_letter(headers.index(EMAIL))
        schools_range = f"'{MASTER_TAB}'!${school_col}:${school_col}"
        banks_range = f"'{MASTER_TAB}'!${bank_col}:${bank_col}"
        emails_range = f"'{MASTER_TAB}'!${email_col}:${email_col}"
        header = ["School \\ Bank", *[b.name for b in banks], "Total", "With Email"]
        values = [header]
        first_row, last_row = 2, len(schools) + 1
        last_bank_col = _column_letter(len(banks))
        for r, school in enumerate(schools, start=first_row):
            row = [school.name]
            for c in range(1, len(banks) + 1):
                row.append(f"=COUNTIFS({schools_range},$A{r},{banks_range},{_column_letter(c)}$1)")
            row.append(f"=SUM(B{r}:{last_bank_col}{r})")
            row.append(f'=COUNTIFS({schools_range},$A{r},{emails_range},"<>")')
            values.append(row)
        totals = ["Total"]
        for c in range(1, len(banks) + 3):
            col = _column_letter(c)
            totals.append(f"=SUM({col}{first_row}:{col}{last_row})")
        values.append(totals)
        values.append([])
        values.append([f"Last updated {updated_at}. Counts use each person's primary school; see 'Also Attended'."])
        ws.clear()
        ws.resize(rows=len(values) + 5, cols=len(header))
        ws.update(values=values, range_name="A1", value_input_option=ValueInputOption.user_entered)
        return ws

    def _write_links(self, links: Sequence[SearchLink]):
        rows = [LINK_HEADERS] + [[l.school, l.bank, l.keyword, l.url] for l in links]
        ws = self._worksheet(LINKS_TAB, rows=len(rows) + 5, cols=len(LINK_HEADERS))
        ws.clear()
        ws.resize(rows=len(rows) + 5, cols=len(LINK_HEADERS))
        ws.update(values=rows, range_name="A1", value_input_option=ValueInputOption.raw)
        return ws

    def _format(self, tabs, headers, people) -> None:
        """One batchUpdate: header styling, basic filter, filter views, hidden columns."""
        master = tabs[0][0]
        metadata = self.spreadsheet.fetch_sheet_metadata(
            params={"fields": "sheets(properties(sheetId,title),filterViews(filterViewId,title))"}
        )
        requests: list[dict] = []
        # Replace the filter views from the last run (the set of schools/banks may have changed).
        for sheet in metadata.get("sheets", []):
            for view in sheet.get("filterViews", []) or []:
                if view.get("title", "").startswith((SCHOOL_VIEW_PREFIX, BANK_VIEW_PREFIX)):
                    requests.append({"deleteFilterView": {"filterId": view["filterViewId"]}})

        for ws, cols in tabs:
            requests.append(_freeze_header(ws.id))
            requests.append(_bold_header(ws.id, cols))

        full_range = {"sheetId": master.id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": len(headers)}
        requests.append({"setBasicFilter": {"filter": {"range": full_range}}})

        school_idx, bank_idx, name_idx = headers.index(SCHOOL), headers.index(BANK), headers.index(NAME)
        for school in sorted({p.primary_school for p in people if p.primary_school}):
            requests.append(_filter_view(f"{SCHOOL_VIEW_PREFIX}{school}", full_range, school_idx, school, [bank_idx, name_idx]))
        for bank in sorted({p.bank for p in people if p.bank}):
            requests.append(_filter_view(f"{BANK_VIEW_PREFIX}{bank}", full_range, bank_idx, bank, [school_idx, name_idx]))

        for hidden in HIDDEN_COLUMNS:
            idx = headers.index(hidden)
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": master.id, "dimension": "COLUMNS", "startIndex": idx, "endIndex": idx + 1},
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                }
            )
        for ws, cols in tabs:
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": cols}
                    }
                }
            )
        self.spreadsheet.batch_update({"requests": requests})

    def _drop_default_sheet(self) -> None:
        """Remove the empty 'Sheet1' a new spreadsheet starts with."""
        try:
            default = self.spreadsheet.worksheet("Sheet1")
        except gspread.WorksheetNotFound:
            return
        if len(self.spreadsheet.worksheets()) > 1 and not any(any(r) for r in default.get_all_values()):
            self.spreadsheet.del_worksheet(default)


def _column_letter(index: int) -> str:
    return rowcol_to_a1(1, index + 1).rstrip("1")


def _freeze_header(sheet_id: int) -> dict:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }


def _bold_header(sheet_id: int, cols: int) -> dict:
    return {
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": cols},
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"bold": True},
                    "backgroundColor": {"red": 0.9, "green": 0.92, "blue": 0.96},
                }
            },
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }
    }


def _filter_view(title: str, grid_range: dict, column: int, value: str, sort_columns: list[int]) -> dict:
    return {
        "addFilterView": {
            "filter": {
                "title": title,
                "range": grid_range,
                "filterSpecs": [
                    {
                        "columnIndex": column,
                        "filterCriteria": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": value}]}},
                    }
                ],
                "sortSpecs": [{"dimensionIndex": c, "sortOrder": "ASCENDING"} for c in sort_columns],
            }
        }
    }
