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

**B — the chat popout** — open `https://www.youtube.com/live_chat?is_popout=1&v=LIVE_ID`, then open DevTools (`F12`), go to the **Console** tab, and paste the snippet you copied from the dashboard. If Chrome shows a one-time "Allow access to local network?" prompt, click **Allow**.

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

If the snippet cannot reach the server: accept the local-network prompt in Chrome, confirm you are using the popout URL (not the embedded chat), and check that the server is running on `127.0.0.1` (not `0.0.0.0` or a remote host).
