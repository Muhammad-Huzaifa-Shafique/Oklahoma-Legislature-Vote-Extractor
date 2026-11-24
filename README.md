# Oklahoma Legislature Vote Extractor

A Python tool that collects Oklahoma House and Senate vote records from the legislature's public pages and writes them as three clean CSV files: one row per vote, one row per member's vote, and a list of members.

This was a team project. I worked on it with my classmates in a graduate course (GRT713). We divided the project into parts, and my part was extracting the House votes. This repository holds the extraction script, which can also collect Senate votes. The first lines of the script say this and link the ChatGPT conversation I used while working on it.

## Why it matters for ML work

Most machine-learning projects start with records that nobody prepared for analysis. This project is that first step on a messy public source: collect many pages in parallel, cut each vote out of semi-structured text with regular expressions, clean the names and duplicates, and save tables that pandas or a database can load. It was also a project shared between several people.

| Skill | Where to look in the script |
|---|---|
| Collecting pages at scale | `discover_ok_house_all_series_by_ids_async`, `discover_ok_senate_all_years_async`, `harvest_urls_async` (asyncio and aiohttp, with a slower fallback if aiohttp is missing) |
| Parsing messy text | `parse_votes_from_page`, `parse_third_reading_blocks`, `parse_session_info` |
| Cleaning | `extract_names`, `explode_concatenated_names`, `dedupe_records` |
| Producing tidy tables | `table_votes_detail`, `table_vote_rollcall`, `table_members`, `save_table` |

## What it produces

| File | One row per | Columns |
|---|---|---|
| `VotesDetail.csv` | vote | Unique Index, Bill Number, Type, Yea, Nay, CP, Excused, Vacant, Result, Location, Date, Time |
| `VoteRollCall.csv` | member's vote | Unique Index, Votes Detail Index, Bill Number, Member, Vote, Type, Result, Location, Date, Time |
| `Members.csv` | member | Unique Index, Chamber, District, Member |

CP is constitutional privilege, a label the script reads from the vote pages. The files are saved to your Downloads folder. If a file with that name already exists, the script saves a numbered copy instead of overwriting it.

## How it works

1. **Find the votes.** On start you choose Auto or manual mode. In Auto mode you then choose House or Senate. For the House it tries vote ID numbers on the former House site for the 47th to 60th Legislatures. For the Senate it walks the Senate site's pages for 2006 to 2025. In manual mode you pick your own files or links: PDFs, web pages, internet shortcuts, or a text file that lists links.
2. **Fetch and read.** Pages are fetched in parallel and PDFs are converted to text. Each vote is cut out of the page text.
3. **Clean.** For each vote it reads the bill number, type of vote, the counts (Yea, Nay, CP, Excused, Vacant), the result, place, date and time, and the member names under each count. It splits names that were run together, removes titles, and drops duplicate records.
4. **Save.** It writes the three CSV files above.

## Try it

Built with Python 3.13 on Windows.

```bash
pip install -r requirements.txt
python ok_votes_extractor.py
```

A small dialog asks for the run mode. If no dialog is available, the script asks in the terminal. Progress is printed as it runs.

`aiohttp` is strongly recommended. Without it the script falls back to a much slower path that makes one request at a time. `PyPDF2` is only needed for PDF files, and `brotli` only for pages served with brotli compression.

## Be kind to the servers

These are public sites, and the defaults are heavy. Auto mode for the House tries every vote ID from 1 to 20,000 in each of 14 legislatures, which is 280,000 requests, up to 128 at a time (`max_id` and `max_concurrency` in `main()`). Lower both numbers before you run it, or use manual mode on a few pages.

## Certificates (changed from our original)

The original script turned off HTTPS certificate checking for its plain `urllib` requests (the `aiohttp` requests always checked). I changed that before publishing, so certificates are now checked everywhere. If a site's certificate cannot be verified on your machine, for example behind a school or company proxy, set `OKVOTES_INSECURE_SSL=1` to skip the check for that run. The script prints a warning when you do.

## Limits

- Written for the sites as they were in fall 2025. Page layouts and addresses may have changed since (the House pages are on `former.okhouse.gov`), so check it against a few pages before relying on it.
- Parsing relies on text patterns tuned to these pages. An unusual layout can drop a vote or split a name in the wrong place.
- There are no automated tests and no hand-checked answers, so accuracy has not been measured.
- It is one long script that asks its questions through dialogs rather than command-line options.
- No data is included. The CSV files are created when you run it.

## What I would add next

- A few saved pages with hand-counted votes to test against, then a measured accuracy.
- Command-line options instead of dialogs, and the script split into modules.

## Author

Made by [Muhammad Huzaifa Shafique](https://github.com/Muhammad-Huzaifa-Shafique) with my classmates.
