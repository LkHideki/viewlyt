# Live mode — how-to

`viewlyt-live` taps a YouTube live chat in real time, feeds batches of messages to an LLM, and streams the results to a local dashboard.

## Prerequisites

- `viewlyt[live]` installed (see Setup below).
- An OpenAI-compatible LLM endpoint. The default is **OpenRouter** (`google/gemini-3.1-flash-lite`) — set `--api-key`. You can also point at **OpenAI**, **Groq**, or a **local** server (**LM Studio** `localhost:1234`, **Ollama** `localhost:11434`).
- **Node** (any LTS) — needed once to build the dashboard (dev only; the built assets are served by the Python process).

## Setup

```bash
uv sync --extra live

# Build the dashboard once:
npm --prefix src/viewlyt/live/dashboard install
npm --prefix src/viewlyt/live/dashboard run build
```

## Run

```bash
uv run viewlyt-live "https://www.youtube.com/watch?v=LIVE_ID"
```

The process prints the URLs and opens the dashboard automatically (default port `8000`):

```
viewlyt-live -> dashboard: http://127.0.0.1:8000/
chat popout:  https://www.youtube.com/live_chat?is_popout=1&v=LIVE_ID
```

## The three windows

**A — the live video** — watch it in any browser tab (optional, for context).

**B — the chat popout** — open `https://www.youtube.com/live_chat?is_popout=1&v=LIVE_ID` in its own window, then start the capture there with **one** of (all on the dashboard's **Capture** panel):

- **Browser extension (recommended, works in Chrome & Vivaldi):** download `viewlyt-extension.zip`, unzip, and load the folder via your browser's Extensions page (Developer mode → Load unpacked). It auto-runs on every chat popout.
- **Bookmark:** copy the bookmark code and paste it as the **URL** of a new bookmark named `viewlyt`; click it while on the popout.
- **Console:** open DevTools (`F12`) → **Console**, paste the snippet, press Enter. If Chrome blocks the paste, type `allow pasting` first.

A small **viewlyt** badge appears in the popout (bottom-right) showing `connected | captured`. Accept Chrome's one-time "Allow access to local network?" prompt if it appears.

> Always use the popout URL. The snippet cannot reach the server from a regular embedded chat frame.

**C — the dashboard** — ask questions and watch the answers update live.

## The dashboard

- **Header** — live counters (Ingested / Buffer / LLM / **Cost**), a spinner while the LLM is analyzing, and a **language selector** on the right (default **Português (BR)**) that sets the language the analyses are written in.
- **Ask bar** (top) — type a question (e.g. `how is the crowd feeling?` or `liste os 3 jogadores mais elogiados`). In **Auto** the system rewrites it and picks the kind, chart and categories for you; **Open**/**Classify** force a kind. **Suggest** reads the current chat and proposes two ready-to-add probes. A new probe is analyzed right away (no wait for the next refresh).
- **Live Results** — one card per probe: a kind badge, the editable label, the prompt beneath it, **Edit** (rename, and for classification reorder/recolor categories), and **Remove**. A **time slider** at the top of each card scrubs through past snapshots; click **LIVE** to snap back to the latest. **Analyze now** forces an immediate analysis. Classification cards choose among 11 visualizations (bars, columns, stacked, donut, lines, area, delta, gauge, heatmap, podium, violin), each with the per-category `%`, the change vs the previous snapshot, and a sparkline. The server keeps the last **120 snapshots per probe**, so a page reload (or a second dashboard) replays the session's history instead of starting blank; **Export JSON / Export CSV** (next to *Analyze now*) download it all — JSON with window/model/totals included, CSV flattened one row per snapshot category (or open-probe text).
- **Live Chat** — the raw captured feed (proves the bridge), bounded with its own scroll. Autoscroll only sticks while you're at the bottom: scroll up to read and a **"↓ N new"** pill counts what keeps arriving (click it to jump back to the latest).
- **Configs → Window** — **Sample** (target messages per analysis), **Refresh (s)**, **Buffer (max)**, **Mode** (count / time / hybrid), and the spam toggles. **Apply** takes effect immediately, no restart.
- **Configs → Model** — provider / Base URL / API Key / Model ID, plus **Budget USD** (`0` = off; pauses analyses once the cumulative cost reaches it). **Apply** swaps the model live.
- **Probes** — a manual **Add Probe** form and a **JSON import** for adding probes in bulk.

## Example

1. Type `how is the audience feeling?` in the ask bar (**Auto**) and press Enter — the system creates a classification probe with inferred categories and the bars fill in as batches arrive.
2. Click **Suggest** to get two more probes proposed from the live chat; click one to add it.
3. On a probe card, switch the chart to **podium** or **area**, or click **Edit** to recolor a category. Drag the slider left to replay earlier snapshots; click **LIVE** to return.
4. In **Window**, change **Refresh** and click **Apply** — the cadence changes immediately.

## Using a cloud or other model

Pass `--base-url`, `--api-key`, and `--model` to point at any OpenAI-compatible endpoint:

```bash
uv run viewlyt-live "https://www.youtube.com/watch?v=LIVE_ID" \
  --base-url https://api.openai.com/v1 \
  --api-key sk-... \
  --model gpt-4o-mini
```

You can also swap the model (and set a spending budget, or the analysis language) live from the dashboard without restarting the server. The setup is persisted to `~/.viewlyt/live-state.json` (the API key Fernet-encrypted), so a restart resumes where you left off; **Reset / forget saved** in the Model panel clears it.

## Troubleshooting

- **Badge says `CANNOT REACH SERVER` (or `captured` stays at 0):** accept Chrome's one-time *local network* prompt; make sure you used the **popout** (not the embedded chat); keep the server on `127.0.0.1`. If you run an **ad blocker**, allow this page to reach `127.0.0.1` — uBlock Origin's *"Block outsider intrusion into LAN"* filter blocks exactly this, so disable it or allowlist the site.
- **Safari:** Safari blocks insecure `ws://` from an `https` page (mixed content) except to the literal host `localhost` — the snippet retries **both** `127.0.0.1` and `localhost` automatically, so just leave it reconnecting for a few seconds. If it still can't connect, a **content blocker** (AdGuard, 1Blocker, uBlock) is the usual cause: disable it for `youtube.com`. The dashboard itself (plain `http://`) works in Safari untouched.
- **A red `youtubei/v1/player/ad_break … net::ERR_BLOCKED_BY_CLIENT` line in the console:** that is your **ad blocker** blocking YouTube's ad telemetry. It is unrelated to viewlyt and does **not** affect chat capture — ignore it.
- **`allow pasting`:** Chrome's console refuses pasted code until you type `allow pasting` once. Use the **extension** or **bookmark** to skip this entirely.
- **A probe shows no results:** it needs the LLM reachable (check the Model panel and the server log) and, for classification, at least a couple of categories. Click **Analyze now** to retry without waiting for the refresh.
- **Spam / repeated messages:** the server drops a user's near-duplicate messages and merges their consecutive messages before sampling, so one spammer counts once (toggle in the **Window** panel).
