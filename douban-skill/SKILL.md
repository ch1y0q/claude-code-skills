---
name: douban-skill
description: >
  Export and sync Douban (豆瓣) book/movie/music/game collections to local CSV files via Frodo API.
  Supports full export (all history) and RSS incremental sync (recent items).
  Use when the user wants to export Douban reading/watching/listening/gaming history,
  back up their Douban data, set up incremental sync, or mentions 豆瓣/douban collections.
  Triggers on: 豆瓣, douban, 读书记录, 观影记录, 书影音, 导出豆瓣.
---

# Douban Collection Export

Export Douban user collections (books, movies, music, games) to CSV files.
Douban has no official data export; the official API shut down in 2018.

## What This Skill Can Do

- Full export of all book/movie/music/game collections via Frodo API
- RSS incremental sync for daily updates (last ~10 items)
- CSV output with UTF-8 BOM (Excel-compatible), cross-platform (macOS/Windows/Linux)
- No login, no cookies, no browser required
- Pre-flight user ID validation (fail fast on wrong ID)

## What This Skill Cannot Do

- Cannot export reviews (长评), notes (读书笔记), or broadcasts (广播)
- Cannot filter by single category in one run (exports all 4 types together)
- Cannot access private profiles (returns 0 items silently)

## Why Frodo API (Do NOT Use Web Scraping)

Douban uses PoW (Proof of Work) challenges on web pages, blocking all HTTP scraping.
We tested 7 approaches — only the Frodo API works. **Do NOT attempt** web scraping,
`browser_cookie3`+`requests`, `curl` with cookies, or Jina Reader.

See [references/troubleshooting.md](references/troubleshooting.md) for the complete
failure log of all 7 tested approaches and why each failed.

## Security & Privacy

The API key and HMAC secret in the script are Douban's **public mobile app credentials**,
extracted from the APK. They are shared by all Douban app users and do not identify you.
No personal credentials are used or stored. Data is fetched only from `frodo.douban.com`.

## Full Export (Primary Method)

```bash
DOUBAN_USER=<user_id> python3 scripts/douban-frodo-export.py
```

**Finding the user ID:** Profile URL `douban.com/people/<ID>/` — the ID is after `/people/`.
If the user provides a full URL, the script auto-extracts the ID.

**Environment variables:**
- `DOUBAN_USER` (required): Douban user ID (alphanumeric or numeric, or full profile URL)
- `DOUBAN_OUTPUT_DIR` (optional): Override output directory

**Default output** (auto-detected per platform):
- macOS: `~/Downloads/douban-sync/<user_id>/`
- Windows: `%USERPROFILE%\Downloads\douban-sync\<user_id>\`
- Linux: `~/Downloads/douban-sync/<user_id>/`

**Dependencies:** Python 3.6+ standard library only (works with `python3` or `uv run`).

Read the final console message for the newly published `snapshot-<UTC timestamp>/`
directory. The exporter traverses API offset windows, removes duplicate subjects
within each type/status, and confirms an empty terminal page before publishing.
On request, schema, pagination, or write failure, treat the run as failed and keep
the previous backup. Do not treat a partial run as a complete export.

## RSS Incremental Sync (Complementary)

```bash
DOUBAN_USER=<user_id> node scripts/douban-rss-sync.mjs
```

RSS returns only the latest ~10 items (no pagination). Keep full-export snapshots
as immutable backups. RSS writes separate CSVs directly under `<user_id>/`; it does
not discover, seed from, or update snapshots. For an ongoing CSV containing full
history, explicitly copy the selected snapshot CSVs into that working directory
before the first RSS sync, preserving the snapshot and any existing working files.

## Output Format

Four CSV files and a manifest per successful full-export snapshot:

```
Downloads/douban-sync/<user_id>/snapshot-<UTC timestamp>/
├── 书.csv      (读过 + 在读 + 想读)
├── 影视.csv    (看过 + 在看 + 想看)
├── 音乐.csv    (听过 + 在听 + 想听)
├── 游戏.csv    (玩过 + 在玩 + 想玩)
└── manifest.json
```

Columns: `title, url, date, rating, status, comment`
- `rating`: ★ to ★★★★★ (empty if unrated)
- `date`: YYYY-MM-DD (when the user marked it)
- Preserve previous exports: each successful run publishes a new snapshot.
- Read `manifest.json` for reported totals, unique exported rows, duplicate removals,
  unavailable slots, and terminal offsets per type/status. Unavailable slots are
  not exported; these counts do not prove access to private or delisted records.

## Workflow

1. Ask for Douban user ID (from profile URL, or accept full URL)
2. Run: `DOUBAN_USER=<id> python3 scripts/douban-frodo-export.py`
3. Verify CSV records with a CSV parser against `manifest.json` in the printed snapshot directory; do not use line counts because comments can contain newlines.
4. (Optional) Set up RSS sync for daily incremental updates

## Troubleshooting

See [references/troubleshooting.md](references/troubleshooting.md) for:
- Frodo API auth details (HMAC-SHA1 signature computation)
- Common errors (code 996 signature error, rate limits, pagination quirks)
- Complete failure log of all 7 tested approaches with root causes
- Alternative approaches (豆伴 extension, Tampermonkey script, browser console)
- API endpoint reference with response format

## Offline Regression Tests

Run `python3 -m unittest discover -s douban-skill/tests -v` from the repository root.
Use synthetic API responses; no Douban account or network access is needed.
