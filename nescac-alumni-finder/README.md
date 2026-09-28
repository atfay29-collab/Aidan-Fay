# NESCAC Alumni → Investment Banks finder

Finds alumni of the 11 NESCAC schools who currently work at bulge-bracket banks
and elite boutiques, looks up their work emails, and writes everything to a
Google Sheet grouped by school and by bank.

- **Schools:** Amherst, Bates, Bowdoin, Colby, Connecticut College, Hamilton,
  Middlebury, Trinity, Tufts, Wesleyan, Williams
- **Bulge bracket:** Goldman Sachs, Morgan Stanley, J.P. Morgan, Bank of America,
  Citi, Barclays, UBS / Credit Suisse
- **Elite boutiques:** Evercore, Lazard, Centerview, Moelis, PJT Partners,
  Perella Weinberg, Houlihan Lokey, Qatalyst

It does **not** scrape LinkedIn. The next section explains why, and what it does instead.

---

## 1. The approach, and why

### The constraint

LinkedIn's User Agreement (§8.2) bans scraping and "bots or other automated
methods", including on your own logged-in account. LinkedIn enforces this. It
restricts or bans accounts, and it sues. In January 2025 it sued Proxycurl, the
best-known LinkedIn data API. Proxycurl shut down in July 2025 and agreed to a
permanent injunction. LinkedIn also never shows email addresses in search results or
profiles, so "scrape LinkedIn for emails" isn't possible even in principle.

### Options considered

| Option | Can it filter by school **and** current bank? | Emails? | Cost | Ban / legal risk | Verdict |
|---|---|---|---|---|---|
| LinkedIn official APIs (Recruiter System Connect, Talent Solutions) | Only inside LinkedIn Recruiter | No | Enterprise contract, partner approval | None | **Not available to individuals.** Only approved ATS partners get access. |
| Proxycurl | Yes | Some | — | — | **Gone.** Shut down July 2025 after LinkedIn's suit. |
| PhantomBuster, or Selenium/Playwright on your own session | Yes, via LinkedIn's UI | No | Paid subscription, or free (DIY) | **High**: it automates *your* account, which is exactly what §8.2 bans | Rejected. Losing your LinkedIn account in the middle of recruiting season costs far more than the data is worth. |
| Apollo.io | School filter exists in the web app, but isn't a documented filter in its People Search API | Yes, through a separate enrichment call | Free tier, then paid | Low | Weak fit: you'd have to pull every employee of J.P. Morgan and filter locally. |
| **People Data Labs (PDL) Person Search API** | **Yes, in one query** (`education.school` + `job_company_*`) | Work emails on **paid** plans (masked as true/false on the free plan) | Free: 100 credits/mo. Paid from ~$100/mo (check current pricing). 1 credit per record returned. | Low: licensed B2B dataset; your LinkedIn account isn't involved | **Chosen as the main source.** |
| **Hunter.io Email Finder** | n/a (name + domain → email) | **Yes**, with a confidence score and verification status | Small free monthly allowance, then paid | Low | **Chosen for email enrichment.** |
| **LinkedIn's alumni tool, browsed by hand** (`linkedin.com/school/<school>/people/?keywords=<bank>`) | Yes, and the most up to date | No | Free | None if you really browse by hand | **Chosen as a free supplement.** The tool generates the links; you browse and paste names into a CSV. |

### What gets built

```
People Data Labs search ─┐                       ┌─ Hunter.io Email Finder
 (school AND bank)        ├─► de-duplicate ─► emails ─┤  (name + bank domain)
Manual CSV from           │   (+ merge with         └─ optional pattern guess
 LinkedIn alumni tool ────┘    existing sheet rows)      (labeled "unverified")
                                      │
                                      ▼
                    Google Sheet (+ local CSV backup)
```

### Requirements that can't be met as written

- **Bulk email extraction from LinkedIn isn't achievable.** LinkedIn doesn't
  expose emails. The closest alternative is in the tool, in this order:
  1. PDL's `work_email`, accepted only if it's at the person's *current* bank's
     domain (needs a paid PDL plan).
  2. Hunter.io Email Finder, keyed on first name + last name + bank domain.
  3. Opt-in `--guess-emails`: applies the bank's address pattern (from Hunter,
     or one you set in `config.py`), for example `first.last@gs.com`. These
     rows are labeled **"Pattern guess (unverified)"**.
  
  If someone has asked Hunter to stop processing their data (HTTP 451), the
  tool records that and does not guess an address for them either.
- **Freshness.** PDL's data is compiled and can lag real job changes by months.
  LinkedIn's alumni tool is the most current, so use the links tab to check the
  people you actually plan to contact.
- **Coverage.** No provider has everyone. Expect PDL to miss some people,
  especially recent grads. The manual CSV path fills the gaps.

---

## 2. Sheet structure, and why

**One master table** (`Alumni` tab) with **School** and **Bank** as columns,
sorted **School → Bank → last name**. On top of it:

- **Filter views**: one per school (`By school: Colby`, sorted by bank) and one
  per bank (`By bank: Goldman Sachs`, sorted by school). Open them from
  *Data → Filter views*. Each is one click and doesn't change what others see.
- **`Summary` tab**: a school × bank count matrix, plus Total and With Email
  columns. It uses live `COUNTIFS` formulas, so it stays correct when you edit
  the Alumni tab.
- **`LinkedIn Search Links` tab**: the manual alumni-search link for every
  school × bank.

**Why not one tab per school?** Tab-per-school handles only one grouping. "Everyone at
Evercore across all schools" would mean opening 11 tabs, and summary math has to
span tabs. A single table with both dimensions as columns groups either way
without duplicating data. The sort order still reads "school, then bank within
school" top to bottom.

Columns: `School, Bank, Bank Type, Full Name, Title, Email, Email Source,
Email Confidence, LinkedIn URL, Location, Grad Year, Also Attended, Found Via,
Last Seen, Status, Notes` (plus a hidden `Source ID`).

**Re-runs are safe.** `Status`, `Notes` and any columns you add are never
overwritten. An email you type in yourself counts as "Manual" and always
outranks what an API returns. People who drop out of the search results are
kept, not deleted. The `Last Seen` date shows how recently each was confirmed.

---

## 3. Setup

Requires Python 3.10+.

```bash
cd nescac-alumni-finder
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill it in as below
```

### 3a. People Data Labs

1. Sign up at <https://dashboard.peopledatalabs.com/> (the free plan works
   without a card).
2. Copy your API key into `.env` as `PDL_API_KEY`.
3. **Plan note:** on the free plan, names, titles, employers and LinkedIn URLs
   come back, but emails are masked as `true`/`false`. The sheet then shows
   "PDL has one (paid plan shows it)", and Hunter fills in emails instead. A
   paid plan unmasks work emails.

### 3b. Hunter.io

1. Sign up at <https://hunter.io/> → *API* → copy the key into `.env` as
   `HUNTER_API_KEY`.
2. Lookups count against your monthly Hunter quota. Cap a run with
   `--max-hunter-lookups N`. Every result, including "not found", is cached
   for 30 days, so re-runs don't repeat lookups.

### 3c. Google Sheets: pick one

**Which one do you have set up?** Both are supported. Set `GOOGLE_AUTH_MODE` in `.env`.

**Option A: service account (recommended for a script)**

1. In <https://console.cloud.google.com/>, create or select a project. Enable
   the **Google Sheets API** (and the **Google Drive API**).
2. *IAM & Admin → Service Accounts → Create*. Open it → *Keys → Add key → JSON*.
   Save the file as `credentials/service_account.json`.
3. Create a blank Google Sheet yourself. Click **Share** and add the service
   account's `client_email` (from the JSON file) as **Editor**. Service accounts
   have no Drive storage of their own, so they can't create the sheet for you.
4. Put the sheet's ID (the long string in
   `docs.google.com/spreadsheets/d/<ID>/edit`) in `.env` as `SPREADSHEET_ID`.
5. `GOOGLE_AUTH_MODE=service_account`

**Option B: OAuth as your own Google account**

1. In the Cloud Console, enable the Google Sheets API and Google Drive API.
   Configure the OAuth consent screen (External; add yourself as a test user).
2. *Credentials → Create credentials → OAuth client ID → Desktop app*.
   Download the JSON as `credentials/oauth_client.json`.
3. `GOOGLE_AUTH_MODE=oauth`. On the first run a browser opens for consent, and
   the token is saved to `credentials/authorized_user.json`.
4. `SPREADSHEET_ID` is optional here. If it's blank, a new sheet is created in
   your Drive, and its ID is printed so you can add it to `.env`.

> **Colby Google Workspace note:** if your `@colby.edu` account blocks
> third-party OAuth apps, use a personal Gmail account for Option B, or use
> Option A.

---

## 4. Usage

```bash
# 1) See how many profiles PDL has (costs <= 1 credit per school)
python -m alumni_finder estimate

# 2) Small test run: two schools, local CSV only
python -m alumni_finder run --schools Colby,Bates --no-sheet

# 3) Full run into the Google Sheet (default cap: 50 PDL records per school)
python -m alumni_finder run

# Useful flags
python -m alumni_finder run --banks "Goldman Sachs,Evercore,Centerview"
python -m alumni_finder run --max-per-school 0            # no cap (watch your credits)
python -m alumni_finder run --title-keywords "investment banking,M&A,capital markets,restructuring,leveraged finance"
python -m alumni_finder run --guess-emails                 # fill gaps with labeled pattern guesses
python -m alumni_finder run --max-hunter-lookups 25        # stay inside Hunter's free tier
```

**Manual gap-filling (free, no ban risk):**

```bash
python -m alumni_finder links     # writes output/linkedin_search_links.csv + a CSV template
```

Open the links **yourself** in a browser where you're logged in to LinkedIn.
Add the people you find to `output/manual_alumni_template.csv` (columns:
`school, bank, full_name, title, linkedin_url, email, location, grad_year`;
only the first three are required). Then:

```bash
python -m alumni_finder run --manual-csv output/manual_alumni_template.csv
```

Manual rows go through the same de-duplication and Hunter email lookup.
Add `--no-pdl` to process only the CSV.

Every run also writes `output/alumni_latest.csv`, so a Sheets outage never
loses results.

---

## 5. Reliability details

**Rate limits and failures**
- PDL's Search API allows about 10 requests per minute, so calls are spaced
  6.5 s apart (configurable).
- HTTP 429 and 5xx responses, and network errors, are retried with exponential
  backoff plus jitter. `Retry-After` is honored when the server sends it.
- **PDL:** a 404 means "no (more) matches" and isn't treated as an error.
  - 402 (out of credits) stops PDL for the run but keeps and writes everything
    found so far.
  - 401/403 aborts before anything is written.
  - An error on one school is logged, and the run continues with the next
    school.
- **Hunter:** a quota or rate limit that persists after retries switches Hunter
  off for the rest of the run; the run itself continues.
  - A 451 is recorded as an opt-out.
  - A failed lookup is counted and skipped.
- **Google:** calls go through gspread's back-off client. The tool connects to
  Google *before* spending any API credits, so a Sheets setup mistake costs
  nothing.
- The run summary lists every failure. The exit code is 2 when anything failed.

**Caching.**
- Every PDL page and Hunter lookup is stored in `.cache/alumni_finder.sqlite3`
  for 30 days, keyed by the exact request.
- If a run crashes halfway, rerunning replays the pages you already paid for,
  and raising `--max-per-school` later reuses the pages already bought.
- Only the fields the tool uses are cached. Personal emails and phone numbers
  are dropped.
- `--refresh-cache` forces fresh data.

**De-duplication.** Two records are the same person if they share:
1. a LinkedIn profile URL, after normalizing (`uk.linkedin.com/in/JaneDoe/?trk=…`
   and `linkedin.com/in/janedoe` match); or
2. a PDL record ID; or
3. the same first and last name at the same bank (ignoring middle names, case,
   accents and suffixes like "CFA"), **unless** the two records carry different
   LinkedIn URLs or PDL IDs. That case is two people who share a name.

This covers:
- someone who attended two NESCAC schools. They show up in two school searches
  and become one row, with the other school under `Also Attended`. Their
  primary school is where they earned a bachelor's, if known.
- a manual CSV row that duplicates a PDL result.
- re-runs against rows already in the sheet.

When records merge, the better email wins: Manual > PDL > Hunter > pattern guess.

---

## 6. Customizing

Everything is in `alumni_finder/config.py`:
- **Add a bank:** name, category, PDL company names, website domains, email
  domain, LinkedIn keywords.
- **Set a known email pattern:** `email_pattern="{first}.{last}"`. Supported
  placeholders are `{first}`, `{last}`, `{f}` and `{l}`.
- **Fix a LinkedIn school slug:** do this if a link from the `links` command
  lands on the wrong school page. `trinity-college-hartford`, `colby-college`
  and `bowdoin-college` have been checked.
- **Trinity** is matched only on its domain (`trincoll.edu`). The name "Trinity
  College" also covers Dublin, Cambridge and others.

## 7. Using the results responsibly

- Send individual, personalized notes. Don't mass-mail.
- Treat pattern guesses as guesses; a bounce is worse than no email.
- Respect opt-outs, and people who don't reply.
- For people based in the UK or EU (e.g. Barclays London), GDPR applies to how you store
  and use their data.
- `.env`, `credentials/`, `.cache/` and `output/` are git-ignored. Keep it that way.

## 8. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests mock every HTTP call, so they never touch PDL, Hunter or Google. They
cover:
- request formats
- pagination and caching
- retry and backoff, and quota handling
- email ranking
- de-duplication
- sheet layout and formulas, including that re-runs keep your edits
