# ytcomments

CLI that scrapes a YouTube video's comments (with likes, dates and replies) into
`out/<title-slug>-<video_id>.txt`, using Selenium + headless Google Chrome,
managed with `uv`. See @README.md for full usage.

## Commands

```bash
uv sync                                             # create env, install deps (Python 3.14)
uv run ytcomments '<youtube-url>'                   # scrape (headless by default) -> out/
uv run ytcomments --limit 100 --max-replies 10 '<url>'
uv run ytcomments --headed '<url>'                  # visible browser (best vs bot wall)
uv run python tests/test_units.py                   # browser-free unit tests (no pytest dep)
```

## Layout

- `src/ytcomments/htmltext.py` — pure, **stdlib-only** text helpers (HTML→text, slug,
  relative-date, flatten, `convert_batch`). Keep it dependency-free: it runs inside
  worker threads/subinterpreters, so it must never import Selenium.
- `src/ytcomments/driver.py` — stealth headless Chrome builder.
- `src/ytcomments/scraper.py` — URL parsing, consent/bot bypass, two-phase load+harvest.
- `src/ytcomments/cli.py` — argparse, orchestration, parallel conversion, file output.
- `tests/test_units.py` — tests for the pure helpers.
- `out/` — deliverables (gitignored).

## Conventions

- Python 3.14, `uv`-managed; pinned via `.python-version`.
- All Selenium/WebDriver calls are single-threaded (WebDriver is not thread-safe).
  The only parallelised work is `html_to_text`, via a **batched `ThreadPoolExecutor`**.
  Do NOT switch it to `InterpreterPoolExecutor`/`ProcessPoolExecutor` — benchmarked
  slower for many tiny GIL-bound parses; the step is negligible next to Selenium.
- Extract comment text from `#content-text` **innerHTML** (never `element.text`, which
  drops emoji `alt`); likes from `#vote-count-middle`; date from `#published-time-text`.
- Output format: one block per top-level comment, blocks separated by a blank line:
  - comment: `@user [N likes, yyyy-mm-dd]: message`
  - reply:   `    ↳ (in reply to @parent) @author [N likes, yyyy-mm-dd]: message`
  - messages are flattened to one line; dates are **approximate** (from YouTube's
    relative timestamps).

## Git / commits

- **Do NOT add `Co-Authored-By` or any co-authorship trailers to commit messages.**
- Commit in small, logical blocks with conventional-style messages
  (`feat(scraper): …`, `chore: …`, `docs: …`).
- **Never commit** scraped output (`out/`, `*.txt` — contain usernames/PII), secrets
  or credentials (`.env`, `*.pem`, `*.key`, …), `.venv/`, or browser profiles from
  `--user-data-dir` (they hold cookies/sessions). `.gitignore` already enforces this;
  if you add a persistent profile, keep it outside the repo or matching the ignore rules.

## Housekeeping

- **Always** remove residual/temporary files after test or scrape runs — e.g.
  `out_test/`, `__pycache__/`, `*.pyc`, `debug_*`, `*.crdownload`, and any temp scripts.
  Never delete the real deliverables in `out/` or anything in `src/`.
- Prefer writing throwaway validation runs to `-o out_test` so they're trivial to purge.
