# Live mode — how-to

`viewlyt-live` taps a YouTube live chat in real time, feeds batches of messages to an LLM, and streams the results to a local dashboard.

## Prerequisites

- `viewlyt[live]` installed (see Setup below).
- An OpenAI-compatible LLM endpoint. The default is a **local** one via [LM Studio](https://lmstudio.ai/): start its local server and load any small model (e.g. `lmstudio-community/Qwen2.5-7B-Instruct-GGUF`).
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
snippet:      http://127.0.0.1:8000/snippet.js  (or copy it from the dashboard)
```

## The three windows

**A — the live video** — watch it in any browser tab (optional, for context).

**B — the chat popout** — open `https://www.youtube.com/live_chat?is_popout=1&v=LIVE_ID` in its own window, then start the capture there with **either**:

- **Bookmarklet (easiest — no "allow pasting"):** on the dashboard, drag the **▶ viewlyt capture** button to your bookmarks bar, then click it while on the popout tab.
- **Console:** open DevTools (`F12`) → **Console**, paste the snippet from the dashboard, press Enter. If Chrome blocks the paste, type `allow pasting` there first.

A small **viewlyt** badge appears in the popout (bottom-right) showing `connected | captured | sent`. Accept Chrome's one-time "Allow access to local network?" prompt if it appears.

> Always use the popout URL. The snippet cannot reach the server from a regular embedded chat frame.

**C — the dashboard** — create probes and watch them update live.

## Example: two probes in action

1. In the **Add Probe** panel, choose kind **classification**:
   - Label: `Mood`
   - Question: `How is the audience feeling?`
   - Categories: `happy, angry, neutral`
   - Click **Add Probe** and watch the `%` bars fill in under **Live Results** as new batches arrive.

2. In the **Window** panel, change **Size (n)** or **Overlap** and click **Apply** — the cadence changes immediately, no restart needed.

3. In the **Add Probe** panel, choose kind **open**:
   - Label: `Complaints`
   - Instruction: `What are the main complaints right now?`
   - Click **Add Probe** and read the rolling summary under **Live Results**.

## Using a cloud or other model

Pass `--base-url`, `--api-key`, and `--model` to point at any OpenAI-compatible endpoint:

```bash
uv run viewlyt-live "https://www.youtube.com/watch?v=LIVE_ID" \
  --base-url https://api.openai.com/v1 \
  --api-key sk-... \
  --model gpt-4o-mini
```

You can also swap the model live from the dashboard's **Model** panel (Base URL / API Key / Model ID → **Apply**) without restarting the server.

## Troubleshooting

- **Badge says `CANNOT REACH SERVER` (or the counts stay at 0):** accept Chrome's one-time *local network* prompt; make sure you used the **popout** (not the embedded chat); keep the server on `127.0.0.1`. If you run an **ad blocker**, allow this page to reach `127.0.0.1` — uBlock Origin's *"Block outsider intrusion into LAN"* filter blocks exactly this, so disable it or allowlist the site.
- **A red `youtubei/v1/player/ad_break … net::ERR_BLOCKED_BY_CLIENT` line in the console:** that is your **ad blocker** blocking YouTube's ad telemetry. It is unrelated to viewlyt and does **not** affect chat capture — ignore it.
- **`allow pasting`:** Chrome's console refuses pasted code until you type `allow pasting` once. Use the **bookmarklet** to skip this entirely.
- **Spam / repeated messages:** the server drops a user's near-duplicate messages and merges their consecutive messages before sampling, so one spammer counts once (toggle in the **Window** panel).
