# ytcomments

Scrape the comments of a YouTube video into `out/<title-slug>-<video_id>.txt`
(plain text, no HTML tags) using **Selenium** + headless **Google Chrome**,
managed with [`uv`](https://github.com/astral-sh/uv).

It opens the watch page and works in two phases (with `tqdm` progress bars):

1. **Load** — scrolls to the bottom repeatedly (up to **25** scroll steps) to
   lazily load up to **100 top-level comments** (the project's primary asset),
   or all of them if fewer.
2. **Expand & harvest** — walks each thread once: scrolls it into view, clicks
   **"Read more"** to un-truncate the text, expands **replies** with a trusted
   click (up to **10 per comment** by default, configurable), and records each
   comment/reply with its **author**, **like count** and **date**.

The HTML fragments are converted to plain text with a batched
`ThreadPoolExecutor` (emoji/emote `alt` text and link text are kept), and the
result is written grouped into blocks — a comment followed by its replies,
blocks separated by a blank line.

## Requirements

- `uv` (installs/manages **Python 3.14** itself — see `.python-version`)
- Google Chrome installed at `/usr/bin/google-chrome` (Selenium Manager
  auto-downloads the matching ChromeDriver — nothing to install manually)

## Setup

```bash
uv sync
```

## Usage

```bash
# Default: headless. Writes out/<title-slug>-dQw4w9WgXcQ.txt
uv run ytcomments 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Accepts youtu.be, /shorts/, /embed/ and bare ids too:
uv run ytcomments 'https://youtu.be/dQw4w9WgXcQ'

# Visible browser (most reliable against the bot wall):
uv run ytcomments --headed 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Collect at most 50 comments and skip replies (much faster):
uv run ytcomments --limit 50 --no-replies 'https://youtu.be/dQw4w9WgXcQ'

# Keep up to 25 replies per comment:
uv run ytcomments --max-replies 25 'https://youtu.be/dQw4w9WgXcQ'

# Write into a different directory:
uv run ytcomments -o ./dump 'https://youtu.be/dQw4w9WgXcQ'
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `url` | — | Video URL or bare 11-char id (positional) |
| `--limit N` | `100` | Target top-level comments to collect (or all if fewer) |
| `--max-viewports N` | `25` | Scroll budget (number of scroll-to-bottom steps) |
| `--no-replies` | off | Don't expand/collect replies (faster) |
| `--max-replies N` | `10` | Max replies to collect per comment (`0` disables) |
| `--headed` | off | Run a visible browser instead of headless |
| `--no-fallback` | off | Don't auto-retry headed when a block is detected |
| `--user-data-dir DIR` | — | Persistent Chrome profile (use one already signed in to defeat the bot wall) |
| `-o, --out-dir DIR` | `out` | Directory for `<title-slug>-<video_id>.txt` |
| `-q, --quiet` | off | Only log warnings/errors |

## Bypassing YouTube/Google blocks

The scraper applies several layers so it works on a fresh machine:

1. **Consent cookies** — `SOCS`/`CONSENT` are pre-set before navigating, so the
   "Before you continue to YouTube" interstitial is skipped on fresh profiles.
   A locale-aware consent-button click (Accept all / Aceitar tudo) is kept as a
   fallback.
2. **Stealth Chrome** — realistic non-headless user agent, a real
   `--window-size` (required or comments never lazy-load in headless),
   `--disable-blink-features=AutomationControlled`, `excludeSwitches`, and a CDP
   script that hides `navigator.webdriver` and patches plugins/languages.
3. **Automatic headed fallback** — if a consent/bot wall is still detected in
   headless mode, the run is retried automatically with a visible browser.

If a flagged/datacenter IP still trips the *"Sign in to confirm you're not a
bot"* wall, pass `--user-data-dir` pointing at a Chrome profile that has logged
into YouTube once — that is the most reliable bypass.

## Output format

`out/<title-slug>-<video_id>.txt` groups each comment with its replies into a
**block**, blocks separated by a blank line:

```
@user [842 likes, 2026-06-04]: message text here
    ↳ (in reply to @user) @other [4 likes, 2026-06-03]: a reply to that comment
    ↳ (in reply to @user) @third [0 likes, 2026-06-03]: another reply

@nextuser [42 likes, 2026-06-01]: the next top-level comment

@third_user [7 likes, 2026-05-30]: a comment with no replies
```

- The message is flattened to a single line (internal line breaks become spaces).
- Custom emotes/emoji are kept as their `alt` text (e.g. `:smile:` or the emoji char).
- Replies are indented as `    ↳ (in reply to @parent) @author …` so the parent
  is always explicit, and a blank line separates each top-level block.
- The like count is YouTube's own (e.g. `842`, `1.2K`); `0` when hidden/none.
- The date is **approximate**: YouTube only exposes a relative time
  ("2 days ago"), which is converted to `yyyy-mm-dd` relative to the run date
  (months≈30d, years≈365d). Authors that don't resolve render as `unknown`.
- The filename slug is the video title, NFKD-normalised with accents stripped
  (so Portuguese titles become ASCII), lowercased and hyphenated.

## Layout

```
pyproject.toml            uv project + console-script entry point
src/ytcomments/
  cli.py                  argparse, orchestration, ThreadPool, formatting, output
  driver.py               stealth Chrome WebDriver builder (10s page-load timeout)
  scraper.py              URL parsing, consent bypass, two-phase load/expand/harvest
  htmltext.py             HTML->text, relative-date, slug, flatten (pure, tested)
tests/test_units.py       browser-free tests for the pure functions
```

## Concurrency

The scrape is **I/O-bound on Selenium** (scrolling/clicking/network), which is
single-threaded by necessity — a WebDriver instance is not thread-safe. The only
parallelisable work is the pure `html_to_text` conversion, which is tiny relative
to the Selenium phase.

It uses a batched `ThreadPoolExecutor` for that step. This was a measured choice,
not the default reflex — benchmarking `html_to_text` over realistic comment HTML:

| approach | 300 fragments | 1600 fragments |
|---|---|---|
| plain loop | ~60 ms | ~315 ms |
| thread pool (batched) | ~99 ms | ~475 ms |
| `InterpreterPoolExecutor` (PEP 734) | ~220 ms | ~340 ms |

For many tiny, GIL-bound parses, subinterpreters/processes add more
startup+pickling overhead than they save, so they are the wrong tool here. The
thread pool is kept because (a) it honours the project's "use threads"
requirement, (b) its overhead is negligible next to minutes of Selenium, and
(c) on a **free-threaded** interpreter it parallelises for real:

```bash
uv python install 3.14t      # free-threaded CPython
uv run --python 3.14t ytcomments '<url>'
```

## Notes / limitations

- Top-level comments target `--limit` (100 by default); replies are capped at
  `--max-replies` (10 by default) and expanded one level (YouTube reply threads
  are flat).
- Comment dates are approximated from YouTube's relative timestamps (see above).
- A residential IP and a signed-in profile dramatically improve reliability.
