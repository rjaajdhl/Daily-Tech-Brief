# Notion workspace schema

This is what the pipeline expects to find in Notion. **Nothing in this repository creates it.** Build it by hand, or let
your agent build it from this page, then put the ids in `config/notion_ids.json`. The tables were read from a working
workspace (Notion API version `2026-03-11`, which uses *data sources*) and cross-checked against the property names the
shipped code reads and writes. Property names are matched **exactly** (case and spacing).

## What you need (and what you do not)

| Object | Needed? | Notes |
|---|---|---|
| **Daily Briefs** database | yes | one row (the "Index" page) per date |
| **Daily Brief Sections** database | yes | one row per date and section; the page body holds the articles |
| **Dashboard** page | yes | an ordinary page; the Overview page and the Sections view are created under it on the first run |
| **`Today — Daily Brief`** linked view of Daily Briefs, placed on the Dashboard page | yes | must exist before the first run; the code only updates its date filter |
| Story archive, Watchlist, Feedback inbox, Deep-dive queue databases | **no** | an earlier version used them; no shipped code reads the `story_*`, `watchlist_*`, `feedback_*` or `deep_dive_*` ids |

## Ids file

`config/notion_ids.json` (copy `config/notion_ids.example.json`). A database id and its data-source id are different values;
the Notion API returns both when you create or retrieve a database. Only the keys below are used; blank the rest.

| Key | Points to |
|---|---|
| `daily_database_id`, `daily_data_source_id` | Daily Briefs database |
| `daily_sections_database_id`, `daily_sections_data_source_id` | Daily Brief Sections database |
| `dashboard_page_id` | the Dashboard page |
| `dashboard_page_title` | exact title of the Dashboard page (default `Technology Daily Dashboard`); the code refuses to write if the title differs |
| `dashboard_daily_view_id` | the linked view named exactly `Today — Daily Brief` (em dash) on the Dashboard, data source = Daily Briefs |
| `dashboard_overview_page_id` | leave empty; written back by the first run (child page titled `Technology Daily Overview`) |
| `dashboard_sections_view_id` | leave empty; the first run creates a view named `Today — Brief Sections` on the Dashboard and writes the id back |

`config/notion_write_scope.json`: put both database ids in `allowed_write_database_ids`, the Dashboard page id in
`allowed_write_parent_page_ids` and the Daily view id in `allowed_write_view_ids`. Anything not listed is read-only to the
pipeline (`work/scope_guard.py`), so a missing entry fails loudly as `SCOPE_BLOCKED:...` instead of writing somewhere else.

Share both databases and the Dashboard page with your Notion integration.

## Database 1 - Daily Briefs

One page per date, created by `find_or_create_index` in `work/v3/publish_notion.py`. Its body is the "At a glance" summary
plus links to the section pages.

| Property | Type | Options | Note |
|---|---|---|---|
| `Name` | title |  | title; `YYYY-MM-DD · Daily Tech Intelligence Brief` |
| `Date` | date |  | the brief (run) date; every lookup filters on it |
| `Signal Level` | select | High, Medium, Low, Developing | not read or written by the shipped code |
| `Stage` | select | DRAFT, FINAL | **required** |
| `Top Topics` | multi_select | AI, Agents, Security, Local Models, Infrastructure, Semiconductors, Musk-related, Cybersecurity, Developer Tools, Elon Musk, Research, Open Models, Applied Technology, Community, Business | optional; written only if the column exists |
| `Reddit Included` | checkbox |  | optional |
| `Brief Status` | select | Draft, Published, Reviewed, Incomplete Research | **required** |
| `Research Included` | checkbox |  | optional |
| `Watchlist Changes` | number |  | optional (always 0) |
| `Quality Note` | rich_text |  | optional |
| `Review Needed` | checkbox |  | optional |
| `Major Story Count` | number |  | optional (always 0) |

`Name`, `Date`, `Stage` and `Brief Status` are needed to create the page. The metadata update at the end skips any column that
does not exist, so the optional ones may be left out.

## Database 2 - Daily Brief Sections

One page per date and section, built by `work/daily_brief_sections.py`. Every column below is written on creation, so all must exist.

| Property | Type | Options | Note |
|---|---|---|---|
| `Name` | title |  | title; `YYYY-MM-DD · Brief Section NN` |
| `Date` | date |  | **required**; same date as the Index |
| `Section Name` | rich_text |  | **required** |
| `Quality Note` | rich_text |  | **required** |
| `Review Needed` | checkbox |  | **required** |
| `Parent Brief ID` | rich_text |  | **required**; page id of the Index page |
| `Section Order` | number |  | **required**; 1-12 |
| `Coverage Status` | select | PASS, PASS_NO_MATERIAL_EVENT, PARTIAL, UNAVAILABLE | **required** |
| `Issue Count` | number |  | **required** |
| `Brief Status` | select | Draft, Published, Incomplete Research, Reviewed | **required** |
| `Stage` | select | DRAFT, FINAL | **required** |
| `Section Key` | rich_text |  | **required**; machine key, see the table below |

Section keys and order (the page title uses the order number):

| Order | `Section Key` | Heading used by the live workspace |
|---|---|---|
| 1 | `ai` | 01. AI, Agents & Frontier Models |
| 2 | `security` | 02. Cybersecurity & Offensive Security |
| 3 | `local_models` | 03. Local & Open Models |
| 4 | `infrastructure` | 04. Infrastructure, Cloud & Compute |
| 5 | `semiconductors` | 05. Semiconductors & AI Infrastructure |
| 6 | `developer_tools` | 06. Developer Tools, Open Source & GitHub |
| 7 | `research` | 07. Research, Papers & Benchmarks |
| 8 | `outside_bubble` | 08. Robotics, Applied Technology & Open Discovery |
| 9 | `community` | 09. Community Signals & Early Radar |
| 10 | `business` | 10. Technology Business & Governance |
| 11 | `review_failed` | 11. Review Failed (articles that failed the fact check, kept with the reason) |
| 12 | `followup` | 12. Follow-up (written by the 11:00 feedback job, not by the daily run) |

Empty sections are still published (`Issue Count` 0). When a date is re-published, the old section pages are not deleted:
their `Section Key` is prefixed `history:` and they are taken out of navigation.

## Page bodies (what the code writes)

**Index page** - `# Daily Tech Brief — <date>`, a `Published: ... · Evidence date: ...` line, `## 00. At a glance` (summary,
"Key developments" bullets, "Why it matters", "Coverage"), then `## Topic Sections` with one bullet per section
(`NN. Name — K issues`) linking to the section page.

**Section page** - `# <section heading>`, a `Published: ... · N issues` line, then one **`to_do` block per article**, unchecked:
`<NN-id> · <title>`. Children of each block, in order:

1. a lead paragraph;
2. a `toggle` titled `▶ Details` holding the body paragraphs, a `Glossary` paragraph with bullets (`term: definition`) and a
   `Sources` paragraph with bullets (`domain · title`, linked).

The unchecked box is deliberate. Ticking an article is the feedback signal that the 11:00 job (`scripts/interest_collect.py`)
reads from the Sections database. Nothing in the daily run depends on it.

**Overview page** (`Technology Daily Overview`, child of the Dashboard) - heading, a counts line, `## At a glance`, bullets and a
`## Browse by section` list. Created on the first run and rewritten every run.

**Dashboard page** - free-form. The code rewrites its body to a short header plus links, and the `Today — Brief Sections` view
sits under it. Add whatever navigation you like around it, but keep the title in `dashboard_page_title`.

## Optional: feedback and My Interest pages

`scripts/setup_my_interest.py` creates `06. Research Controls` / `06.01. My Interest` under the page given by
`TI_NOTION_ROOT_PAGE_ID` (usually the page that contains the Dashboard). It needs no databases. Its write scope is
`config/notion_write_scope_followup.json`.

## Build checklist for an agent

1. Create the two databases with exactly the properties above. Select options may be pre-created; Notion also adds unknown
   options when a page is written.
2. Create the Dashboard page, and on it the `Today — Daily Brief` linked view of Daily Briefs.
3. Fill `config/notion_ids.json` and `config/notion_write_scope.json`; share everything with the integration.
4. Run `python work/v3/run_daily.py --no-publish`, then a real run for one past date, and compare the Notion pages with
   "Page bodies" above. `data/v3/<date>/notion_publish_readback.json` lists every check and any warning.

*Status: this page describes a working workspace and the code that uses it. It has not yet been used to build a workspace from
scratch. If a step fails, trust the property names in the code over this page, and fix the page.*
