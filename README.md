# viewlyt

Collects the comments of one or several YouTube videos into
`out/<title-slug>-<video_id>.md` (plain text, no HTML tags) using **Selenium**
+ **Google Chrome** headless, managed with [`uv`](https://github.com/astral-sh/uv).

It opens the video page and works in two phases (with `tqdm` progress bars):

1. **Loading** — repeatedly scrolls to the end (up to **25** scroll steps) to
   lazily load up to **150 top-level comments** (the project's main asset), or
   all of them if there are fewer.
2. **Expansion & collection** — walks each thread once: scrolls to it, clicks
   **"Read more"** to untruncate the text, expands the **replies** with a
   reliable click (up to **5 per comment** by default, configurable), and
   records each comment/reply with its **author**, **like count**, and **date**.

The HTML fragments are converted to plain text with a `ThreadPoolExecutor` in
batches (the `alt` of emojis/emotes and the link text are preserved), and the
result is written grouped into blocks — a comment followed by its replies,
blocks separated by a blank line.

Optionally, with `-t`/`--transcript`, it also collects the **full transcript**
of the video (opening the panel via the transcript button in the description)
into `out/<title-slug>-<video_id>.transcript.md`. Use `-c -t` for comments
**and** transcript, or `-t` alone for the transcript only.

> **Behavior change:** `-t`/`--transcript` alone now collects ONLY the
> transcript (previously, `--transcript` also kept the comments). For both,
> use `-c -t`.

## Requirements

- `uv` (installs/manages Python; requires **Python ≥ 3.11**, and the
  `.python-version` pins **3.14** for development)
- Google Chrome (or Chromium) installed. The binary is located automatically:
  `$VIEWLYT_CHROME_BINARY` → `/usr/bin/google-chrome` → any `chrome`/`chromium`
  on the `PATH` → Selenium autodetection (default macOS/Windows locations). Set
  `VIEWLYT_CHROME_BINARY` to point to a specific binary (e.g. Brave, or a path
  outside the `PATH`). Selenium Manager downloads the compatible ChromeDriver
  on its own — nothing to install manually.

## Installation

```bash
uv sync
```

## Usage

```bash
# Default: headless. Writes out/<title-slug>-dQw4w9WgXcQ.md
uv run viewlyt 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Also accepts youtu.be, /shorts/, /embed/ and the bare id:
uv run viewlyt 'https://youtu.be/dQw4w9WgXcQ'

# Visible browser (more reliable against the bot wall):
uv run viewlyt --headed 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Collects at most 50 comments and skips replies (much faster):
uv run viewlyt --limit-comments 50 --no-replies 'https://youtu.be/dQw4w9WgXcQ'

# Keeps up to 25 replies per comment:
uv run viewlyt --limit-replies 25 'https://youtu.be/dQw4w9WgXcQ'

# Writes to another directory:
uv run viewlyt -o ./dump 'https://youtu.be/dQw4w9WgXcQ'

# Comments + full video transcript (the -c and -t selectors together):
uv run viewlyt -c -t 'https://youtu.be/dQw4w9WgXcQ'

# Transcript only (skips the comments — much faster):
uv run viewlyt -t 'https://youtu.be/dQw4w9WgXcQ'             # == --transcript-only

# First 17 related (sidebar) videos -> out/<slug>-<id>.related.md:
uv run viewlyt -r 17 'https://youtu.be/dQw4w9WgXcQ'

# Everything in ONE file out/<slug>-<id>.unified.md (comments + transcript + related):
uv run viewlyt --unify 'https://youtu.be/dQw4w9WgXcQ'

# Combine several videos into a single out/unified-all.md:
uv run viewlyt --unify-all '<url1>' '<url2>' '<url3>'

# Don't merge consecutive comments from the same author (merging is the default):
uv run viewlyt --no-merge-comments 'https://youtu.be/dQw4w9WgXcQ'

# Several videos at once (pool of reused instances):
uv run viewlyt '<url1>' '<url2>' '<url3>'

# From a .txt file (one URL per line) or .csv (any column):
uv run viewlyt --from-file urls.txt
uv run viewlyt videos.csv -j 4          # 4 browsers in parallel
```

### Options

| Flag | Default | Description |
|------|--------|-----------|
| `inputs…` | — | one or more URLs/ids and/or `.txt`/`.csv` paths (positional) |
| `-V, --version` | — | shows the version and exits |
| `-f, --from-file PATH` | — | file with URLs/ids (`.txt` one per line, `.csv` any column); repeatable |
| `-j, --jobs N` | `min(4, # videos)` | number of concurrent browsers (reused instances) |
| `--limit-comments N` | `150` | Target number of top-level comments to collect (or all, if fewer). `--limit` is a kept alias. |
| `--max-viewports N` | `25` | Scroll budget (number of scroll-to-end steps) |
| `--no-replies` | off | Does not expand/collect replies (faster) |
| `--limit-replies N` | `5` | Maximum replies per comment (`0` disables it). `--max-replies` is a kept alias. |
| `--no-merge-comments` | off | Does not merge consecutive top-level comments from the same author (merging is the default; `--prevent-comment-group` is an alias) |
| `-c, --comments` | off | Collects comments (the default when no selector is given; combine with `-t` for both) |
| `-t, --transcript` | off | Collects the transcript → `out/<title-slug>-<video_id>.transcript.md`. Without `-c`, collects ONLY the transcript; with `-c`, both. **Changes** the old meaning of `--transcript` (which also kept the comments). |
| `--transcript-only` | off | Collects the transcript only (alias of `-t` without `-c`) |
| `-r, --related N` | `0` | Collects the first N related (sidebar) videos → `out/<slug>-<id>.related.md` (`0` = off). Without `-c` it selects related ONLY; combine with `-c`/`-t`. The sidebar exposes **views**, not likes. |
| `--unify` | off | Writes all of a video's products into ONE `out/<slug>-<id>.unified.md` (instead of separate files). Alone it collects everything (comments + transcript + 20 related; override the count with `-r N`); with `-c`/`-t` it unifies only those. |
| `--unify-all` | off | Like `--unify`, but combines ALL videos into a single `out/unified-all.md` (no per-video files). Mutually exclusive with `--unify`. |
| `--headed` | off | Uses a visible browser instead of headless |
| `--no-fallback` | off | Does not retry in visible mode when a block is detected |
| `--user-data-dir DIR` | — | Persistent Chrome profile (use one already logged in to get past the bot wall) |
| `-o, --out-dir DIR` | `out` | Directory for `<title-slug>-<video_id>.md` |
| `-q, --quiet` | off | Only logs warnings/errors |

## Several videos (batch mode)

You can pass multiple URLs and/or files. The URLs are deduplicated by video id
and processed by a **limited pool of reused Chrome instances**: each worker
keeps **one** browser and processes several videos in sequence (amortizing the
cost of launching Chrome), with up to `--jobs` browsers in parallel (default
`min(4, # videos)`). Since the work is I/O-bound, this speeds things up
considerably.

- Failures are isolated per video (one failing video doesn't bring down the
  batch); a problematic session is recreated automatically.
- With **one** video, the detailed per-phase bars appear; with **several**, a
  general "videos" bar appears plus a final per-video summary.
- Each video produces its own `out/<title-slug>-<video_id>.md`.

> Each Chrome instance consumes memory (~300–500 MB). Adjust `--jobs` according to the available RAM.

## Transcript

With `-t`/`--transcript` (or `--transcript-only`), the collector expands the
description, clicks the **"Show transcript"** button and reads the transcript
panel, writing `out/<title-slug>-<video_id>.transcript.md` with **one segment
per line**:

```
[0:00] You're probably using the Cloud wrong.
[0:02] It wasn't made just to answer you,
```

- The timestamp is YouTube's own, **verbatim** (`m:ss` or `h:mm:ss` on long
  videos) — never reformatted.
- **No deduplication**: refrains and markers like `[Music]` repeat on purpose.
- It is **opt-in** (keeps comment collection fast by default). `-t`/`--transcript-only`
  (without `-c`) skips the comments and is much faster.
- Videos **without a transcript** (many music clips) are skipped gracefully —
  the final summary shows `transcript: unavailable` and no file is created.
- For running text without timestamps: `sed 's/^\[[^]]*\] //' file.transcript.md`.

## Related videos

With `-r`/`--related N` the collector reads the watch page's secondary column
(the "related" / "up next" sidebar) and writes the first N videos to
`out/<title-slug>-<video_id>.related.md` as a numbered Markdown list:

```
1. [1.2B views. Michael Jackson - Smooth Criminal (Official Video)](https://www.youtube.com/watch?v=h_D3VFfhvs4)
2. [20M views. RickRolled by an Ad...](https://www.youtube.com/watch?v=ci6ZtPAN0PM)
```

- The metric is **views, not likes**: the sidebar only exposes a view count
  (likes live on each video's own page). The number is YouTube's own text, kept
  **verbatim** (e.g. `1.2B views`, or `1,2 mi de visualizações` under another
  locale) — nothing is recomputed.
- **Shorts are skipped** (they use a different DOM node); every line is a real
  video with a canonical `watch?v=` URL.
- It is **opt-in** (`0` = off). Without `-c` it collects the related list ONLY
  (fast); combine with `-c`/`-t` to also get comments/transcript.
- **Known limitation:** a title containing `]` (e.g. `[Official Video]`,
  `[4K Remaster]`) breaks the Markdown link syntactically — it's kept as-is by
  design (the text stays readable). Treat the file as plain text.

## Unified output

By default each product goes to its own file. To get **everything in one file**:

- **`--unify`** writes a video's products into a single
  `out/<title-slug>-<video_id>.unified.md` (instead of the separate
  `.md`/`.transcript.md`/`.related.md`):

  ```
  # <video title>

  ## Comments
  @user [842 likes, 2026-06-04]: ...

  ## Transcript
  [0:00] ...

  ## Related videos
  1. [1.2B views. ...](https://www.youtube.com/watch?v=...)
  ```

- **`--unify-all`** combines **all** the videos of a run into a single
  `out/unified-all.md` (one `# title` block per video, in input order; no
  per-video files). Mutually exclusive with `--unify`.

Both **collect every product when used alone** (comments + transcript + 20
related) — pass `-r N` to change the related count, or give explicit `-c`/`-t`
to unify only those. Empty sections (e.g. a video with no transcript) are
skipped, and headers are Markdown so the file renders nicely. (Like the
related-title caveat, third-party text that begins with `#`/`##` — a comment or
title — renders as a fake heading; treat the file as plain text.)

## Getting past YouTube/Google blocks

The collector applies several layers to work on a fresh machine:

1. **Consent cookies** — `SOCS`/`CONSENT` are set before navigating, so the
   "Before you continue to YouTube" notice is skipped on fresh profiles. A
   language-aware click on the consent button (Accept all / Aceitar tudo)
   remains as a fallback.
2. **Chrome stealth** — a realistic (non-headless) user agent, a real
   `--window-size` (mandatory, otherwise the comments never load in headless),
   `--disable-blink-features=AutomationControlled`, `excludeSwitches`, and a CDP
   script that hides `navigator.webdriver` and adjusts plugins/languages.
3. **Automatic fallback to visible mode** — if a consent/bot block is still
   detected in headless, the run is automatically retried with a visible
   browser.

If a flagged/datacenter IP still hits the *"Sign in to confirm you're not a
robot"* wall, pass `--user-data-dir` pointing to a Chrome profile that has
already logged in to YouTube — it's the most reliable bypass.

## Output format

`out/<title-slug>-<video_id>.md` groups each comment with its replies into a
**block**, blocks separated by a blank line:

```
@user [842 likes, 2026-06-04]: comment text here
    ↳ (in reply to @user) @other [4 likes, 2026-06-03]: a reply to that comment
    ↳ (in reply to @user) @third [0 likes, 2026-06-03]: another reply

@nextuser [42 likes, 2026-06-01]: the next top-level comment

@third_user [7 likes, 2026-05-30]: a comment with no replies
```

- The message is flattened into a single line (internal breaks become spaces).
- Custom emotes/emojis are preserved by their `alt` text (e.g. `:smile:` or the emoji character).
- Replies are indented as `    ↳ (in reply to @parent) @author …`, always making
  the parent explicit, and a blank line separates each top-level block.
- The like count is YouTube's own (e.g. `842`, `1.2K`); `0` when hidden/nonexistent.
- The date is **approximate**: YouTube only exposes a relative time ("2 days ago"), which is
  converted to `yyyy-mm-dd` relative to the run date (months≈30d, years≈365d).
  Authors that don't resolve appear as `unknown`.
- The filename slug is the video title, NFKD-normalized with accents removed
  (Portuguese titles become ASCII), lowercased and hyphenated.
- **Merging (default):** **consecutive top-level comments from the same author**
  (a real one — never `unknown`/empty) are merged into a single block (likes/date
  from the first, texts concatenated in order, all replies kept) and **exact
  duplicates** (same author + same text) are discarded. Disable it with
  `--no-merge-comments` (alias `--prevent-comment-group`).

## Layout

```
pyproject.toml            uv project + console-script entry point
src/viewlyt/
  __init__.py             public API (scrape_video, helpers) + __version__
  api.py                  scrape_video / scrape_videos / Session / ScrapeResult (use as a library)
  cli.py                  argparse, URL/file collection, instance pool, formatting, output
  driver.py               Chrome WebDriver builder with stealth (10s timeout)
  scraper.py              URL parsing, consent bypass, two-phase collection, transcript, related
  htmltext.py             HTML→text, relative date, slug, flatten, format_transcript/related/unified (pure, tested)
tests/test_units.py       browser-free tests for the pure functions
```

## Use as a library

Everything the CLI does is available as a typed library (the package ships
`py.typed`, so editors and `mypy`/`pyright` see the types).

### One video

```python
from viewlyt import scrape_video

r = scrape_video("https://youtu.be/dQw4w9WgXcQ", transcript=True, related=5)
print(r.title)
for c in r.top_level:                 # or r.comments / r.replies
    print(c.author, c.likes, c.date, c.text)
for v in r.related:                   # RelatedVideo(video_id, title, views, url)
    print(v.views, v.title, v.url)

print("\n".join(r.comment_lines()))   # same text as the CLI's .md (merged)
print("\n".join(r.transcript_lines()))
print("\n".join(r.related_lines()))
print("\n".join(r.unified_lines()))   # all products in one document (like --unify)
r.write("out/")                       # .md / .transcript.md / .related.md (non-empty only)
r.write("out/", unify=True)           # or a single <slug>-<id>.unified.md
```

`scrape_video` builds and closes its own Chrome and returns a `ScrapeResult`. It
raises `viewlyt.BlockedError` on the bot wall (try `headless=False` or
`user_data_dir=` of a logged-in profile).

### Many videos on one reused browser

Building Chrome is the slow part, so reuse it. `scrape_videos` runs a bounded
pool of reused browsers; `Session` is the manual, single-browser equivalent:

```python
from viewlyt import scrape_videos, Session

# Pool of `jobs` reused browsers. Returns a list ALIGNED to input order:
# a ScrapeResult per success, or None for a failed video (logged, not dropped).
results = scrape_videos(urls, jobs=4, transcript=True, related=10)
for url, r in zip(urls, results):
    if r is None:
        print("failed:", url)
    else:
        r.write("out/")

# Or drive one browser yourself (a headless Session falls back to headed on a block):
with Session(headless=True) as s:
    a = s.scrape(url1)
    b = s.scrape(url2)                # same browser, no cold-start

# The --unify-all equivalent: one document over many videos
from viewlyt import join_unified
doc = join_unified([r.unified_lines() for r in results if r])
```

### Pure helpers (no Selenium)

The pure, dependency-free helpers — `html_to_text`, `format_comment_lines`,
`group_consecutive_comments`, `format_transcript`, `format_related`,
`format_unified`, `join_unified`, `parse_relative_date`, `flatten_inline`,
`slugify` — and the Selenium-backed building blocks (`build_driver`,
`collect_comments`, `collect_related`, `fetch_transcript`, `extract_video_id`)
are all exposed. `import viewlyt` stays Selenium-free until you touch a
Selenium-backed name; to use only the pure helpers, import them straight from the
leaf module: `from viewlyt.htmltext import html_to_text`.

## Development

```bash
uv sync                       # installs deps + the 'dev' group (ruff, pytest, pre-commit)
uv run pytest                 # tests (no browser)
uv run ruff check --fix       # lint
uv run ruff format            # formatting
uv run pre-commit install     # runs ruff + pytest on every commit
```

## Concurrency

The collection is **bound by Selenium I/O** (scrolling/clicking/network), which
is single-threaded by necessity — a WebDriver instance is not thread-safe. The
only parallelizable work is the pure `html_to_text` conversion, which is tiny
next to the Selenium phase.

For that step a `ThreadPoolExecutor` in batches is used. It was a measured
choice, not the default reflex — measuring `html_to_text` over realistic comment
HTML:

| approach | 300 fragments | 1600 fragments |
|---|---|---|
| simple loop | ~60 ms | ~315 ms |
| thread pool (batched) | ~99 ms | ~475 ms |
| `InterpreterPoolExecutor` (PEP 734) | ~220 ms | ~340 ms |

For many tiny GIL-bound parses, subinterpreters/processes add more
startup+pickling cost than they save, so they're the wrong tool here. The thread
pool is kept because (a) it satisfies the project's "use threads" requirement,
(b) its overhead is negligible next to the minutes of Selenium, and (c) on a
**free-threaded** interpreter it actually parallelizes:

```bash
uv python install 3.14t      # CPython free-threaded
uv run --python 3.14t viewlyt '<url>'
```

## Notes / limitations

- Top-level comments aim for the `--limit-comments` (150 by default); replies are
  limited by `--limit-replies` (5 by default) and expanded one level (YouTube's
  reply threads are flat).
- The comment dates are approximated from YouTube's relative times (see above).
- A residential IP and a logged-in profile greatly improve reliability.
- The output is `.md`, but the text comes from third parties: when **importing
  into a spreadsheet** (Excel/Sheets), a cell starting with `=`, `+`, `-` or `@`
  may be interpreted as a formula (CSV/formula injection). Treat it as untrusted
  data or disable formula interpretation when importing.

## License

[MIT](LICENSE) © Lucas Hideki
