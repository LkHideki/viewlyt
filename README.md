# viewlyt

> A command-line tool — and a typed Python library — for pulling **text out of
> YouTube**: a video's **transcript**, its **comments** (with likes, dates and
> replies), and its **related-videos** sidebar. It drives headless Google Chrome
> through Selenium, and writes clean, **LLM-ready Markdown** into `out/`.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Typed](https://img.shields.io/badge/typed-yes-brightgreen)
![Managed with uv](https://img.shields.io/badge/managed%20with-uv-purple)

## What is this?

`viewlyt` is a small, **CLI-first Python package**. Day to day you use it as the
`vl` command; everything it does is also importable as a **typed library**
(`from viewlyt import scrape_video`, ships `py.typed`). It is **not a framework** —
there's nothing to wire up or extend, just a tool you run and an API you call.

**One command, three modes** — the extra modes are opt-in dependencies:

| Command | What you get | Install |
|---|---|---|
| `vl '<url>'` | **Scrape** a video → Markdown in `out/` (transcript by default; `-c` comments, `-r` related, `-u` all-in-one) | `uv sync` |
| `vl ask out/*.md '<question>'` | **Ask** — chat / RAG over what you already collected, no re-scraping | `uv sync --extra ask` |
| `vl live '<live-url>'` | **Live** — real-time live-chat analysis on a local dashboard | `uv sync --extra live` |

Discover everything from the CLI: `vl --help`, or `vl help ask` / `vl help live`
for a specific mode.

## Quickstart

```bash
# 1. Install — creates the `vl` command inside this project
uv sync

# 2. Scrape a video's transcript (the DEFAULT) -> out/<title>-<id>.transcript.md
uv run vl 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# 3. Comments instead? add -c. Both? -c -t. Everything in one file? -u
uv run vl -c 'https://youtu.be/dQw4w9WgXcQ'
```

Want a global `vl` on your `PATH` (callable from anywhere, no `uv run`)? From the
repo root run `uv tool install .`, then just `vl '<url>'`.

## Contents

- [How the scraper works](#how-the-scraper-works)
- [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [Options](#options)
- [Several videos (batch mode)](#several-videos-batch-mode)
- [Transcript](#transcript) · [Related videos](#related-videos) · [Unified output](#unified-output)
- [Getting past YouTube/Google blocks](#getting-past-youtubegoogle-blocks) · [Output format](#output-format)
- Optional modes: [`vl live`](#live-mode-real-time) · [`vl ask`](#chat-with-your-collected-data-vl-ask)
- [Use as a library](#use-as-a-library) · [Layout](#layout) · [Development](#development)

## How the scraper works

`vl '<url>'` opens the video page and works in two phases (with `tqdm` progress
bars) when collecting comments:

1. **Loading** — repeatedly scrolls to the end (up to **25** scroll steps) to
   lazily load up to **150 top-level comments**, or all of them if there are
   fewer.
2. **Expansion & collection** — walks each thread once: scrolls to it, clicks
   **"Read more"** to untruncate the text, expands the **replies** with a
   reliable click (up to **5 per comment** by default, configurable), and
   records each comment/reply with its **author**, **like count**, and **date**.

The HTML fragments are converted to plain text with a `ThreadPoolExecutor` in
batches (the `alt` of emojis/emotes and the link text are preserved), and the
result is written grouped into blocks — a comment followed by its replies,
blocks separated by a blank line.

**By default** (no selector) `vl` collects the **full transcript** only, into
`out/<title-slug>-<video_id>.transcript.md`. Pass `-c`/`--comments` to collect
the comments instead (`out/<title-slug>-<video_id>.md`), `-c -t` for **both**,
and `-t`/`--transcript` is the explicit transcript-only form. The transcript is
written **token-lean by default**: no `[m:ss]` timestamps and **2 segments per
line** (half the newlines); pass `--ts`/`--timestamps` to keep the stamps.

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
uv sync                 # deps + the dev group; creates the `vl` command
uv run vl --version     # sanity check

# Optional: put `vl` on your PATH so you can call it from anywhere
uv tool install .       # then: vl 'https://youtu.be/dQw4w9WgXcQ'
```

The optional modes pull heavier dependencies only when you ask for them:
`uv sync --extra ask` (chat), `--extra rag` (persistent RAG), `--extra live`
(real-time dashboard).

## Usage

```bash
# Default: headless, TRANSCRIPT only. Writes out/<title-slug>-dQw4w9WgXcQ.transcript.md
uv run vl 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Also accepts youtu.be, /shorts/, /embed/ and the bare id:
uv run vl 'https://youtu.be/dQw4w9WgXcQ'

# Comments only -> out/<title-slug>-<video_id>.md:
uv run vl -c 'https://youtu.be/dQw4w9WgXcQ'

# Visible browser (more reliable against the bot wall):
uv run vl --headed 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'

# Comments: at most 50, skipping replies (much faster):
uv run vl -c --limit-comments 50 --no-replies 'https://youtu.be/dQw4w9WgXcQ'

# Comments: up to 25 replies per comment:
uv run vl -c --limit-replies 25 'https://youtu.be/dQw4w9WgXcQ'

# Writes to another directory:
uv run vl -o ./dump 'https://youtu.be/dQw4w9WgXcQ'

# Comments + full video transcript (the -c and -t selectors together):
uv run vl -c -t 'https://youtu.be/dQw4w9WgXcQ'

# Transcript only (== the default; == --transcript-only):
uv run vl -t 'https://youtu.be/dQw4w9WgXcQ'

# Transcript WITH the [m:ss] timestamps (the default strips them):
uv run vl -t --ts 'https://youtu.be/dQw4w9WgXcQ'

# First 17 related (sidebar) videos -> out/<slug>-<id>.related.md:
uv run vl -r 17 'https://youtu.be/dQw4w9WgXcQ'

# Everything in ONE file out/<slug>-<id>.unified.md (comments + transcript + related):
uv run vl -u 'https://youtu.be/dQw4w9WgXcQ'        # -u == --unify

# Also copy the full output to the system clipboard:
uv run vl -u --copy 'https://youtu.be/dQw4w9WgXcQ'

# Combine several videos into a single out/unified-all.md:
uv run vl --unify-all '<url1>' '<url2>' '<url3>'

# Don't merge consecutive comments from the same author (merging is the default):
uv run vl -c --no-merge-comments 'https://youtu.be/dQw4w9WgXcQ'

# Several videos at once (pool of reused instances):
uv run vl '<url1>' '<url2>' '<url3>'

# From a .txt file (one URL per line) or .csv (any column):
uv run vl --from-file urls.txt
uv run vl videos.csv -j 4          # 4 browsers in parallel
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
| `-c, --comments` | off | Collects comments → `out/<title-slug>-<video_id>.md`; combine with `-t` for both |
| `-t, --transcript` | off | Collects the transcript → `out/<title-slug>-<video_id>.transcript.md`. This is also the **default** when no selector is given; add `-c` for comments too. |
| `--transcript-only` | off | Collects the transcript only (alias of `-t` without `-c`) |
| `--ts, --timestamps` | off | Keeps the `[m:ss]`/`[mm:ss]` timestamps on transcript lines (default: stripped; `h:mm:ss` on long videos is always kept). `--no-ts` is a deprecated no-op. |
| `-r, --related N` | `0` | Collects the first N related (sidebar) videos → `out/<slug>-<id>.related.md` (`0` = off). Selects related; combine with `-c`/`-t`. The sidebar exposes **views**, not likes. |
| `-u, --unify` | off | Writes all of a video's products into ONE `out/<slug>-<id>.unified.md` (instead of separate files). Alone it collects everything (comments + transcript + 20 related; override the count with `-r N`); with `-c`/`-t` it unifies only those. |
| `--unify-all` | off | Like `--unify`, but combines ALL videos into a single `out/unified-all.md` (no per-video files). Mutually exclusive with `--unify`. |
| `--copy` | off | Also copies the full output (the unified doc, or the produced file's content) to the system clipboard (needs `pbcopy`/`clip`/`xclip`/`xsel`) |
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
  batch); a problematic session is recreated automatically, and each video gets
  **one automatic retry** on a fresh session before it counts as failed.
- Workers start **staggered** (a jittered offset each) so N Chromes don't all
  launch — and hit YouTube — at the same instant.
- With **one** video, the detailed per-phase bars appear; with **several**, a
  general "videos" bar carries live `ok`/`fail`/`retry` counters and a `✓`/`↻`/`✗`
  line is printed per finished video, plus the final per-video summary.
- Each video produces its own `out/<title-slug>-<video_id>.md`.

> Each Chrome instance consumes memory (~300–500 MB). Adjust `--jobs` according to the available RAM.

## Transcript

With `-t`/`--transcript` (or `--transcript-only`), the collector expands the
description, clicks the **"Show transcript"** button and reads the transcript
panel, writing `out/<title-slug>-<video_id>.transcript.md`. The default output
is **token-lean**: timestamps stripped and **2 segments joined per line** (half
the newlines — cheaper to feed an LLM):

```
You're probably using the Cloud wrong. It wasn't made just to answer you,
```

With `--ts`/`--timestamps` each segment keeps its stamp (still 2 per line):

```
[0:00] You're probably using the Cloud wrong. [0:02] It wasn't made just to answer you,
```

- The timestamp (under `--ts`) is YouTube's own, **verbatim** (`m:ss` or
  `h:mm:ss` on long videos) — never reformatted; `h:mm:ss` stamps survive even
  the default stripping.
- **No deduplication**: refrains and markers like `[Music]` repeat on purpose.
- It is **opt-in** (keeps comment collection fast by default). `-t`/`--transcript-only`
  (without `-c`) skips the comments and is much faster.
- Videos **without a transcript** (many music clips) are skipped gracefully —
  the final summary shows `transcript: unavailable` and no file is created.
- Library users: `ScrapeResult.transcript_lines(timestamps=True, pair=False)`
  gives the verbatim one-segment-per-line form.

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
  <transcript text, 2 segments per line (add --ts for [m:ss] stamps)>

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
2. **Chrome stealth with a coherent, rotated fingerprint** — the user agent
   matches the REAL OS and the REAL installed Chrome major (read from the
   running binary) and is overridden **together with its Client-Hint metadata**
   via CDP `Network.setUserAgentOverride`, so the UA header and `Sec-CH-UA` can
   never contradict each other; the major rotates per driver (current or
   previous), the window size is drawn from a set of realistic viewports, and
   scroll timing is jittered. Plus the classic layer: a real `--window-size`
   (mandatory, otherwise the comments never load in headless),
   `--disable-blink-features=AutomationControlled`, `excludeSwitches`, and a CDP
   script that hides `navigator.webdriver` and adjusts plugins/languages.
3. **Automatic fallback to visible mode** — if a consent/bot block is still
   detected in headless, the run is automatically retried with a visible
   browser (and every video gets one automatic retry on a fresh session).

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

## Live mode (real-time)

> Opt-in: `uv sync --extra live`. Command: **`vl live`**.

`vl live` taps a YouTube **live chat** in real time, batches the messages to an
LLM, and streams the results — live percentages, rolling summaries, and charts —
to a local dashboard you drive in a second window (create probes by typing a
question, set a spending budget, pick the output language, and more). Run
`vl help live` for every flag.

```bash
uv sync --extra live
# Default capture is server-side: this process drives its own headless Chrome
# on the chat popout — nothing to paste into any browser, and the only route
# that works with Safari (WebKit blocks insecure ws:// from https pages):
uv run vl live 'https://www.youtube.com/watch?v=LIVE_ID'

# Prefer running the snippet/extension in your own browser? opt back into it:
uv run vl live --capture browser 'https://www.youtube.com/watch?v=LIVE_ID'
```

## Chat with your collected data (`vl ask`)

> Opt-in: `uv sync --extra ask` (chat) or `--extra rag` (persistent). Command: **`vl ask`**.

`vl ask` talks to the `out/*.md` (transcripts + comments) you've **already
collected** — no re-scraping. By default it's an **ephemeral chat**: it loads the
files straight into the model's context and answers, and **nothing is saved** —
built for "collect, ask around for a couple of days, then forget it". Each
document is tagged with its video (title, id, url) and engagement metrics, so you
can *compare* videos ("which one got more love?", "how do they relate?").

```bash
uv sync --extra ask
export OPENROUTER_API_KEY=sk-or-...         # the LLM provider
export LLM_NAME=google/gemini-2.5-flash     # any OpenRouter model id

# One-shot (the shell expands out/*.md into files; the leftover text is the question):
uv run vl ask out/*.md 'which video had the better reception, and why?'

# No question -> interactive REPL over the same loaded base (Ctrl-D to quit):
uv run vl ask out/*.md
> which video had the better reception?
> what are the most common complaints in the comments?
```

- **Nothing persists.** The default chat keeps no index and writes no files — close
  it and it's gone. It only needs `openai` (the light `ask` extra). `--lang` sets the
  answer language (default Portuguese (Brazil)); `--model` overrides `$LLM_NAME`.
- **Fits the context.** One to a few videos (transcript + ~150 comments each) fit a
  flash model's context comfortably; you get a warning if the base is very large
  (then pass fewer files, or use `--persist`).

### Persistent base with a sliding window (`--persist`)

For a base you reuse for a while — but **not forever** — add `--persist`. It builds a
**[LightRAG](https://github.com/HKUDS/LightRAG)** knowledge-graph index under
`out/.rag/` and, **on every open, drops documents older than `--ttl-days`** (default
`15`; `$RAG_TTL_DAYS`; `0` keeps all). So the base is a rolling window, not a pile
that grows without bound.

```bash
uv sync --extra rag       # heavier: lightrag + local fastembed embeddings
uv run vl ask --persist out/*.md 'how do these relate?'
uv run vl ask --persist 'summarize the recurring complaints'   # reuses the index
```

- **LLM on OpenRouter, embeddings local.** Embeddings (which OpenRouter doesn't
  reliably serve) run **locally** via [`fastembed`](https://github.com/qdrant/fastembed)
  — no key, on CPU; the first run downloads a small multilingual model
  (`intfloat/multilingual-e5-large`, ~1 GB; set
  `EMBEDDING_NAME=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` for a
  lighter one). Switch providers with `EMBEDDING_PROVIDER=openai|ollama|openrouter`.
- **Cost.** Here the pricey part is *ingestion* (the LLM extracts entities per chunk
  to build the graph). By default the re-extraction pass is **off**; route extraction
  to a cheaper model with `--extract-model NAME` (or `$LLM_EXTRACT_NAME`; the answer
  keeps `$LLM_NAME`), and tune `RAG_MAX_GLEANING` / `RAG_CHUNK_TOKENS`. `--mode` picks
  the retrieval mode (`naive|local|global|hybrid|mix`, default `mix`).
- **Limitation.** A graph RAG targets semantic/relational questions, not exact
  number-crunching; each document's header carries pre-computed counts (comments,
  replies, summed/top likes), but treat aggregate figures as approximate.

**Library use:** `from viewlyt.rag import chat; chat(paths, "question")` (ephemeral),
or `analyze(paths, "question", ttl_days=15)` (persistent). The pure
`prepare_documents` / `build_document` need no extra.

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

## Layout

```
pyproject.toml            uv project + the `vl` console script
src/viewlyt/
  __init__.py             public API (scrape_video, helpers) + __version__
  vl.py                   the `vl` command dispatcher (routes ask/live; lazy imports)
  api.py                  scrape_video / scrape_videos / Session / ScrapeResult (use as a library)
  cli.py                  argparse, URL/file collection, instance pool, formatting, output
  driver.py               Chrome WebDriver builder with stealth (10s timeout)
  scraper.py              URL parsing, consent bypass, two-phase collection, transcript, related
  htmltext.py             HTML→text, relative date, slug, flatten, format_transcript/related/unified (pure, tested)
  rag.py                  `vl ask`: ephemeral chat (default) or --persist LightRAG (opt-in 'ask'/'rag' extras; lazy)
  live/                   `vl live`: opt-in real-time live-chat subpackage (FastAPI + dashboard; extra 'live')
tests/test_units.py       browser-free tests for the pure functions
tests/test_rag.py         browser-free tests for the pure RAG-prep helpers
tests/test_smoke.py       CLI surface + `vl` dispatcher routing/help/packaging (subprocess, no browser)
```

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
uv run --python 3.14t vl '<url>'
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
