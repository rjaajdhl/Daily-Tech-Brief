---
name: daily-tech-brief
description: "Use when setting up or running the Daily Tech Brief pipeline. Collects, selects, writes and publishes a daily tech brief to Notion."
version: 0.1.0
author: Daily Tech Brief contributors
license: MIT
platforms: [macos, linux]
metadata:
  hermes:
    tags: [Technology, Intelligence, News, Notion, Pipeline, Cron]
---

# Daily Tech Brief

A pipeline that, once a day, collects the previous day's technology news from ~65 configured sources, groups the
articles into events, scores and selects them, writes one article per event from its source text, fact-checks the
article, and publishes a dated brief to Notion. This skill tells you how to install it for the user, run it, schedule
it, and read the result. The code is in this repository (`work/`, `scripts/`, `config/`); the skill does not contain it.

**Status of these instructions:** the exported tree was checked to compile and to import with `TI_ROOT` pointing at an
empty directory. It has not been run end to end on a clean machine. Say so to the user and report real output of each step;
do not claim a step worked unless you saw it work.

## What the user must provide (ask; never invent or guess)

1. A **Notion integration token** (`NOTION_API_KEY`) and a workspace laid out as `config/notion_ids.example.json` describes
   (Daily database, Sections database, Dashboard page, feedback and queue databases). Nothing in this repo creates that
   workspace. If it does not exist, stop and tell the user what is missing.
2. Hermes with a working **Codex/ChatGPT OAuth login**. `work/v3/llm.py` gets its credential from `hermes_cli.auth`.
3. **Ollama** with `bge-m3` (`ollama pull bge-m3`) on `http://localhost:11434`.
4. A **Crawl4AI** server and a **SearXNG** instance (URLs and token for `.env`).
5. Their own **interests**: the scoring rubric `SELECT_SYS` in `work/v3/pipeline.py` and `config/source_registry.json`
   describe one specific reader. Ask what the user cares about and edit those before the first real run.

## Install

```bash
git clone https://github.com/rjaajdhl/Daily-Tech-Brief.git ~/daily-tech-brief && cd ~/daily-tech-brief
export TI_ROOT="$PWD"
cp .env.example .env                                                       # then fill in the values the user gave you
cp config/notion_ids.example.json config/notion_ids.json                   # fill in the user's Notion ids
cp config/notion_write_scope.example.json config/notion_write_scope.json   # allow-list: only these Notion targets may be written
python3 -m pip install -r requirements.txt
```

Never commit or print `.env`, `config/notion_ids.json`, `config/notion_write_scope.json`. They are git-ignored; keep it that way.

## Check before the first run

`work/v3/preflight.py` checks the LLM credential, Notion access, and the embedding server (it starts Ollama if it is down).
Notion is **required even with `--no-publish`**, because preflight checks it.

```bash
python3 work/v3/run_daily.py --no-publish        # full pipeline for yesterday, no Notion write
```

Read `data/v3/<date>/run_report.json`: every stage has `status`, `seconds`, `note`. A stage marked `ok` only means it
finished. Open `articles.jsonl` and read a few articles before you tell the user the output is good.

## Run

```bash
python3 work/v3/run_daily.py                          # target = yesterday (America/Los_Angeles), publishes to Notion
python3 work/v3/run_daily.py --date 2026-10-13        # a specific day
python3 work/v3/run_daily.py --date 2026-10-13 --force-from publish   # redo from a stage onward
```

Finished stages are skipped on a re-run. A full day takes roughly 1.5-2 hours with an OAuth-backed model, mostly extract and write.

## Schedule with Hermes cron

`scripts/v3_daily.py` is the cron entry point: it locks so runs cannot overlap, retries once after 10 minutes, and posts an
outage notice if the retry also fails. Hermes cron only runs scripts that resolve **inside `$HERMES_HOME/scripts/`**, so put a
small wrapper there instead of pointing the job at the repo:

```bash
cat > "$HERMES_HOME/scripts/daily_tech_brief.sh" <<'EOF'
#!/bin/bash
export TI_ROOT="$HOME/daily-tech-brief"
exec python3 "$TI_ROOT/scripts/v3_daily.py"
EOF
chmod +x "$HERMES_HOME/scripts/daily_tech_brief.sh"
```

Then create the job with the cronjob tool: `script="daily_tech_brief.sh"`, `no_agent=true` (the script is the job, no LLM runs
for the schedule itself), `schedule="0 4 * * *"`. Run it once by hand first. Hermes delivers the script's stdout, which is the
stage table, so the user sees failures without opening logs.

## Reading a failure

* `FAILED at stage <x>` in the table: open `data/v3/<date>/run_daily.log` and read the last traceback. Fix the cause, then
  `--force-from <x>`.
* **Do not report success from the cron status alone.** After a publish, read the Notion page back (exact date, section
  counts, `Stage`/`Brief Status`). A scheduler `ok` does not prove the page exists.
* A day with no publishable articles still publishes (an honest empty page); a day that cannot run publishes an outage notice.

## Pitfalls

* Notion rewrites some URLs on save (adds a trailing `/`, or `=` to a valueless query parameter). Do not treat that as a content error.
* Ollama off means embedding fails with `Connection refused`; preflight starts it, but only if the `ollama` binary is in a known path.
* The writer only uses text retained from the sources. Do not "improve" an article by adding facts the sources do not contain.
* Changing the scoring rubric changes what is published. Make the change, replay a past day (`--date <old day> --force-from select
  --no-publish`) and compare before the next scheduled run.
