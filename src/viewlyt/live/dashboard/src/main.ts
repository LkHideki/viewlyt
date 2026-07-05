// viewlyt.live — Control Panel main script
// Strict TypeScript. Runtime deps: marked + DOMPurify (bundled, no CDN).

import { marked } from "marked";
import DOMPurify from "dompurify";

// ---------------------------------------------------------------------------
// Provider table
// ---------------------------------------------------------------------------

interface ProviderInfo {
  base_url: string;
  model: string;
  keyHint: string;
}

const PROVIDERS: Record<string, ProviderInfo> = {
  lmstudio: {
    base_url: "http://localhost:1234/v1",
    model: "local-model",
    keyHint: "any non-empty",
  },
  ollama: {
    base_url: "http://localhost:11434/v1",
    model: "llama3.1",
    keyHint: "any non-empty",
  },
  openai: {
    base_url: "https://api.openai.com/v1",
    model: "gpt-4o-mini",
    keyHint: "sk-...",
  },
  openrouter: {
    base_url: "https://openrouter.ai/api/v1",
    model: "google/gemini-3.1-flash-lite",
    keyHint: "sk-or-...",
  },
  groq: {
    base_url: "https://api.groq.com/openai/v1",
    model: "llama-3.3-70b-versatile",
    keyHint: "gsk_...",
  },
};

// ---------------------------------------------------------------------------
// WebSocket base
// ---------------------------------------------------------------------------

const wsBase =
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host;

// ---------------------------------------------------------------------------
// Message interfaces
// ---------------------------------------------------------------------------

interface StateMsg {
  type: "state";
  window: {
    n: number;
    gap: number;
    mode: string;
    dedupe?: boolean;
    merge_authors?: boolean;
    capacity?: number;
  };
  model: { base_url: string; model: string; budget?: number; language?: string };
  paused: boolean;
  processing?: boolean;
  budget_blocked?: boolean;
  ingested: number;
  latency_ms?: number | null;
  avg_latency_ms?: number | null;
  tokens_total?: number;
  cost_total?: number;
  probes: ProbeDescriptor[];
}

interface ResultMsg {
  type: "result";
  probe_id: string;
  kind: string;
  label: string;
  n: number;
  ts: number;
  pct?: Record<string, number>;
  text?: string;
}

interface StatMsg {
  type: "stat";
  ingested: number;
  buffer: number;
  window: number;
}

interface ProcMsg {
  type: "proc";
  active: boolean;
  latency_ms?: number;
  avg_latency_ms?: number;
}

interface ErrorMsg {
  type: "error";
  message: string;
}

interface ChatFeedMsg {
  type: "chat";
  items: { author: string; text: string }[];
}

interface ProbeDescriptor {
  id: string;
  kind: string;
  label: string;
  question?: string;
  categories?: string[];
  instruction?: string;
  chart?: string;
  // Optional per-category color overrides (category name -> hex like "#4c8bf5").
  // Empty/missing entries fall back to the palette (getCategoryColor).
  colors?: Record<string, string>;
  // descartes-only presentation settings: what the x/y axes represent, whether
  // cells show a raw count or a percentage, and with how many decimals.
  x_label?: string;
  y_label?: string;
  value_mode?: "pct" | "abs";
  pct_decimals?: number;
  max_words?: number;
  // Per-probe overrides; 0/absent = follow the global Window config.
  interval_s?: number; // own re-analysis cadence, seconds
  sample_n?: number; // own sample size (messages per analysis)
}

// Server -> dashboard cost frame: broadcast once per analysed window, after the
// results. cost_* are USD (0 when the provider doesn't expose cost).
interface CostMsg {
  type: "cost";
  tokens_total: number;
  tokens_delta: number;
  cost_total: number;
  cost_delta: number;
}

// Server -> dashboard suggestions frame (B10): exactly two ready-to-use probe
// descriptors proposed for the current chat + the user's typed request. Each
// dict already carries an id; clicking a chip upserts it verbatim.
interface SuggestMsg {
  type: "suggestions";
  probes: ProbeDescriptor[];
}

// Server -> dashboard backfill frame, sent once right after `state` on connect:
// each probe's stored snapshots (result-shaped dicts, oldest -> newest), so a
// reloaded/late dashboard replays the session history instead of starting blank.
interface HistoryMsg {
  type: "history";
  probes: Record<string, ResultMsg[]>;
}

type InboundMsg =
  | StateMsg
  | ResultMsg
  | StatMsg
  | ErrorMsg
  | ChatFeedMsg
  | ProcMsg
  | CostMsg
  | SuggestMsg
  | HistoryMsg;

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------

function el<T extends HTMLElement>(id: string): T {
  const e = document.getElementById(id);
  if (!e) throw new Error(`Element #${id} not found`);
  return e as T;
}

function inputVal(id: string): string {
  return (el<HTMLInputElement>(id)).value.trim();
}

function setInputVal(id: string, val: string | number): void {
  el<HTMLInputElement>(id).value = String(val);
}

// ---------------------------------------------------------------------------
// Control WebSocket with send queue
// ---------------------------------------------------------------------------

let controlWs: WebSocket;
const sendQueue: string[] = [];

function send(op: object): void {
  const msg = JSON.stringify(op);
  if (controlWs.readyState === WebSocket.OPEN) {
    controlWs.send(msg);
  } else {
    sendQueue.push(msg);
  }
}

function flushQueue(): void {
  while (sendQueue.length > 0 && controlWs.readyState === WebSocket.OPEN) {
    controlWs.send(sendQueue.shift()!);
  }
}

// ---------------------------------------------------------------------------
// Status indicator
// ---------------------------------------------------------------------------

function setStatus(connected: boolean): void {
  const s = el<HTMLSpanElement>("status");
  s.textContent = connected ? "Connected" : "Disconnected";
  s.className = "status " + (connected ? "connected" : "disconnected");
}

// ---------------------------------------------------------------------------
// Probe descriptor state — the source of truth for every card's chrome/editor.
// One .result-card is rendered per probe (A4); there is no separate probe list.
// ---------------------------------------------------------------------------

const probeState = new Map<string, ProbeDescriptor>();

// ---------------------------------------------------------------------------
// Markdown renderer: full CommonMark/GFM via `marked`, then sanitized with
// DOMPurify. LLM output is untrusted (chat content can steer the model into
// emitting hostile markup), so sanitizing is NOT optional: a strict tag/attr
// whitelist, and every link is forced to a safe new-tab target.
// ---------------------------------------------------------------------------

marked.setOptions({ gfm: true, breaks: true });

DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

const MD_ALLOWED_TAGS = [
  "p", "br", "hr", "strong", "em", "b", "i", "del", "code", "pre",
  "ul", "ol", "li", "a", "blockquote", "span",
  "h1", "h2", "h3", "h4", "h5", "h6",
  "table", "thead", "tbody", "tr", "th", "td",
];

function renderMarkdown(src: string): string {
  const html = marked.parse(src, { async: false }) as string;
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: MD_ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "start", "target", "rel"],
  });
}

// ---------------------------------------------------------------------------
// Result cards
// ---------------------------------------------------------------------------

const CATEGORY_COLORS = [
  "#4c8bf5",
  "#34a853",
  "#fbbc04",
  "#ea4335",
  "#a142f4",
  "#24c1e0",
  "#ff6d00",
  "#00bfa5",
];

function getCategoryColor(index: number): string {
  return CATEGORY_COLORS[index % CATEGORY_COLORS.length];
}

/**
 * The fill/swatch color for a probe's category: the probe's own override
 * (probe.colors[cat]) when set, else the stable palette slot for its order
 * index. Used everywhere a CATEGORY color is chosen inside the chart renderers.
 */
function colorFor(probeId: string, cat: string, orderIndex: number): string {
  return probeState.get(probeId)?.colors?.[cat] ?? getCategoryColor(orderIndex);
}

// ---------------------------------------------------------------------------
// Per-probe snapshot history + scrubber view state (R5)
// Each result frame appends a Snapshot; the card shows ONE snapshot at a time,
// chosen by a per-card scrubber. resultHistory is oldest -> newest, capped.
// ---------------------------------------------------------------------------

interface Snapshot {
  ts: number;
  n: number;
  pct?: Record<string, number>;
  text?: string;
}

const HISTORY_CAP = 60;
const resultHistory = new Map<string, Snapshot[]>();
const viewState = new Map<string, { index: number; live: boolean }>();

// ---------------------------------------------------------------------------
// Per-probe chart type (server-persisted on the probe descriptor)
// ---------------------------------------------------------------------------

type ChartType =
  | "bars"
  | "columns"
  | "stacked"
  | "donut"
  | "lines"
  | "area"
  | "delta"
  | "gauge"
  | "heatmap"
  | "podium"
  | "violin"
  | "descartes";
const CHART_TYPES: ChartType[] = [
  "bars",
  "columns",
  "stacked",
  "donut",
  "lines",
  "area",
  "delta",
  "gauge",
  "heatmap",
  "podium",
  "violin",
  "descartes",
];

/** Read a probe's persisted chart type (server-side), defaulting to "bars". */
function probeChart(probeId: string): ChartType {
  const stored = probeState.get(probeId)?.chart;
  return stored !== undefined && (CHART_TYPES as readonly string[]).includes(stored)
    ? (stored as ChartType)
    : "bars";
}

/** Small delta badge: ▲ +x.x (green) / ▼ -x.x (red) / 0 (dim) vs previous. */
function buildDeltaBadge(delta: number | null): HTMLElement {
  const badge = document.createElement("span");
  if (delta === null || Math.abs(delta) < 0.05) {
    badge.className = "delta-badge delta-zero";
    badge.textContent = "0";
  } else if (delta > 0) {
    badge.className = "delta-badge delta-up";
    badge.textContent = `▲ +${delta.toFixed(1)}`;
  } else {
    badge.className = "delta-badge delta-down";
    badge.textContent = `▼ ${delta.toFixed(1)}`;
  }
  return badge;
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Create a namespaced SVG element with the given attributes. */
function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    node.setAttribute(k, String(v));
  }
  return node;
}

// The sparkline is a compact ACCENT, not the chart: cap its points so a long
// session (up to 60 snapshots) can't widen its auto-sized grid column and
// starve the 1fr track of the real (colored) bar.
const SPARK_POINTS = 10;

/** Map a 0–100 value to one of the 8 unicode block characters (last N points). */
function sparkline(values: number[]): string {
  const blocks = "▁▂▃▄▅▆▇█";
  return values
    .slice(-SPARK_POINTS)
    .map((v) => blocks[Math.min(7, Math.floor(v / 12.5))])
    .join("");
}

// ---------------------------------------------------------------------------
// Display renderers (one snapshot at a time; the scrubber picks the snapshot)
// ---------------------------------------------------------------------------

/** Clamp a percentage into the 0–100 range. */
function clampPct(pct: number): number {
  return Math.max(0, Math.min(100, pct));
}

/**
 * One category's view at the scrubbed snapshot: its current pct, the series of
 * its pct from the first snapshot up to (and including) the viewed one, the
 * delta vs the immediately-previous snapshot, and its stable color index.
 */
interface CatView {
  cat: string;
  pct: number;
  series: number[];
  delta: number | null;
  i: number;
}

/** Default: horizontal % bars + delta badge vs previous + per-category sparkline. */
function renderBars(probeId: string, views: CatView[]): HTMLElement {
  const barsEl = document.createElement("div");
  barsEl.className = "bars";

  for (const v of views) {
    const row = document.createElement("div");
    row.className = "bar-row";

    const catLabel = document.createElement("span");
    catLabel.className = "bar-label";
    catLabel.textContent = v.cat;

    const track = document.createElement("div");
    track.className = "bar-track";

    const fill = document.createElement("div");
    fill.className = "bar-fill";
    fill.style.width = `${clampPct(v.pct)}%`;
    fill.style.backgroundColor = colorFor(probeId, v.cat, v.i);

    const pctLabel = document.createElement("span");
    pctLabel.className = "bar-pct";
    pctLabel.textContent = `${v.pct.toFixed(1)}%`;

    // Delta vs the immediately-previous snapshot, beside the pct.
    const deltaBadge = buildDeltaBadge(v.delta);

    const sparkEl = document.createElement("span");
    sparkEl.className = "spark";
    sparkEl.textContent = v.series.length > 0 ? sparkline(v.series) : "";

    track.appendChild(fill);
    row.appendChild(catLabel);
    row.appendChild(track);
    row.appendChild(pctLabel);
    row.appendChild(deltaBadge);
    row.appendChild(sparkEl);
    barsEl.appendChild(row);
  }

  return barsEl;
}

/**
 * The loved stats table, reused under EVERY non-bars chart: one row per
 * category with a color swatch, name, pct, delta badge and sparkline.
 */
function buildStats(probeId: string, views: CatView[]): HTMLElement {
  const stats = document.createElement("div");
  stats.className = "cat-stats";

  for (const v of views) {
    const row = document.createElement("div");
    row.className = "cat-stat-row";

    const swatch = document.createElement("span");
    swatch.className = "cat-swatch";
    swatch.style.backgroundColor = colorFor(probeId, v.cat, v.i);

    const name = document.createElement("span");
    name.className = "cat-name";
    name.textContent = v.cat;

    const pctEl = document.createElement("span");
    pctEl.className = "cat-pct";
    pctEl.textContent = `${v.pct.toFixed(1)}%`;

    const sparkEl = document.createElement("span");
    sparkEl.className = "cat-spark";
    sparkEl.textContent = v.series.length > 0 ? sparkline(v.series) : "";

    row.appendChild(swatch);
    row.appendChild(name);
    row.appendChild(pctEl);
    row.appendChild(buildDeltaBadge(v.delta));
    row.appendChild(sparkEl);
    stats.appendChild(row);
  }

  return stats;
}

/** Vertical columns (SVG): one bar per category, height proportional to pct. */
function renderColumns(probeId: string, views: CatView[]): Element {
  const width = 320;
  const height = 140;
  const padX = 8;
  const padY = 8;
  const baseline = height - padY;

  const root = svg("svg", {
    class: "chart-svg chart-columns",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });

  // Faint baseline line along the bottom.
  root.appendChild(
    svg("line", {
      class: "chart-baseline",
      x1: padX,
      y1: baseline,
      x2: width - padX,
      y2: baseline,
    }),
  );

  const n = views.length;
  const plotW = width - 2 * padX;
  const plotH = baseline - padY;
  // Evenly spaced slots; bar takes ~60% of its slot, centered.
  const slot = n > 0 ? plotW / n : plotW;
  const barW = Math.max(2, slot * 0.6);

  views.forEach((v, idx) => {
    const h = (clampPct(v.pct) / 100) * plotH;
    const x = padX + slot * idx + (slot - barW) / 2;
    const y = baseline - h;
    root.appendChild(
      svg("rect", {
        x: x.toFixed(1),
        y: y.toFixed(1),
        width: barW.toFixed(1),
        height: Math.max(0, h).toFixed(1),
        fill: colorFor(probeId, v.cat, v.i),
        rx: 2,
      }),
    );
  });

  return root;
}

/** Single full-width bar split into colored segments. */
function renderStacked(probeId: string, entries: [string, number][]): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "stacked-bar";
  entries.forEach(([cat, pct], i) => {
    const seg = document.createElement("div");
    seg.className = "stacked-seg";
    seg.style.flexBasis = `${clampPct(pct)}%`;
    seg.style.backgroundColor = colorFor(probeId, cat, i);
    seg.title = `${cat}: ${pct.toFixed(1)}%`;
    bar.appendChild(seg);
  });
  return bar;
}

/** Inline SVG donut — one arc per category, sized to its pct. */
function renderDonut(probeId: string, entries: [string, number][]): Element {
  const size = 140;
  const cx = size / 2;
  const cy = size / 2;
  const radius = 52;
  const circumference = 2 * Math.PI * radius;

  const root = svg("svg", {
    class: "chart-svg chart-donut",
    viewBox: `0 0 ${size} ${size}`,
    role: "img",
  });

  const total = entries.reduce((acc, [, pct]) => acc + Math.max(0, pct), 0);
  let offset = 0;
  if (total > 0) {
    entries.forEach(([cat, pct], i) => {
      const frac = Math.max(0, pct) / total;
      const dash = frac * circumference;
      const seg = svg("circle", {
        cx,
        cy,
        r: radius,
        fill: "none",
        stroke: colorFor(probeId, cat, i),
        "stroke-width": 18,
        "stroke-dasharray": `${dash} ${circumference - dash}`,
        // Start at 12 o'clock and walk clockwise from the running offset.
        "stroke-dashoffset": -offset,
        transform: `rotate(-90 ${cx} ${cy})`,
      });
      root.appendChild(seg);
      offset += dash;
    });
  }

  return root;
}

/** Inline SVG multi-line chart over each category's snapshot series. */
function renderLines(probeId: string, views: CatView[]): Element {
  const width = 320;
  const height = 120;
  const padX = 4;
  const padY = 6;

  const root = svg("svg", {
    class: "chart-svg chart-lines",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });

  // Faint baseline at y = 0%.
  root.appendChild(
    svg("line", {
      class: "chart-baseline",
      x1: padX,
      y1: height - padY,
      x2: width - padX,
      y2: height - padY,
    }),
  );

  const plotW = width - 2 * padX;
  const plotH = height - 2 * padY;
  const xFor = (idx: number, len: number): number =>
    len <= 1 ? padX + plotW : padX + (idx / (len - 1)) * plotW;
  const yFor = (pct: number): number => padY + (1 - clampPct(pct) / 100) * plotH;

  for (const v of views) {
    const series = v.series;
    if (series.length === 0) continue;
    const pts = series
      .map((p, idx) => `${xFor(idx, series.length).toFixed(1)},${yFor(p).toFixed(1)}`)
      .join(" ");
    root.appendChild(
      svg("polyline", {
        class: "chart-line",
        stroke: colorFor(probeId, v.cat, v.i),
        points: pts,
      }),
    );
  }

  return root;
}

/**
 * 100%-stacked area over the snapshot history: shows how the mix evolves. Each
 * category is a filled band between its lower and upper cumulative boundary at
 * every time point. A single time point degrades to one vertical 100% stack.
 */
function renderArea(probeId: string, views: CatView[]): Element {
  const width = 320;
  const height = 120;

  const root = svg("svg", {
    class: "chart-svg chart-area",
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });

  if (views.length === 0) return root;

  // Number of time points = longest series among the categories.
  const steps = views.reduce((m, v) => Math.max(m, v.series.length), 0);
  if (steps === 0) return root;

  const xFor = (t: number): number => (steps <= 1 ? 0 : (t / (steps - 1)) * width);
  // y grows downward; cumulative 0 -> top (0), 100 -> bottom (height) inverted.
  const yFor = (cumPct: number): number => height - (clampPct(cumPct) / 100) * height;

  // At each time point compute the normalized (to 100%) cumulative boundaries.
  // boundaries[t][k] = cumulative pct AFTER stacking categories 0..k-1.
  const lowers: number[][] = [];
  const uppers: number[][] = [];
  for (let i = 0; i < views.length; i++) {
    lowers.push(new Array<number>(steps).fill(0));
    uppers.push(new Array<number>(steps).fill(0));
  }
  for (let t = 0; t < steps; t++) {
    const vals = views.map((v) => Math.max(0, v.series[t] ?? 0));
    const total = vals.reduce((a, b) => a + b, 0);
    let cum = 0;
    for (let i = 0; i < views.length; i++) {
      const share = total > 0 ? (vals[i] / total) * 100 : 0;
      lowers[i][t] = cum;
      cum += share;
      uppers[i][t] = cum;
    }
  }

  if (steps === 1) {
    // Single vertical 100% stack: one full-width rect band per category.
    for (let i = 0; i < views.length; i++) {
      const yTop = yFor(uppers[i][0]);
      const yBot = yFor(lowers[i][0]);
      root.appendChild(
        svg("rect", {
          x: 0,
          y: Math.min(yTop, yBot).toFixed(1),
          width,
          height: Math.abs(yBot - yTop).toFixed(1),
          fill: colorFor(probeId, views[i].cat, views[i].i),
          "fill-opacity": 0.85,
        }),
      );
    }
    return root;
  }

  for (let i = 0; i < views.length; i++) {
    // Upper boundary left->right, then lower boundary right->left = closed band.
    const top: string[] = [];
    const bottom: string[] = [];
    for (let t = 0; t < steps; t++) {
      top.push(`${xFor(t).toFixed(1)},${yFor(uppers[i][t]).toFixed(1)}`);
      bottom.push(`${xFor(t).toFixed(1)},${yFor(lowers[i][t]).toFixed(1)}`);
    }
    bottom.reverse();
    root.appendChild(
      svg("polygon", {
        class: "chart-area-band",
        points: top.concat(bottom).join(" "),
        fill: colorFor(probeId, views[i].cat, views[i].i),
        "fill-opacity": 0.85,
      }),
    );
  }

  return root;
}

/** Diverging horizontal bars: change vs previous snapshot (right=green, left=red). */
function renderDelta(views: CatView[]): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "delta-chart";

  // Symmetric scale from the largest absolute delta (min 1 so flat data shows).
  const deltas = views.map((v) => v.delta ?? 0);
  const maxAbs = Math.max(1, ...deltas.map((d) => Math.abs(d)));

  views.forEach((v, idx) => {
    const delta = deltas[idx];
    const row = document.createElement("div");
    row.className = "delta-row";

    const label = document.createElement("span");
    label.className = "delta-label";
    label.textContent = v.cat;

    // Track with a centered zero axis; one half fills per sign.
    const track = document.createElement("div");
    track.className = "delta-track";

    const neg = document.createElement("div");
    neg.className = "delta-half delta-half-neg";
    const pos = document.createElement("div");
    pos.className = "delta-half delta-half-pos";

    const bar = document.createElement("div");
    bar.className = delta >= 0 ? "delta-bar delta-bar-pos" : "delta-bar delta-bar-neg";
    bar.style.width = `${(Math.abs(delta) / maxAbs) * 100}%`;

    if (delta >= 0) pos.appendChild(bar);
    else neg.appendChild(bar);
    track.appendChild(neg);
    track.appendChild(pos);

    const value = document.createElement("span");
    value.className =
      delta > 0.05
        ? "delta-value delta-up"
        : delta < -0.05
          ? "delta-value delta-down"
          : "delta-value delta-zero";
    value.textContent =
      delta > 0.05 ? `+${delta.toFixed(1)}` : delta < -0.05 ? delta.toFixed(1) : "0";

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(value);
    wrap.appendChild(row);
  });

  return wrap;
}

/**
 * Semicircular gauge for the leading category: a 180° track plus a colored arc
 * filling pct/100 of the half-circle, a big centered pct number and the category
 * name beneath. Defensive when every category is zero (empty track only).
 */
function renderGauge(views: CatView[], probeId: string): Element {
  const width = 200;
  const height = 120;
  const cx = width / 2;
  const cy = height - 12;
  const radius = 78;
  const stroke = 16;

  const root = svg("svg", {
    class: "chart-svg chart-gauge",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
  });

  // Point on the semicircle for a fraction f in [0,1] (left=0 -> right=1),
  // walking the upper half: 180° (left) down to 0° (right).
  const pointAt = (f: number): [number, number] => {
    const angle = Math.PI * (1 - Math.max(0, Math.min(1, f)));
    return [cx + radius * Math.cos(angle), cy - radius * Math.sin(angle)];
  };
  const arcPath = (f: number): string => {
    const [sx, sy] = pointAt(0);
    const [ex, ey] = pointAt(f);
    const large = f > 0.5 ? 1 : 0;
    return `M ${sx.toFixed(1)} ${sy.toFixed(1)} A ${radius} ${radius} 0 ${large} 1 ${ex.toFixed(1)} ${ey.toFixed(1)}`;
  };

  // Full-sweep background track.
  root.appendChild(
    svg("path", {
      class: "chart-gauge-track",
      d: arcPath(1),
      fill: "none",
      stroke: "#2a2f3a",
      "stroke-width": stroke,
      "stroke-linecap": "round",
    }),
  );

  // Top category (max pct); defensive when there are no categories / all zero.
  let top: CatView | null = null;
  for (const v of views) {
    if (top === null || v.pct > top.pct) top = v;
  }
  const pct = top ? clampPct(top.pct) : 0;
  if (top && pct > 0) {
    root.appendChild(
      svg("path", {
        class: "chart-gauge-arc",
        d: arcPath(pct / 100),
        fill: "none",
        stroke: colorFor(probeId, top.cat, top.i),
        "stroke-width": stroke,
        "stroke-linecap": "round",
      }),
    );
  }

  const num = svg("text", {
    class: "chart-gauge-pct",
    x: cx,
    y: cy - 8,
    "text-anchor": "middle",
    fill: "currentColor",
    "font-size": 26,
    "font-weight": 700,
  });
  num.textContent = `${pct.toFixed(0)}%`;
  root.appendChild(num);

  const name = svg("text", {
    class: "chart-gauge-name",
    x: cx,
    y: cy + 10,
    "text-anchor": "middle",
    fill: "currentColor",
    "font-size": 11,
    "fill-opacity": 0.7,
  });
  name.textContent = top ? top.cat : "—";
  root.appendChild(name);

  return root;
}

/**
 * Heatmap grid: one ROW per category, columns = the last up-to-24 snapshots of
 * that category's series. Cell fill = the category color at opacity scaled by
 * its % (0 -> near transparent). Defensive against short/empty series. Names
 * live in the stats table, so no left gutter is drawn.
 */
function renderHeatmap(views: CatView[], probeId: string): Element {
  const COLS = 24;
  const cell = 12;
  const gap = 2;
  const rows = views.length;
  const maxLen = views.reduce((m, v) => Math.max(m, v.series.length), 0);
  const cols = Math.max(1, Math.min(COLS, maxLen));
  const width = cols * (cell + gap);
  const height = Math.max(1, rows) * (cell + gap);

  const root = svg("svg", {
    class: "chart-svg chart-heatmap",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
  });

  views.forEach((v, r) => {
    // Take the last `cols` values of this category's series (right-aligned).
    const series = v.series;
    const start = Math.max(0, series.length - cols);
    const fill = colorFor(probeId, v.cat, v.i);
    for (let c = 0; c < cols; c++) {
      const value = series[start + c];
      const has = value !== undefined;
      const opacity = has ? clampPct(value) / 100 : 0;
      root.appendChild(
        svg("rect", {
          x: c * (cell + gap),
          y: r * (cell + gap),
          width: cell,
          height: cell,
          rx: 2,
          fill,
          "fill-opacity": Math.max(0.04, opacity).toFixed(3),
        }),
      );
    }
  });

  return root;
}

/**
 * Winner's podium: rank categories by current pct (desc) and render the top 3 as
 * blocks ordered 2nd–1st–3rd, heights proportional to pct (1st tallest). Fewer
 * than three categories simply show what exists. Defensive on empty input.
 */
function renderPodium(views: CatView[], probeId: string): Element {
  const ranked = [...views].sort((a, b) => b.pct - a.pct).slice(0, 3);

  const width = 300;
  const height = 150;
  const root = svg("svg", {
    class: "chart-svg chart-podium",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
  });
  if (ranked.length === 0) return root;

  // Visual slot order left->right by rank: 2nd, 1st, 3rd (center is tallest).
  const slotOrder = [1, 0, 2].filter((r) => r < ranked.length);
  const slotW = width / slotOrder.length;
  const blockW = Math.min(72, slotW * 0.7);
  const baseline = height - 20;
  const maxPct = Math.max(1, ...ranked.map((v) => clampPct(v.pct)));
  const maxBarH = baseline - 24;

  slotOrder.forEach((rank, slot) => {
    const v = ranked[rank];
    const h = (clampPct(v.pct) / maxPct) * maxBarH;
    const x = slot * slotW + (slotW - blockW) / 2;
    const y = baseline - h;

    root.appendChild(
      svg("rect", {
        x: x.toFixed(1),
        y: y.toFixed(1),
        width: blockW.toFixed(1),
        height: Math.max(2, h).toFixed(1),
        rx: 3,
        fill: colorFor(probeId, v.cat, v.i),
      }),
    );

    const place = svg("text", {
      x: x + blockW / 2,
      y: y - 4,
      "text-anchor": "middle",
      fill: "currentColor",
      "font-size": 12,
      "font-weight": 700,
    });
    place.textContent = `#${rank + 1} ${v.pct.toFixed(0)}%`;
    root.appendChild(place);

    const baseLbl = svg("text", {
      x: x + blockW / 2,
      y: baseline + 14,
      "text-anchor": "middle",
      fill: "currentColor",
      "font-size": 10,
      "fill-opacity": 0.7,
    });
    baseLbl.textContent = v.cat;
    root.appendChild(baseLbl);
  });

  return root;
}

/**
 * Violin plot: per category, a ~9-bin histogram of its series values, mirrored
 * into a symmetric vertical shape. Categories with <3 points degrade to a thin
 * bar at the current value. Defensive against empty input.
 */
function renderViolin(views: CatView[], probeId: string): Element {
  const BINS = 9;
  const slotW = 56;
  const height = 130;
  const padY = 8;
  const width = Math.max(slotW, views.length * slotW);

  const root = svg("svg", {
    class: "chart-svg chart-violin",
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
  });
  if (views.length === 0) return root;

  const plotH = height - 2 * padY;
  // Value (0..100) -> y, with 0 at the bottom and 100 at the top.
  const yFor = (value: number): number => padY + (1 - clampPct(value) / 100) * plotH;
  const maxHalf = slotW * 0.42;

  views.forEach((v, idx) => {
    const cx = idx * slotW + slotW / 2;
    const color = colorFor(probeId, v.cat, v.i);
    const series = v.series;

    if (series.length < 3) {
      // Too few points for a distribution: a thin bar at the current value.
      const y = yFor(v.pct);
      root.appendChild(
        svg("rect", {
          x: (cx - 2).toFixed(1),
          y: Math.min(y, height - padY).toFixed(1),
          width: 4,
          height: Math.max(2, height - padY - y).toFixed(1),
          rx: 2,
          fill: color,
          "fill-opacity": 0.85,
        }),
      );
      return;
    }

    // Histogram over BINS buckets spanning 0..100.
    const counts = new Array<number>(BINS).fill(0);
    for (const raw of series) {
      const value = clampPct(raw);
      const b = Math.min(BINS - 1, Math.floor((value / 100) * BINS));
      counts[b] += 1;
    }
    const maxCount = Math.max(1, ...counts);

    // Build a closed mirrored path: down the right edge, up the left edge.
    const rightPts: string[] = [];
    const leftPts: string[] = [];
    for (let b = 0; b < BINS; b++) {
      // Bin center value -> y; width proportional to the bin's share.
      const value = ((b + 0.5) / BINS) * 100;
      const y = yFor(value);
      const half = (counts[b] / maxCount) * maxHalf;
      rightPts.push(`${(cx + half).toFixed(1)},${y.toFixed(1)}`);
      leftPts.push(`${(cx - half).toFixed(1)},${y.toFixed(1)}`);
    }
    leftPts.reverse();
    root.appendChild(
      svg("path", {
        class: "chart-violin-shape",
        d: `M ${rightPts.concat(leftPts).join(" L ")} Z`,
        fill: color,
        "fill-opacity": 0.8,
      }),
    );
  });

  return root;
}

// ---------------------------------------------------------------------------
// Descartes: a GENERIC 2-D coordinate probe ("qual o seu palpite?", "esforço x
// impacto", …) plotted on a Cartesian grid. Every category is an ordered pair
// "(x, y)" — x is the column (X axis), y is the row (Y axis); axis labels need
// not be numeric. Each cell's intensity is the share of the chat that landed on
// that coordinate. Axes are DERIVED from the probe's categories (first-appearance
// order = axis order), so no extra fields are needed on the probe descriptor, and
// the grid itself doubles as the editor: hover the plot to add a column/row
// before/after, click a tick to rename it, or "✕" to remove it.
// ---------------------------------------------------------------------------

// Matches "(x, y)" with components that may be words or numbers, no nested
// "," "(" ")" (those are reserved as the pair's own delimiters).
const PAIR_RE = /^\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)$/;
const MAX_AXIS = 6; // cap |X|/|Y| so the enum (|X|×|Y| categories) stays small

function parsePair(cat: string): { x: string; y: string } | null {
  const m = cat.trim().match(PAIR_RE);
  return m ? { x: m[1]!.trim(), y: m[2]!.trim() } : null;
}

function formatPair(x: string, y: string): string {
  return `(${x.trim()}, ${y.trim()})`;
}

/** Fold a label for identity comparisons: accent/case/whitespace-insensitive.
 * Mirrors probes.py's _fold_label (Python) — kept in sync by hand; if the two
 * ever diverge (e.g. a different casefold vs lower-case edge case), a category
 * that matches on one side could silently stop matching on the other. */
function foldAxisLabel(s: string): string {
  return s
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .trim()
    .replace(/\s+/g, " ")
    .toLowerCase();
}

/** A collision-free Map key for a folded (x, y) pair — JSON-encoded rather than
 * naively concatenated ("x|y"), so a literal "|" typed into an axis label (they
 * aren't restricted the way "," "(" ")" are) can never make two DIFFERENT
 * coordinates collide on the same key. */
function pairKey(x: string, y: string): string {
  return JSON.stringify([foldAxisLabel(x), foldAxisLabel(y)]);
}

/** Trim, drop blanks, and dedup labels (fold-insensitive, first spelling wins). */
function dedupeLabels(labels: string[]): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of labels) {
    const label = raw.trim();
    if (!label) continue;
    const key = foldAxisLabel(label);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(label);
  }
  return out;
}

/** Distinct X/Y labels in first-appearance order — the array order of
 *  `categories` IS the axis order, so "add before/after" is just a splice. */
function deriveAxes(cats: string[]): { xs: string[]; ys: string[] } {
  const xs: string[] = [];
  const ys: string[] = [];
  const seenX = new Set<string>();
  const seenY = new Set<string>();
  for (const cat of cats) {
    const p = parsePair(cat);
    if (!p) continue; // non-pair (legacy data, stray LLM miss) — not an axis value
    const fx = foldAxisLabel(p.x);
    if (!seenX.has(fx)) {
      seenX.add(fx);
      xs.push(p.x);
    }
    const fy = foldAxisLabel(p.y);
    if (!seenY.has(fy)) {
      seenY.add(fy);
      ys.push(p.y);
    }
  }
  return { xs, ys };
}

/** The full x×y cross product as canonical "(x, y)" strings, x-major, plus a
 *  plain "outro" escape category — the enum is strict, so without one the model
 *  is forced to guess a coordinate even when a message fits none of them.
 *  deriveAxes already ignores non-pair categories, and renderDescartes already
 *  routes them into the "fora da grade" bucket, so this needs no other plumbing. */
function buildGridCategories(xs: string[], ys: string[]): string[] {
  const cats: string[] = [];
  for (const x of xs) for (const y of ys) cats.push(formatPair(x, y));
  cats.push("outro");
  return cats;
}

function seedGridCategories(): string[] {
  return buildGridCategories(["1", "2", "3"], ["1", "2", "3"]);
}

/** If every label on an axis is an integer, suggest the next one (continuing the
 *  sequence on whichever side is being added to); otherwise "" (force a name). */
function nextAxisLabel(labels: string[], side: "before" | "after"): string {
  if (labels.length > 0 && labels.every((l) => /^-?\d+$/.test(l))) {
    const nums = labels.map(Number);
    return String(side === "after" ? Math.max(...nums) + 1 : Math.min(...nums) - 1);
  }
  return "";
}

/** Rebuild a probe's categories from explicit X/Y label lists (single write path
 *  for every axis mutation: add/rename/remove, from the grid or the seed button),
 *  upsert, then re-render its card at the current snapshot.
 *
 *  Known, accepted limitation: this always rebuilds `categories` as the PURE
 *  x×y cross product (+ "outro"). If a probe somehow already carries a stray
 *  non-pair category beyond "outro" (e.g. hand-edited via "Import JSON"), the
 *  first grid edit silently drops it — unlike the empty-state's seed button,
 *  which confirms before replacing. Narrow enough (requires an unusual
 *  pre-existing mixed state) that adding a confirm to every add/rename/remove
 *  wasn't judged worth the added friction on the common case. */
function applyDescartesAxes(probeId: string, xs: string[], ys: string[]): void {
  const cleanX = dedupeLabels(xs).slice(0, MAX_AXIS);
  const cleanY = dedupeLabels(ys).slice(0, MAX_AXIS);
  const prev = probeState.get(probeId);
  const probe: ProbeDescriptor = {
    ...(prev ?? { id: probeId, kind: "classification", label: "" }),
    kind: "classification",
    categories: buildGridCategories(cleanX, cleanY),
    chart: "descartes",
    colors: {},
  };
  send({ op: "upsert_probe", probe });
  probeState.set(probe.id, probe);
  const card = document.querySelector<HTMLDivElement>(
    `[data-probe-id="${CSS.escape(probeId)}"]`,
  );
  if (card) {
    updateCardChrome(probeId);
    const vs = viewState.get(probeId);
    renderDisplay(card, probeId, "classification", vs ? vs.index : 0);
  }
}

/** First non-colliding "base"/"base 2"/"base 3"/… against `existing` (fold-insensitive) —
 *  so repeated quick "+" clicks (before the first new tick gets renamed) each land as
 *  a distinct axis value instead of silently deduping away. */
function uniqueLabel(base: string, existing: string[]): string {
  if (!existing.some((l) => foldAxisLabel(l) === foldAxisLabel(base))) return base;
  for (let i = 2; ; i++) {
    const candidate = `${base} ${i}`;
    if (!existing.some((l) => foldAxisLabel(l) === foldAxisLabel(candidate))) return candidate;
  }
}

function descartesAddAxis(probeId: string, axis: "x" | "y", side: "before" | "after"): void {
  const { xs, ys } = deriveAxes(probeState.get(probeId)?.categories ?? []);
  const target = axis === "x" ? xs : ys;
  if (target.length >= MAX_AXIS) {
    showToast(`Máximo de ${MAX_AXIS} ${axis === "x" ? "colunas" : "linhas"}.`, "info");
    return;
  }
  const base = nextAxisLabel(target, side) || (axis === "x" ? "nova coluna" : "nova linha");
  const label = uniqueLabel(base, target);
  if (side === "before") target.unshift(label);
  else target.push(label);
  applyDescartesAxes(probeId, xs, ys);
  // Focus the freshly-added tick for an immediate "insert and rename" flow.
  requestAnimationFrame(() => {
    const card = document.querySelector<HTMLDivElement>(
      `[data-probe-id="${CSS.escape(probeId)}"]`,
    );
    const sel = `.descartes-tick-label[data-axis="${axis}"][data-label="${CSS.escape(label)}"]`;
    card?.querySelector<HTMLButtonElement>(sel)?.click();
  });
}

/** Returns whether the rename was applied — false (no-op or rejected) tells the
 *  caller to revert the tick back to its read state showing the ORIGINAL label. */
function descartesRenameAxis(
  probeId: string,
  axis: "x" | "y",
  oldLabel: string,
  newLabel: string,
): boolean {
  const trimmed = newLabel.trim();
  if (!trimmed || foldAxisLabel(trimmed) === foldAxisLabel(oldLabel)) return false;
  const { xs, ys } = deriveAxes(probeState.get(probeId)?.categories ?? []);
  const list = axis === "x" ? xs : ys;
  const i = list.findIndex((l) => foldAxisLabel(l) === foldAxisLabel(oldLabel));
  if (i === -1) return false;
  // Refuse a rename that collides with a DIFFERENT existing label on the same
  // axis: applyDescartesAxes dedupes fold-insensitively, so a silent collision
  // would merge two columns/rows into one, discarding whichever's data lost the
  // dedup — always warn instead.
  const collides = list.some((l, j) => j !== i && foldAxisLabel(l) === foldAxisLabel(trimmed));
  if (collides) {
    showToast(
      `Já existe ${axis === "x" ? "uma coluna" : "uma linha"} "${trimmed}" — escolha outro nome.`,
      "info",
    );
    return false;
  }
  list[i] = trimmed;
  applyDescartesAxes(probeId, xs, ys);
  return true;
}

function descartesRemoveAxis(probeId: string, axis: "x" | "y", label: string): void {
  const { xs, ys } = deriveAxes(probeState.get(probeId)?.categories ?? []);
  const list = axis === "x" ? xs : ys;
  // Removing the LAST label of an axis would zero out the x×y cross product
  // entirely — wiping the OTHER axis's labels too, since they only exist as far
  // as some category still pairs with them. Refuse instead of silently emptying
  // the whole grid; the empty-state's own (confirmed) reseed is the way to start over.
  if (list.length <= 1) {
    showToast(
      `Não é possível remover ${axis === "x" ? "a última coluna" : "a última linha"} — a grade ficaria vazia.`,
      "info",
    );
    return;
  }
  const next = list.filter((l) => foldAxisLabel(l) !== foldAxisLabel(label));
  applyDescartesAxes(probeId, axis === "x" ? next : xs, axis === "y" ? next : ys);
}

/** Flip a descartes probe between showing a raw message count ("#") and a
 *  percentage ("%") per cell — persisted like any other axis edit. */
function descartesToggleValueMode(probeId: string): void {
  const prev = probeState.get(probeId);
  if (!prev) return;
  const probe: ProbeDescriptor = {
    ...prev,
    value_mode: prev.value_mode === "abs" ? "pct" : "abs",
  };
  send({ op: "upsert_probe", probe });
  probeState.set(probeId, probe);
  const card = document.querySelector<HTMLDivElement>(
    `[data-probe-id="${CSS.escape(probeId)}"]`,
  );
  if (card) {
    const vs = viewState.get(probeId);
    renderDisplay(card, probeId, "classification", vs ? vs.index : 0);
  }
}

/** One axis tick: a click-to-rename label + a hover-reveal "✕" to remove the
 *  whole column/row. Used for both the X (bottom) and Y (left) gutters. */
function buildAxisTick(probeId: string, axis: "x" | "y", label: string, gridClass: string): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = `${gridClass} descartes-tick`;

  const renderReadState = (): void => {
    const text = document.createElement("button");
    text.type = "button";
    text.className = "descartes-tick-label";
    text.dataset["axis"] = axis;
    text.dataset["label"] = label;
    text.textContent = label;
    text.title = `${label} — clique para renomear`;
    text.addEventListener("click", enterEditState);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "descartes-tick-del";
    del.textContent = "✕";
    del.title = axis === "x" ? "Remover coluna" : "Remover linha";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      descartesRemoveAxis(probeId, axis, label);
    });
    wrap.replaceChildren(text, del);
  };

  const enterEditState = (): void => {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "descartes-tick-input";
    input.value = label;
    let committed = false;
    const commit = (): void => {
      if (committed) return;
      committed = true;
      // On success, applyDescartesAxes triggers a full card re-render that
      // discards this tick entirely — nothing else to do. On failure (blank/
      // unchanged/colliding name), the re-render never happens, so revert this
      // tick to its read state (showing the ORIGINAL label) instead of leaving
      // a dead, unfocused <input> behind.
      if (!descartesRenameAxis(probeId, axis, label, input.value)) renderReadState();
    };
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") input.blur();
      else if (ev.key === "Escape") {
        committed = true; // suppress the blur commit that follows
        renderReadState();
      }
    });
    input.addEventListener("blur", commit);
    wrap.replaceChildren(input);
    input.focus();
    input.select();
  };

  renderReadState();
  return wrap;
}

function buildAxisAddButton(
  probeId: string,
  axis: "x" | "y",
  side: "before" | "after",
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = `descartes-addbtn descartes-add-${axis}-${side}`;
  btn.textContent = "+";
  btn.title =
    axis === "x"
      ? side === "before"
        ? "Adicionar coluna antes"
        : "Adicionar coluna depois"
      : side === "before"
        ? "Adicionar linha abaixo"
        : "Adicionar linha acima";
  btn.addEventListener("click", () => descartesAddAxis(probeId, axis, side));
  return btn;
}

function renderDescartes(views: CatView[], probeId: string, n: number): HTMLElement {
  const root = document.createElement("div");
  root.className = "descartes";

  const probe = probeState.get(probeId);
  const categories = probe?.categories ?? [];
  const derived = deriveAxes(categories);
  const valueMode: "pct" | "abs" = probe?.value_mode === "abs" ? "abs" : "pct";
  const decimals = Math.min(2, Math.max(0, Math.round(probe?.pct_decimals ?? 0)));
  // Clamp at render time (not just on interactive edits): categories can also
  // arrive from the LLM/ask-bar or a pasted "Import JSON" probe spec, neither of
  // which goes through applyDescartesAxes's own MAX_AXIS cap. Anything beyond
  // the clamp still exists in `categories` (so its data isn't silently wrong),
  // it just isn't drawn — its pct is folded into "outro" below.
  const xs = derived.xs.slice(0, MAX_AXIS);
  const ys = derived.ys.slice(0, MAX_AXIS);
  const xsSet = new Set(xs.map(foldAxisLabel));
  const ysSet = new Set(ys.map(foldAxisLabel));

  // pct by (x,y) pair key; anything that isn't a parseable pair (legacy data, a
  // stray LLM miss, or "outro" itself), or that falls outside the clamped axes,
  // is never treated as a plotted coordinate — it lands in "outro" instead.
  const pctByPair = new Map<string, number>();
  let outro = 0;
  for (const v of views) {
    const p = parsePair(v.cat);
    if (p && xsSet.has(foldAxisLabel(p.x)) && ysSet.has(foldAxisLabel(p.y))) {
      pctByPair.set(pairKey(p.x, p.y), v.pct);
    } else {
      outro += v.pct;
    }
  }

  // No axes yet → an empty state that offers to seed a 3×3 grid, so the user
  // never has to hand-type every coordinate.
  if (xs.length === 0 || ys.length === 0) {
    const empty = document.createElement("div");
    empty.className = "descartes-empty";
    const hint = document.createElement("p");
    hint.className = "empty-hint";
    hint.textContent =
      "Descartes mapeia cada mensagem numa coordenada (x, y) de um plano cartesiano.";
    const gen = document.createElement("button");
    gen.type = "button";
    gen.className = "btn-secondary btn-small";
    gen.textContent = "Gerar grade 3×3";
    gen.addEventListener("click", () => {
      // Switching an existing (non-pair) classification probe to descartes must
      // never silently wipe its real categories.
      if (
        categories.length > 0 &&
        !window.confirm("Isso substitui as categorias atuais desta probe. Continuar?")
      ) {
        return;
      }
      const seeded = deriveAxes(seedGridCategories());
      applyDescartesAxes(probeId, seeded.xs, seeded.ys);
      showToast("Grade 3×3 gerada.", "success");
    });
    empty.appendChild(hint);
    empty.appendChild(gen);
    root.appendChild(empty);
    return root;
  }

  let maxPct = 0;
  for (const pct of pctByPair.values()) maxPct = Math.max(maxPct, pct);
  maxPct = Math.max(1, maxPct);
  let modalKey = "";
  let modalPct = -1;
  for (const [key, pct] of pctByPair) {
    if (pct > modalPct) {
      modalPct = pct;
      modalKey = key;
    }
  }

  const main = document.createElement("div");
  main.className = "descartes-main";

  const plot = document.createElement("div");
  plot.className = "descartes-plot";
  plot.style.setProperty("--cols", String(xs.length));
  plot.style.setProperty("--rows", String(ys.length));

  // Rows top→bottom = ys reversed: the LAST y (appended) draws at the top, the
  // FIRST y (prepended) draws at the bottom — the origin, like a real plane.
  for (let r = ys.length - 1; r >= 0; r--) {
    const y = ys[r]!;
    plot.appendChild(buildAxisTick(probeId, "y", y, "descartes-ylabel"));
    for (const x of xs) {
      const key = pairKey(x, y);
      const pct = pctByPair.get(key);
      const cell = document.createElement("div");
      cell.className = "descartes-cell";
      if (pct === undefined) {
        cell.classList.add("is-empty");
        cell.setAttribute("aria-label", `${x}, ${y}: sem dados`);
      } else {
        const alpha = 0.12 + 0.88 * (pct / maxPct);
        cell.style.background = `rgba(76, 139, 245, ${alpha.toFixed(3)})`;
        if (key === modalKey && modalPct > 0) cell.classList.add("is-modal");
        const abs = Math.round((pct / 100) * n);
        const value =
          valueMode === "abs" ? String(abs) : `${pct.toFixed(decimals)}%`;
        if (pct >= 2) {
          const t = document.createElement("span");
          t.className = "descartes-pct";
          t.textContent = value;
          cell.appendChild(t);
        }
        cell.title = `(${x}, ${y}) — ${value}`;
        cell.setAttribute("aria-label", `${x}, ${y}: ${value}`);
      }
      plot.appendChild(cell);
    }
  }
  // Bottom axis row: empty corner + the X ticks, left→right.
  const corner = document.createElement("span");
  corner.className = "descartes-corner";
  plot.appendChild(corner);
  for (const x of xs) plot.appendChild(buildAxisTick(probeId, "x", x, "descartes-xlabel"));
  main.appendChild(plot);

  // Hover-reveal "+" on all 4 edges: left/right add a column before/after,
  // bottom/top add a row before(origin)/after — mirrors the axis-derivation order.
  main.appendChild(buildAxisAddButton(probeId, "x", "before"));
  main.appendChild(buildAxisAddButton(probeId, "x", "after"));
  main.appendChild(buildAxisAddButton(probeId, "y", "before"));
  main.appendChild(buildAxisAddButton(probeId, "y", "after"));

  // Axis titles — what x/y represent, set in the Edit panel — placed right
  // next to their axis: Y rotated alongside the row ticks, X under the column
  // ticks. Hidden entirely until named (no placeholder clutter by default).
  const graph = document.createElement("div");
  graph.className = "descartes-graph";
  if (probe?.y_label) {
    const yTitle = document.createElement("div");
    yTitle.className = "descartes-ytitle";
    yTitle.textContent = probe.y_label;
    graph.appendChild(yTitle);
  }
  const body = document.createElement("div");
  body.className = "descartes-body";
  body.appendChild(main);
  if (probe?.x_label) {
    const xTitle = document.createElement("div");
    xTitle.className = "descartes-xtitle";
    xTitle.textContent = probe.x_label;
    body.appendChild(xTitle);
  }
  graph.appendChild(body);
  root.appendChild(graph);

  // Legend: a single sequential-intensity gradient (this is a generic density
  // map, not a win/lose outcome) + the out-of-grid bucket, if any.
  const foot = document.createElement("div");
  foot.className = "descartes-foot";
  const legend = document.createElement("div");
  legend.className = "descartes-legend";
  legend.appendChild(document.createTextNode("menos"));
  const bar = document.createElement("span");
  bar.className = "descartes-legend-bar";
  legend.appendChild(bar);
  legend.appendChild(document.createTextNode("mais"));
  const modeBtn = document.createElement("button");
  modeBtn.type = "button";
  modeBtn.className = "descartes-valuemode";
  modeBtn.textContent = valueMode === "abs" ? "#" : "%";
  modeBtn.title =
    valueMode === "abs" ? "Mostrar como percentual" : "Mostrar como contagem (#)";
  modeBtn.addEventListener("click", () => descartesToggleValueMode(probeId));
  legend.appendChild(modeBtn);
  foot.appendChild(legend);
  if (outro > 0.05) {
    const o = document.createElement("span");
    o.className = "descartes-outro";
    o.textContent = `fora da grade: ${outro.toFixed(0)}%`;
    foot.appendChild(o);
  }
  root.appendChild(foot);

  return root;
}

/**
 * Build the classification .result-display content for the snapshot at `index`:
 * the chosen visualization wrapped in .chart-viz, plus (for every chart EXCEPT
 * "bars"/"descartes") the .cat-stats table underneath it.
 */
function buildClassificationBody(probeId: string, index: number): HTMLElement {
  const body = document.createElement("div");
  body.className = "result-display";

  const history = resultHistory.get(probeId) ?? [];
  const snap = history[index];
  if (!snap || !snap.pct) return body;

  // Order categories by the probe's CURRENT order (so an Edit-panel reorder reflects
  // immediately, even on an already-captured snapshot), then append any leftover keys
  // the snapshot still carries from a previous category set so nothing is dropped.
  const pct = snap.pct;
  const ordered = (probeState.get(probeId)?.categories ?? []).filter((c) => c in pct);
  const seen = new Set(ordered);
  for (const c of Object.keys(pct)) {
    if (!seen.has(c)) ordered.push(c);
  }
  const entries: [string, number][] = ordered.map((c) => [c, pct[c]]);
  const views: CatView[] = entries.map(([cat, pct], i) => {
    const series = history
      .slice(0, index + 1)
      .map((s) => s.pct?.[cat] ?? 0);
    const delta =
      index > 0 ? pct - (history[index - 1].pct?.[cat] ?? pct) : null;
    return { cat, pct, series, delta, i };
  });

  const type = probeChart(probeId);

  const viz = document.createElement("div");
  viz.className = "chart-viz";
  switch (type) {
    case "columns":
      viz.appendChild(renderColumns(probeId, views));
      break;
    case "stacked":
      viz.appendChild(renderStacked(probeId, entries));
      break;
    case "donut":
      viz.appendChild(renderDonut(probeId, entries));
      break;
    case "lines":
      viz.appendChild(renderLines(probeId, views));
      break;
    case "area":
      viz.appendChild(renderArea(probeId, views));
      break;
    case "delta":
      viz.appendChild(renderDelta(views));
      break;
    case "gauge":
      viz.appendChild(renderGauge(views, probeId));
      break;
    case "heatmap":
      viz.appendChild(renderHeatmap(views, probeId));
      break;
    case "podium":
      viz.appendChild(renderPodium(views, probeId));
      break;
    case "violin":
      viz.appendChild(renderViolin(views, probeId));
      break;
    case "descartes":
      viz.appendChild(renderDescartes(views, probeId, snap.n));
      break;
    default:
      viz.appendChild(renderBars(probeId, views));
      break;
  }
  body.appendChild(viz);

  // "bars" carries its own per-row stats; "descartes" IS a grid of every
  // category (a stats table would just duplicate all 16+ scorelines).
  if (type !== "bars" && type !== "descartes") {
    body.appendChild(buildStats(probeId, views));
  }
  return body;
}

/** Build the open .result-display: a single markdown-rendered snapshot text. */
function buildOpenBody(probeId: string, index: number): HTMLElement {
  const body = document.createElement("div");
  body.className = "result-display";

  const snap = resultHistory.get(probeId)?.[index];
  // A div, not a <p>: the markdown can contain block elements (lists, headings).
  const textEl = document.createElement("div");
  textEl.className = "result-text";
  if (snap && snap.text) {
    // Preprocess: insert newline before inline ordinal markers so list items
    // don't run together when the model returns the list on one line.
    // Covers: "1º", "2°", "3)" etc. (masculine ordinal º, degree °, closing paren)
    const preprocessed = snap.text.replace(/\s+(\d+\s*[º°)])/g, "\n$1");
    textEl.innerHTML = renderMarkdown(preprocessed);
  }
  body.appendChild(textEl);
  return body;
}

/** (Re)render a card's single .result-display at the given snapshot index. */
function renderDisplay(card: HTMLDivElement, probeId: string, kind: string, index: number): void {
  const existing = card.querySelector<HTMLDivElement>(".result-display");
  // Never clobber an in-progress descartes axis-tick rename (an <input> mid-edit,
  // e.g. because a periodic snapshot/result frame landed while the user was
  // typing) — the next render, once the user commits/cancels, picks up the
  // latest data anyway.
  if (existing?.querySelector(".descartes-tick-input:focus")) return;
  const next =
    kind === "classification"
      ? buildClassificationBody(probeId, index)
      : buildOpenBody(probeId, index);
  if (existing) existing.replaceWith(next);
  else card.appendChild(next);
}

/** Format the "n=…" + time string used by both header meta and scrub meta. */
function snapStamp(snap: Snapshot): string {
  return new Date(snap.ts * 1000).toLocaleTimeString() + "  n=" + String(snap.n);
}

/**
 * Refresh the scrubber DOM (range bounds/value, scrub-meta text + LIVE badge)
 * and the header meta to reflect the snapshot currently being viewed.
 */
function refreshScrubber(card: HTMLDivElement, probeId: string, index: number): void {
  const history = resultHistory.get(probeId) ?? [];
  const max = Math.max(0, history.length - 1);
  const snap = history[index];

  const scrubber = card.querySelector<HTMLDivElement>(".result-scrubber");
  const range = card.querySelector<HTMLInputElement>(".scrub-range");
  const meta = card.querySelector<HTMLDivElement>(".scrub-meta");
  const live = card.querySelector<HTMLSpanElement>(".scrub-live");

  if (scrubber) scrubber.classList.toggle("is-single", history.length <= 1);
  if (range) {
    range.min = "0";
    range.max = String(max);
    range.value = String(index);
  }
  if (meta) {
    // Keep the LIVE badge node; set the leading text via the first text node.
    const label = snap ? snapStamp(snap) + " " : "";
    if (meta.firstChild && meta.firstChild.nodeType === Node.TEXT_NODE) {
      meta.firstChild.textContent = label;
    } else {
      meta.insertBefore(document.createTextNode(label), meta.firstChild);
    }
  }
  if (live) {
    if (index === max) {
      live.textContent = "LIVE";
      live.classList.remove("scrub-behind");
    } else {
      live.textContent = String(index - max); // e.g. "-3" snapshots behind
      live.classList.add("scrub-behind");
    }
  }
}

// ---------------------------------------------------------------------------
// Cards ARE the probe surface (A4). One .result-card per probe, created from
// state OR from the first result (whichever lands first). The card carries its
// own header chrome, an inline editor, the scrubber and the single display.
// ---------------------------------------------------------------------------

/** The kind a card currently renders as (drives display + chrome). */
function cardKind(probeId: string): string {
  return probeState.get(probeId)?.kind ?? "open";
}

/** Read a card's display kind from the DOM (set on the card dataset). */
function domKind(card: HTMLDivElement): string {
  return card.dataset["kind"] ?? "open";
}

/** True while a card's inline editor is open (must not be clobbered by sync). */
function editorOpen(card: HTMLDivElement): boolean {
  const editor = card.querySelector<HTMLDivElement>(".card-editor");
  return editor != null && !editor.classList.contains("hidden");
}

// --- Column-span control glyph (1 vs 2 grid columns) -----------------------
function spanRect(x: number, w: number): SVGRectElement {
  return svg("rect", { x, y: 2.5, width: w, height: 11, rx: 1.5 });
}

/** A tiny panel glyph: two side-by-side panels for the "two columns" target,
 *  one wide panel for the "one column" target. Built via the shared `svg()`
 *  helper (no innerHTML), so it needs no sanitising. */
function spanGlyph(twoCol: boolean): SVGSVGElement {
  const s = svg("svg", { viewBox: "0 0 16 16", width: 13, height: 13, "aria-hidden": "true" });
  if (twoCol) {
    s.appendChild(spanRect(2, 5));
    s.appendChild(spanRect(9, 5));
  } else {
    s.appendChild(spanRect(2.5, 11));
  }
  return s;
}

/**
 * Find (or build) the .result-card for a probe. A freshly-built card has the
 * FULL skeleton and its static handlers wired exactly once; the kind-specific
 * chrome (badge text, label, prompt, chart-select / edit-categories visibility)
 * is applied later by updateCardChrome from state.
 */
function ensureCard(probeId: string): HTMLDivElement {
  const container = el<HTMLDivElement>("results");
  const existing = container.querySelector<HTMLDivElement>(
    `[data-probe-id="${CSS.escape(probeId)}"]`,
  );
  if (existing) return existing;

  const card = document.createElement("div");
  card.className = "result-card";
  card.dataset["probeId"] = probeId;
  card.dataset["kind"] = cardKind(probeId);

  // --- header (vertical: a compact meta strip, then the title on its own line) ---
  const header = document.createElement("div");
  header.className = "result-header";

  // Meta strip: kind badge (left) + action controls (right). Keeping the
  // controls up here means they never steal width from the title below.
  const headerTop = document.createElement("div");
  headerTop.className = "result-header-top";

  const badge = document.createElement("span");
  badge.className = "result-kind-badge";
  badge.textContent = cardKind(probeId);
  headerTop.appendChild(badge);

  const right = document.createElement("div");
  right.className = "result-header-right";

  const select = document.createElement("select");
  select.className = "chart-select";
  for (const t of CHART_TYPES) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    select.appendChild(opt);
  }
  select.value = probeChart(probeId);
  select.addEventListener("change", () => {
    const chosen = select.value as ChartType;
    // Persist server-side by re-upserting the existing probe descriptor.
    const prev = probeState.get(probeId);
    const probe: ProbeDescriptor = prev
      ? { ...prev, chart: chosen }
      : { id: probeId, kind: "classification", label: "", chart: chosen };
    // Descartes needs "(x, y)" pair categories. If the probe has none at all, seed
    // a default 3×3 grid so picking it just works (never clobbers existing
    // categories — a probe that already has some shows the grid's empty-state).
    if (chosen === "descartes" && (prev?.categories?.length ?? 0) === 0) {
      probe.categories = seedGridCategories();
    }
    send({ op: "upsert_probe", probe });
    probeState.set(probe.id, probe);
    const vs = viewState.get(probeId);
    renderDisplay(card, probeId, domKind(card), vs ? vs.index : 0);
  });

  // Collapse toggle: header stays visible (title + meta), body folds away.
  const collapseBtn = document.createElement("button");
  collapseBtn.className = "card-collapse btn-secondary btn-small";
  collapseBtn.type = "button";
  const collapsedKey = "viewlyt.cardcollapsed." + probeId;
  const setCollapsed = (collapsed: boolean): void => {
    card.classList.toggle("collapsed", collapsed);
    collapseBtn.textContent = collapsed ? "▸" : "▾";
    collapseBtn.title = collapsed ? "Expand" : "Collapse";
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
  };
  setCollapsed(lsGet(collapsedKey) === "1");
  collapseBtn.addEventListener("click", () => {
    const next = !card.classList.contains("collapsed");
    setCollapsed(next);
    lsSet(collapsedKey, next ? "1" : "0");
  });

  const editBtn = document.createElement("button");
  editBtn.className = "card-edit btn-secondary btn-small";
  editBtn.type = "button";
  editBtn.textContent = "Edit";
  editBtn.addEventListener("click", () => openEditor(probeId));

  const removeBtn = document.createElement("button");
  removeBtn.className = "card-remove btn-danger btn-small";
  removeBtn.type = "button";
  removeBtn.textContent = "Remove";
  removeBtn.addEventListener("click", () => {
    send({ op: "remove_probe", id: probeId });
    probeState.delete(probeId);
    resultHistory.delete(probeId);
    viewState.delete(probeId);
    card.remove();
  });

  right.appendChild(select);
  right.appendChild(editBtn);
  right.appendChild(removeBtn);
  right.appendChild(collapseBtn);
  headerTop.appendChild(right);
  header.appendChild(headerTop);

  // Title + prompt on their own lines below the meta strip: the label gets a
  // full, dedicated line (wraps completely, never truncated to an ellipsis) and
  // the prompt sits under it as a 2-line-clamped subtitle.
  const titles = document.createElement("div");
  titles.className = "result-titles";
  const labelEl = document.createElement("span");
  labelEl.className = "result-label";
  const promptEl = document.createElement("span");
  promptEl.className = "result-prompt";
  titles.appendChild(labelEl);
  titles.appendChild(promptEl);
  header.appendChild(titles);

  card.appendChild(header);

  // --- inline editor (hidden by default) ---
  const editor = document.createElement("div");
  editor.className = "card-editor hidden";

  const labelField = document.createElement("div");
  labelField.className = "field-group";
  const labelInput = document.createElement("input");
  labelInput.className = "edit-label";
  labelInput.type = "text";
  labelInput.placeholder = "Label";
  labelField.appendChild(labelInput);
  editor.appendChild(labelField);

  const promptField = document.createElement("div");
  promptField.className = "field-group";
  const promptInput = document.createElement("textarea");
  promptInput.className = "edit-prompt";
  promptInput.rows = 3;
  promptInput.placeholder = "Prompt";
  promptField.appendChild(promptInput);
  editor.appendChild(promptField);

  const editCats = document.createElement("div");
  editCats.className = "edit-categories";
  editor.appendChild(editCats);

  // Per-probe settings: own refresh cadence + own sample size (blank = global).
  const settings = document.createElement("div");
  settings.className = "edit-settings";
  const mkSetting = (cls: string, labelText: string, step: string): HTMLInputElement => {
    const wrap = document.createElement("label");
    wrap.className = "edit-setting";
    const span = document.createElement("span");
    span.textContent = labelText;
    const input = document.createElement("input");
    input.className = cls;
    input.type = "number";
    input.min = "0";
    input.step = step;
    input.placeholder = "global";
    wrap.appendChild(span);
    wrap.appendChild(input);
    settings.appendChild(wrap);
    return input;
  };
  mkSetting("edit-interval", "Refresh (s)", "5");
  mkSetting("edit-sample", "Sample (msgs)", "10");
  const settingsHint = document.createElement("span");
  settingsHint.className = "hint";
  settingsHint.textContent = "blank/0 = follow the global Window config";
  settings.appendChild(settingsHint);
  editor.appendChild(settings);

  const buttonRow = document.createElement("div");
  buttonRow.className = "button-row";
  const saveBtn = document.createElement("button");
  saveBtn.className = "edit-save btn-primary btn-small";
  saveBtn.type = "button";
  saveBtn.textContent = "Save";
  saveBtn.addEventListener("click", () => saveEditor(probeId));
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "edit-cancel btn-secondary btn-small";
  cancelBtn.type = "button";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => closeEditor(probeId));
  buttonRow.appendChild(saveBtn);
  buttonRow.appendChild(cancelBtn);
  editor.appendChild(buttonRow);
  card.appendChild(editor);

  // --- scrubber ---
  const scrubber = document.createElement("div");
  scrubber.className = "result-scrubber";

  const range = document.createElement("input");
  range.className = "scrub-range";
  range.type = "range";
  range.min = "0";
  range.max = "0";
  range.value = "0";
  range.step = "1";

  const scrubMeta = document.createElement("div");
  scrubMeta.className = "scrub-meta";
  // Leading text node (filled by refreshScrubber) + the LIVE/behind badge.
  scrubMeta.appendChild(document.createTextNode(""));
  const liveBadge = document.createElement("span");
  liveBadge.className = "scrub-live";
  liveBadge.textContent = "LIVE";
  liveBadge.title = "Jump to the most recent";
  // Clicking the LIVE / -N badge snaps the scrubber back to the latest snapshot.
  liveBadge.addEventListener("click", () => {
    const hist = resultHistory.get(probeId) ?? [];
    const max = Math.max(0, hist.length - 1);
    const vs = viewState.get(probeId);
    if (vs) {
      vs.index = max;
      vs.live = true;
    }
    range.value = String(max);
    renderDisplay(card, probeId, domKind(card), max);
    refreshScrubber(card, probeId, max);
  });
  scrubMeta.appendChild(liveBadge);

  scrubber.appendChild(range);
  scrubber.appendChild(scrubMeta);
  card.appendChild(scrubber);

  // Wire the scrubber listener ONCE; read the live max + kind each time.
  // rAF-throttled: dragging fires `input` continuously and each pass rebuilds
  // the chart SVG from scratch, so render at most once per frame (the handler
  // reads range.value at frame time — intermediate positions are skipped).
  let scrubQueued = false;
  range.addEventListener("input", () => {
    if (scrubQueued) return;
    scrubQueued = true;
    requestAnimationFrame(() => {
      scrubQueued = false;
      const hist = resultHistory.get(probeId) ?? [];
      const max = Math.max(0, hist.length - 1);
      const idx = Math.min(max, Math.max(0, Number(range.value)));
      const vs = viewState.get(probeId);
      if (vs) {
        vs.index = idx;
        vs.live = idx === max;
      }
      renderDisplay(card, probeId, domKind(card), idx);
      refreshScrubber(card, probeId, idx);
    });
  });

  // --- display (placeholder until the first result arrives) ---
  const display = document.createElement("div");
  display.className = "result-display";
  const placeholder = document.createElement("p");
  placeholder.className = "empty-hint";
  placeholder.textContent = "Waiting for first result…";
  display.appendChild(placeholder);
  card.appendChild(display);

  // --- column-span toggle: a discreet button at the card's bottom-right that
  // flips the card between one and two grid columns. Purely a client display
  // preference (like collapse), persisted per-probe in localStorage. ---
  const spanBtn = document.createElement("button");
  spanBtn.type = "button";
  spanBtn.className = "card-span";
  const spanKey = "viewlyt.cardspan." + probeId;
  const setSpan = (wide: boolean): void => {
    card.classList.toggle("span-2", wide);
    spanBtn.classList.toggle("is-wide", wide);
    spanBtn.setAttribute("aria-pressed", String(wide));
    spanBtn.title = wide ? "Shrink to one column" : "Expand to two columns";
    // The glyph previews the TARGET state (what a click will produce).
    spanBtn.replaceChildren(spanGlyph(!wide));
  };
  setSpan(lsGet(spanKey) === "1");
  spanBtn.addEventListener("click", () => {
    const next = !card.classList.contains("span-2");
    setSpan(next);
    lsSet(spanKey, next ? "1" : "0");
  });
  card.appendChild(spanBtn);

  // Drop the top-level "Waiting for results…" hint (a DIRECT child of #results;
  // never a card's own nested placeholder) the moment the first card appears.
  const topHint = container.querySelector(":scope > .empty-hint");
  if (topHint) topHint.remove();

  container.prepend(card);
  return card;
}

/**
 * Apply the read-only chrome from state: kind badge, label, prompt line, and
 * the show/hide of the chart-select + edit-categories by kind. NEVER touches the
 * editor inputs while the editor is open (don't clobber an in-progress edit) and
 * never touches the display / scrubber / history.
 */
function updateCardChrome(probeId: string): void {
  const card = ensureCard(probeId);
  const probe = probeState.get(probeId);
  const kind = probe?.kind ?? domKind(card);
  card.dataset["kind"] = kind;

  const badge = card.querySelector<HTMLSpanElement>(".result-kind-badge");
  if (badge) badge.textContent = kind;

  const labelEl = card.querySelector<HTMLSpanElement>(".result-label");
  if (labelEl) labelEl.textContent = probe?.label ?? "";

  const promptEl = card.querySelector<HTMLSpanElement>(".result-prompt");
  if (promptEl) {
    promptEl.textContent =
      kind === "classification" ? (probe?.question ?? "") : (probe?.instruction ?? "");
  }

  const select = card.querySelector<HTMLSelectElement>(".chart-select");
  if (select) {
    select.classList.toggle("hidden", kind !== "classification");
    select.value = probeChart(probeId);
  }

  // Editor's category rows only make sense for classification — but never
  // rebuild them while the editor is open mid-edit.
  if (!editorOpen(card)) {
    const editCats = card.querySelector<HTMLDivElement>(".edit-categories");
    if (editCats) editCats.classList.toggle("hidden", kind !== "classification");
  }
}

/** Mirror probeState onto the cards: create/update/remove (A4). */
function syncCardsFromState(): void {
  const container = el<HTMLDivElement>("results");
  for (const probeId of probeState.keys()) {
    ensureCard(probeId);
    updateCardChrome(probeId);
  }
  // Remove cards whose probe disappeared (skip the error banner card).
  const cards = container.querySelectorAll<HTMLDivElement>(".result-card[data-probe-id]");
  cards.forEach((card) => {
    const pid = card.dataset["probeId"];
    if (pid !== undefined && !probeState.has(pid)) {
      card.remove();
      // Drop the orphaned snapshots too: a probe recreated later under the same
      // slug id must start fresh, not inherit the removed probe's scrubber.
      resultHistory.delete(pid);
      viewState.delete(pid);
    }
  });
}

// ---------------------------------------------------------------------------
// Per-card inline editor (A2/A3): rename / re-prompt / reorder / recolor.
// ---------------------------------------------------------------------------

/** Build one .edit-categories row: [color][name input][up][down][delete]. */
function buildEditCatRow(probeId: string, cat: string, orderIndex: number): HTMLDivElement {
  const row = document.createElement("div");
  row.className = "edit-cat-row";

  const color = document.createElement("input");
  color.className = "edit-color";
  color.type = "color";
  color.value = colorFor(probeId, cat, orderIndex);

  const name = document.createElement("input");
  name.className = "edit-cat-name";
  name.type = "text";
  name.value = cat;
  name.placeholder = "category";

  const up = document.createElement("button");
  up.className = "cat-up btn-secondary btn-small";
  up.type = "button";
  up.textContent = "↑";
  up.title = "Move up";
  up.addEventListener("click", () => {
    const prev = row.previousElementSibling;
    // Don't hop over the trailing "+ Add category" button (a non-row sibling).
    if (prev && prev.classList.contains("edit-cat-row")) row.parentElement?.insertBefore(row, prev);
  });

  const down = document.createElement("button");
  down.className = "cat-down btn-secondary btn-small";
  down.type = "button";
  down.textContent = "↓";
  down.title = "Move down";
  down.addEventListener("click", () => {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("edit-cat-row")) row.parentElement?.insertBefore(next, row);
  });

  const del = document.createElement("button");
  del.className = "cat-del btn-danger btn-small";
  del.type = "button";
  del.textContent = "✕";
  del.title = "Delete category";
  del.addEventListener("click", () => row.remove());

  row.appendChild(color);
  row.appendChild(name);
  row.appendChild(up);
  row.appendChild(down);
  row.appendChild(del);
  return row;
}

/** Populate + reveal a card's editor from its current descriptor. */
function openEditor(probeId: string): void {
  const card = ensureCard(probeId);
  const probe = probeState.get(probeId);
  const kind = probe?.kind ?? domKind(card);

  const labelInput = card.querySelector<HTMLInputElement>(".edit-label");
  if (labelInput) labelInput.value = probe?.label ?? "";

  const promptInput = card.querySelector<HTMLTextAreaElement>(".edit-prompt");
  if (promptInput) {
    promptInput.value =
      kind === "classification" ? (probe?.question ?? "") : (probe?.instruction ?? "");
  }

  const editCats = card.querySelector<HTMLDivElement>(".edit-categories");
  if (editCats) {
    editCats.innerHTML = "";
    // Descartes' categories are "(x, y)" pairs — a flat per-pair list (one row
    // per grid cell, up to 36) is unreadable and pointless: axes are edited
    // directly on the grid itself (hover the plot to add, click a tick to
    // rename, "✕" to remove). Point users there instead.
    if (kind === "classification" && probeChart(probeId) === "descartes") {
      editCats.classList.remove("hidden");
      const hint = document.createElement("p");
      hint.className = "hint";
      hint.textContent = "Edite as colunas e linhas direto no gráfico do card.";
      editCats.appendChild(hint);

      const axisRow = document.createElement("div");
      axisRow.className = "edit-axis-row";
      const xLabelInput = document.createElement("input");
      xLabelInput.className = "edit-xlabel";
      xLabelInput.type = "text";
      xLabelInput.placeholder = "Eixo X representa";
      xLabelInput.value = probe?.x_label ?? "";
      const yLabelInput = document.createElement("input");
      yLabelInput.className = "edit-ylabel";
      yLabelInput.type = "text";
      yLabelInput.placeholder = "Eixo Y representa";
      yLabelInput.value = probe?.y_label ?? "";
      axisRow.appendChild(xLabelInput);
      axisRow.appendChild(yLabelInput);
      editCats.appendChild(axisRow);

      const decimalsField = document.createElement("label");
      decimalsField.className = "edit-setting";
      const decimalsSpan = document.createElement("span");
      decimalsSpan.textContent = "Casas decimais (%)";
      const decimalsInput = document.createElement("input");
      decimalsInput.className = "edit-decimals";
      decimalsInput.type = "number";
      decimalsInput.min = "0";
      decimalsInput.max = "2";
      decimalsInput.step = "1";
      decimalsInput.value = String(probe?.pct_decimals ?? 0);
      decimalsField.appendChild(decimalsSpan);
      decimalsField.appendChild(decimalsInput);

      // "#" (absolute count) vs "%" (relative share) — the same toggle as the
      // card's own footer button, mirrored here so it's reachable from Edit too.
      const modeRow = document.createElement("div");
      modeRow.className = "ask-chips edit-valuemode-row";
      const pctBtn = document.createElement("button");
      pctBtn.type = "button";
      pctBtn.className = "ask-chip edit-valuemode-btn";
      pctBtn.dataset["mode"] = "pct";
      pctBtn.textContent = "%";
      const absBtn = document.createElement("button");
      absBtn.type = "button";
      absBtn.className = "ask-chip edit-valuemode-btn";
      absBtn.dataset["mode"] = "abs";
      absBtn.textContent = "#";
      const syncMode = (mode: string): void => {
        pctBtn.classList.toggle("ask-chip-active", mode !== "abs");
        absBtn.classList.toggle("ask-chip-active", mode === "abs");
        decimalsField.classList.toggle("hidden", mode === "abs");
      };
      pctBtn.addEventListener("click", () => syncMode("pct"));
      absBtn.addEventListener("click", () => syncMode("abs"));
      syncMode(probe?.value_mode ?? "pct");
      modeRow.appendChild(pctBtn);
      modeRow.appendChild(absBtn);
      editCats.appendChild(modeRow);
      editCats.appendChild(decimalsField);
    } else if (kind === "classification") {
      editCats.classList.remove("hidden");
      const cats = probe?.categories ?? [];
      cats.forEach((cat, i) => editCats.appendChild(buildEditCatRow(probeId, cat, i)));
      // "+ Add category": append a fresh, empty, focusable row before this button.
      const addBtn = document.createElement("button");
      addBtn.className = "cat-add btn-secondary btn-small";
      addBtn.type = "button";
      addBtn.textContent = "+ Add category";
      addBtn.addEventListener("click", () => {
        const count = editCats.querySelectorAll(".edit-cat-row").length;
        const newRow = buildEditCatRow(probeId, "", count);
        editCats.insertBefore(newRow, addBtn);
        newRow.querySelector<HTMLInputElement>(".edit-cat-name")?.focus();
      });
      editCats.appendChild(addBtn);
    } else {
      editCats.classList.add("hidden");
    }
  }

  const intervalInput = card.querySelector<HTMLInputElement>(".edit-interval");
  if (intervalInput) intervalInput.value = probe?.interval_s ? String(probe.interval_s) : "";
  const sampleInput = card.querySelector<HTMLInputElement>(".edit-sample");
  if (sampleInput) sampleInput.value = probe?.sample_n ? String(probe.sample_n) : "";

  const editor = card.querySelector<HTMLDivElement>(".card-editor");
  if (editor) editor.classList.remove("hidden");
}

/** Read the editor back, build the full descriptor, upsert it, then close. */
function saveEditor(probeId: string): void {
  const card = ensureCard(probeId);
  const prev = probeState.get(probeId);
  const kind = prev?.kind ?? domKind(card);

  const label = card.querySelector<HTMLInputElement>(".edit-label")?.value.trim() ?? "";
  const prompt = card.querySelector<HTMLTextAreaElement>(".edit-prompt")?.value.trim() ?? "";

  let probe: ProbeDescriptor;
  if (kind === "classification" && probeChart(probeId) === "descartes") {
    // Descartes' categories/colors are managed on the grid itself (see openEditor):
    // the flat .edit-cat-row list is never populated for it, so reading it here
    // would silently wipe the whole grid. Preserve them; only label/question/
    // per-probe settings come from this editor.
    const xLabel = card.querySelector<HTMLInputElement>(".edit-xlabel")?.value.trim() ?? "";
    const yLabel = card.querySelector<HTMLInputElement>(".edit-ylabel")?.value.trim() ?? "";
    const decimalsRaw = Number(card.querySelector<HTMLInputElement>(".edit-decimals")?.value);
    const decimals = Number.isFinite(decimalsRaw) ? Math.min(2, Math.max(0, Math.round(decimalsRaw))) : 0;
    const activeModeBtn = card.querySelector<HTMLButtonElement>(
      ".edit-valuemode-btn.ask-chip-active",
    );
    const valueMode = activeModeBtn?.dataset["mode"] === "abs" ? "abs" : "pct";
    probe = {
      kind: "classification",
      id: probeId,
      label,
      question: prompt,
      categories: prev?.categories ?? [],
      chart: "descartes",
      colors: prev?.colors ?? {},
      x_label: xLabel,
      y_label: yLabel,
      value_mode: valueMode,
      pct_decimals: decimals,
    };
  } else if (kind === "classification") {
    const categories: string[] = [];
    const colors: Record<string, string> = {};
    const seen = new Set<string>();
    const rows = card.querySelectorAll<HTMLDivElement>(".edit-categories .edit-cat-row");
    rows.forEach((row) => {
      const cat = (row.querySelector<HTMLInputElement>(".edit-cat-name")?.value ?? "").trim();
      if (cat === "") return;
      const key = cat.toLowerCase();
      if (seen.has(key)) return; // skip duplicate category names
      seen.add(key);
      categories.push(cat);
      const color = row.querySelector<HTMLInputElement>(".edit-color")?.value;
      if (color) colors[cat] = color;
    });
    probe = {
      kind: "classification",
      id: probeId,
      label,
      question: prompt,
      categories,
      chart: probeChart(probeId),
      colors,
    };
  } else {
    probe = {
      kind: "open",
      id: probeId,
      label,
      instruction: prompt,
      max_words: prev?.max_words ?? 60,
    };
  }

  // Per-probe overrides (blank/0 = follow the global Window config).
  const interval = Number(card.querySelector<HTMLInputElement>(".edit-interval")?.value) || 0;
  const sample = Number(card.querySelector<HTMLInputElement>(".edit-sample")?.value) || 0;
  if (interval > 0) probe.interval_s = interval;
  if (sample > 0) probe.sample_n = Math.round(sample);

  send({ op: "upsert_probe", probe });
  probeState.set(probeId, probe);
  closeEditor(probeId);
  // The next state broadcast + syncCardsFromState refreshes the read-only
  // chrome; refresh now too so reorder/recolor show on the current snapshot.
  updateCardChrome(probeId);
  const vs = viewState.get(probeId);
  if (vs) renderDisplay(card, probeId, kind, vs.index);
}

/** Hide a card's editor (no descriptor change). */
function closeEditor(probeId: string): void {
  const card = ensureCard(probeId);
  const editor = card.querySelector<HTMLDivElement>(".card-editor");
  if (editor) editor.classList.add("hidden");
}

// ---------------------------------------------------------------------------
// Result frames: append a snapshot, sync the card, render the display (A4).
// The label / prompt / kind badge come from state (updateCardChrome), NOT msg;
// a result only updates the snapshot history + scrubber + display + meta.
// ---------------------------------------------------------------------------

function upsertResultCard(msg: ResultMsg): void {
  // 1. Append this frame as a snapshot; cap from the FRONT.
  let history = resultHistory.get(msg.probe_id);
  if (!history) {
    history = [];
    resultHistory.set(msg.probe_id, history);
  }
  history.push({ ts: msg.ts, n: msg.n, pct: msg.pct, text: msg.text });
  let dropped = 0;
  if (history.length > HISTORY_CAP) {
    dropped = history.length - HISTORY_CAP;
    history.splice(0, dropped);
  }

  let view = viewState.get(msg.probe_id);
  if (!view) {
    view = { index: history.length - 1, live: true };
    viewState.set(msg.probe_id, view);
  } else if (dropped > 0 && !view.live) {
    // Pinned-back view: shift the index left by however many we dropped.
    view.index = Math.max(0, view.index - dropped);
  }

  // The card may pre-exist (from state) or be created here (result-first).
  const card = ensureCard(msg.probe_id);

  // Drop the "Waiting for first result…" placeholder on the first real frame.
  const placeholder = card.querySelector(".result-display .empty-hint");
  if (placeholder) placeholder.remove();

  // The kind comes from state when known; fall back to the frame's kind so a
  // result that arrives before its state still renders correctly.
  const kind = probeState.has(msg.probe_id) ? cardKind(msg.probe_id) : msg.kind;
  card.dataset["kind"] = kind;
  // Make sure the chart-select is visible for classification even before state.
  const select = card.querySelector<HTMLSelectElement>(".chart-select");
  if (select) select.classList.toggle("hidden", kind !== "classification");

  // If pinned to latest, jump the view to the newest snapshot.
  if (view.live) view.index = history.length - 1;

  // Keep the selector in sync with the server-persisted chart type.
  if (kind === "classification" && select) select.value = probeChart(msg.probe_id);

  // Render the display at the current view index and refresh chrome.
  renderDisplay(card, msg.probe_id, kind, view.index);
  refreshScrubber(card, msg.probe_id, view.index);
}

// ---------------------------------------------------------------------------
// History backfill: seed each probe's snapshot history (all but the newest),
// then route the newest through upsertResultCard, which appends it, builds the
// card and renders display + scrubber exactly like a live frame.
// ---------------------------------------------------------------------------

function handleHistory(msg: HistoryMsg): void {
  for (const snaps of Object.values(msg.probes)) {
    if (!snaps.length) continue;
    const last = snaps[snaps.length - 1];
    const seed: Snapshot[] = snaps
      .slice(0, -1)
      .map((s) => ({ ts: s.ts, n: s.n, pct: s.pct, text: s.text }));
    resultHistory.set(last.probe_id, seed);
    viewState.delete(last.probe_id); // fresh view, pinned to LIVE
    upsertResultCard(last);
  }
}

// ---------------------------------------------------------------------------
// Toasts — stacking, auto-dismissing, screen-reader-announced notifications.
// Replaces the single overwrite-in-place error card: distinct errors now stack
// (up to 4), each dismisses itself, and successes get lightweight feedback too.
// ---------------------------------------------------------------------------

type ToastKind = "error" | "success" | "info";

const TOAST_MAX = 4;

function showToast(message: string, kind: ToastKind = "info", ttlMs = 6000): void {
  const container = el<HTMLDivElement>("toasts");
  const toast = document.createElement("div");
  toast.className = `toast toast-${kind}`;
  // "alert" announces assertively on insert; successes/infos stay polite.
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = (kind === "error" ? "⚠ " : "") + message;
  toast.appendChild(text);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.textContent = "✕";
  close.setAttribute("aria-label", "Dismiss notification");
  close.addEventListener("click", () => toast.remove());
  toast.appendChild(close);
  container.appendChild(toast);
  while (container.childElementCount > TOAST_MAX && container.firstChild) {
    container.removeChild(container.firstChild);
  }
  window.setTimeout(() => toast.remove(), ttlMs);
}

function showError(message: string): void {
  showToast(message, "error", 12000);
}

// ---------------------------------------------------------------------------
// Live chat feed
// ---------------------------------------------------------------------------

// Autoscroll only while the user is already at the "newest" edge; scrolling away
// to read pauses it (new lines pile up behind a "N new" jump button) instead of
// yanking the view back 4×/s. The newest edge is the BOTTOM by default, or the
// TOP when the user flips the feed to newest-first (persisted preference).
const FEED_STICK_PX = 40;
const FEED_MAX = 400;
const FEED_ORDER_KEY = "viewlyt.feednewestfirst";
let feedUnread = 0;
let feedNewestFirst = lsGet(FEED_ORDER_KEY) === "1";

/** Is the view parked at the edge where the newest message lands? */
function feedAtStickEdge(feed: HTMLElement): boolean {
  return feedNewestFirst
    ? feed.scrollTop < FEED_STICK_PX
    : feed.scrollHeight - feed.scrollTop - feed.clientHeight < FEED_STICK_PX;
}

function updateFeedJump(): void {
  const jump = document.getElementById("feed-jump");
  if (!jump) return;
  jump.classList.toggle("hidden", feedUnread === 0);
  jump.textContent = `${feedNewestFirst ? "↑" : "↓"} ${feedUnread} new`;
}

/** Snap the view to the newest edge (bottom, or top in newest-first mode). */
function jumpFeedToEdge(): void {
  const feed = document.getElementById("feed");
  if (!feed) return;
  feed.scrollTop = feedNewestFirst ? 0 : feed.scrollHeight;
  feedUnread = 0;
  updateFeedJump();
}

function makeFeedLine(author: string, text: string): HTMLDivElement {
  const line = document.createElement("div");
  const a = document.createElement("span");
  a.className = "feed-author";
  a.textContent = author + ": ";
  line.appendChild(a);
  line.appendChild(document.createTextNode(text));
  return line;
}

/**
 * Reflect the current feed direction onto the DOM + the toggle button. Does NOT
 * reorder existing lines — the caller does that when the user flips the mode.
 */
function applyFeedOrder(): void {
  const wrap = document.querySelector(".feed-wrap");
  if (wrap) wrap.classList.toggle("newest-first", feedNewestFirst);
  const btn = document.getElementById("feed-order");
  if (btn) {
    const arrow = btn.querySelector(".feed-order-arrow");
    if (arrow) arrow.textContent = feedNewestFirst ? "↑" : "↓";
    btn.setAttribute("aria-pressed", String(feedNewestFirst));
    btn.title = feedNewestFirst
      ? "Newest messages appear at the top (stack grows down). Click for newest at the bottom."
      : "Newest messages appear at the bottom (stack grows up). Click for newest at the top.";
  }
  updateFeedJump();
}

/** Flip the DOM order of the already-rendered feed lines (in place). */
function reverseFeedDom(feed: HTMLElement): void {
  const lines = Array.from(feed.children);
  if (lines.length < 2) return; // nothing (or just a placeholder) to flip
  const frag = document.createDocumentFragment();
  for (let i = lines.length - 1; i >= 0; i--) frag.appendChild(lines[i]!);
  feed.appendChild(frag);
}

function wireFeed(): void {
  const feed = document.getElementById("feed");
  const jump = document.getElementById("feed-jump");
  const order = document.getElementById("feed-order");
  if (jump) jump.addEventListener("click", jumpFeedToEdge);
  if (order && feed) {
    order.addEventListener("click", () => {
      reverseFeedDom(feed);
      feedNewestFirst = !feedNewestFirst;
      lsSet(FEED_ORDER_KEY, feedNewestFirst ? "1" : "0");
      applyFeedOrder();
      jumpFeedToEdge(); // land on the newest edge after the flip
    });
  }
  if (feed) {
    // Scrolling back to the newest edge by hand also clears the unread counter.
    feed.addEventListener("scroll", () => {
      if (feedUnread > 0 && feedAtStickEdge(feed)) {
        feedUnread = 0;
        updateFeedJump();
      }
    });
  }
  applyFeedOrder(); // reflect the persisted preference on load
}

function appendFeed(items: { author: string; text: string }[]): void {
  const feed = document.getElementById("feed");
  if (!feed || items.length === 0) return;
  // Drop the "No messages yet…" placeholder on the first real batch.
  const placeholder = feed.querySelector(":scope > .empty-hint");
  if (placeholder) placeholder.remove();
  const stick = feedAtStickEdge(feed);
  const frag = document.createDocumentFragment();

  if (feedNewestFirst) {
    // Newest at the top: emit the batch reversed (newest → oldest) and prepend.
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i]!;
      frag.appendChild(makeFeedLine(it.author, it.text));
    }
    const before = feed.scrollHeight;
    feed.insertBefore(frag, feed.firstChild);
    const added = feed.scrollHeight - before; // height gained above the fold
    if (stick) {
      feed.scrollTop = 0;
    } else {
      // Keep the content the user is reading stationary as lines push in above,
      // then surface an "↑ N new" nudge toward the top.
      feed.scrollTop += added;
      feedUnread += items.length;
      updateFeedJump();
    }
    // Trim the oldest (bottom) — off-screen below, so it never shifts the view.
    while (feed.childElementCount > FEED_MAX && feed.lastChild) {
      feed.removeChild(feed.lastChild);
    }
  } else {
    // Newest at the bottom (default): append in order.
    for (const { author, text } of items) frag.appendChild(makeFeedLine(author, text));
    feed.appendChild(frag);
    while (feed.childElementCount > FEED_MAX && feed.firstChild) {
      feed.removeChild(feed.firstChild);
    }
    if (stick) {
      feed.scrollTop = feed.scrollHeight;
    } else {
      feedUnread += items.length;
      updateFeedJump();
    }
  }
}

// ---------------------------------------------------------------------------
// State message handler
// ---------------------------------------------------------------------------

function setProc(active: boolean, latencyMs?: number | null, avgLatencyMs?: number | null): void {
  const e = el<HTMLSpanElement>("proc");
  // Top-of-page loading spinner: visible only while an analysis is in flight.
  const spinner = document.getElementById("spinner");
  if (spinner) spinner.classList.toggle("hidden", !active);
  if (active) {
    e.textContent = "analyzing…";
    e.style.color = "#fbbf24";
  } else {
    e.textContent = latencyMs != null ? `${latencyMs} ms` : "idle";
    e.style.color = "";
  }
  const avgEl = document.getElementById("avg-latency");
  if (avgEl && avgLatencyMs != null) avgEl.textContent = `${avgLatencyMs} ms`;
}

// ---------------------------------------------------------------------------
// Cost frame (A5): update the Cost stat card. Shows USD when the provider
// reports it, else a compact token count. Both elements are optional — bail
// quietly if the stats-bar hasn't got the Cost card.
// ---------------------------------------------------------------------------

// Budget cap (B8): the last-known spending budget in USD (0 = off) and the most
// recent cost frame. handleState refreshes the cost label's "/budget" suffix on
// a budget change by re-rendering from this cached frame.
let currentBudget = 0;
let lastCost: CostMsg | null = null;

/** "1.2k tok" for >=1000, else "842 tok". */
function fmtTok(n: number): string {
  return n >= 1000 ? (n / 1000).toFixed(1) + "k tok" : n + " tok";
}

/**
 * Render `value` with a leading "+" when it is >= 0 (a negative value already
 * carries its own "-" from `render`, so we never double it).
 */
function signed(value: number, render: (n: number) => string): string {
  return (value >= 0 ? "+" : "") + render(value);
}

function handleCost(msg: CostMsg): void {
  // Cache the frame so a later budget change can re-render the "/budget" suffix.
  lastCost = msg;

  const main = document.getElementById("cost");
  const delta = document.getElementById("cost-delta");
  if (!main || !delta) return;

  if (currentBudget > 0) {
    // Budget mode: show spent / budget regardless of whether the provider
    // reports a dollar cost (0 spend renders as "$0.0000 / $<budget>").
    main.textContent =
      "$" + msg.cost_total.toFixed(4) + " / $" + currentBudget.toFixed(2);
  } else {
    const hasCost = msg.cost_total > 0;
    main.textContent = hasCost ? "$" + msg.cost_total.toFixed(4) : fmtTok(msg.tokens_total);
  }

  const hasCost = msg.cost_total > 0;
  delta.textContent = hasCost
    ? signed(msg.cost_delta, (n) => "$" + n.toFixed(4))
    : signed(msg.tokens_delta, fmtTok);
}

function handleState(msg: StateMsg): void {
  // Window inputs
  setInputVal("n", msg.window.n);
  setInputVal("gap", msg.window.gap);
  el<HTMLSelectElement>("mode").value = msg.window.mode;
  if (typeof msg.window.dedupe === "boolean")
    el<HTMLInputElement>("dedupe").checked = msg.window.dedupe;
  if (typeof msg.window.merge_authors === "boolean")
    el<HTMLInputElement>("merge_authors").checked = msg.window.merge_authors;
  if (typeof msg.window.capacity === "number")
    setInputVal("capacity", msg.window.capacity);

  // Model inputs
  setInputVal("base_url", msg.model.base_url);
  setInputVal("model", msg.model.model);
  // api_key is intentionally not filled (write-only)

  // Spending budget (B8): remember it, mirror it onto the #budget field (when
  // present) and re-render the Cost card so its "/budget" suffix tracks changes.
  currentBudget = Number(msg.model.budget) || 0;
  const budgetEl = document.getElementById("budget") as HTMLInputElement | null;
  if (budgetEl) budgetEl.value = String(currentBudget);
  if (lastCost) handleCost(lastCost);

  // Analysis language (header selector)
  const langEl = document.getElementById("language") as HTMLSelectElement | null;
  if (langEl && typeof msg.model.language === "string" && msg.model.language) {
    langEl.value = msg.model.language;
  }

  // Reverse-map base_url -> provider dropdown
  const providerSel = el<HTMLSelectElement>("provider");
  const matchedKey = Object.keys(PROVIDERS).find(
    (k) => PROVIDERS[k].base_url === msg.model.base_url
  );
  if (matchedKey !== undefined) {
    providerSel.value = matchedKey;
  }

  // Stats — honor the runtime flags so a dashboard connecting mid-analysis or
  // after spending shows the truth instead of "idle / $0".
  el<HTMLSpanElement>("ingested").textContent = String(msg.ingested);
  setProc(!!msg.processing, msg.latency_ms, msg.avg_latency_ms);

  // Budget stop is sticky state, not a transient error: show a persistent header
  // chip while analyses are paused by the spending cap (late joiners see it too).
  const budgetFlag = document.getElementById("budget-flag");
  if (budgetFlag) budgetFlag.classList.toggle("hidden", !msg.budget_blocked);
  if (!lastCost && ((msg.tokens_total ?? 0) > 0 || (msg.cost_total ?? 0) > 0)) {
    handleCost({
      type: "cost",
      tokens_total: msg.tokens_total ?? 0,
      tokens_delta: 0,
      cost_total: msg.cost_total ?? 0,
      cost_delta: 0,
    });
  }

  // Probes — rebuild the descriptor map, then mirror it onto the cards.
  probeState.clear();
  for (const p of msg.probes) {
    probeState.set(p.id, p);
  }
  syncCardsFromState();

  // A state frame is the ack of an ask-bar rewrite — restore the send button.
  if (askPending) setAskPending(false);
}

// ---------------------------------------------------------------------------
// Inbound dashboard WebSocket
// ---------------------------------------------------------------------------

let dashboardWs: WebSocket;

function connectDashboard(): void {
  dashboardWs = new WebSocket(wsBase + "/dashboard");

  dashboardWs.onopen = () => {
    setStatus(true);
  };

  dashboardWs.onclose = () => {
    setStatus(false);
    setTimeout(connectDashboard, 3000);
  };

  dashboardWs.onerror = () => {
    // onclose will fire after onerror
  };

  dashboardWs.onmessage = (event: MessageEvent) => {
    let msg: InboundMsg;
    try {
      msg = JSON.parse(event.data as string) as InboundMsg;
    } catch {
      return;
    }

    switch (msg.type) {
      case "state":
        handleState(msg);
        break;
      case "result":
        upsertResultCard(msg);
        break;
      case "stat":
        el<HTMLSpanElement>("ingested").textContent = String(msg.ingested);
        el<HTMLSpanElement>("buffer").textContent = String(msg.buffer);
        break;
      case "error":
        showError(msg.message);
        break;
      case "chat":
        appendFeed(msg.items);
        break;
      case "proc":
        setProc(msg.active, msg.latency_ms, msg.avg_latency_ms);
        break;
      case "cost":
        handleCost(msg);
        break;
      case "suggestions":
        renderSuggestions(msg.probes);
        break;
      case "history":
        handleHistory(msg);
        break;
    }
  };
}

function connectControl(): void {
  controlWs = new WebSocket(wsBase + "/control");

  controlWs.onopen = () => {
    flushQueue();
  };

  controlWs.onclose = () => {
    setTimeout(connectControl, 3000);
  };

  controlWs.onerror = () => {
    // onclose fires next
  };
}

// ---------------------------------------------------------------------------
// Probe kind toggle
// ---------------------------------------------------------------------------

function updateProbeFieldVisibility(): void {
  const kind = el<HTMLSelectElement>("probe-kind").value;
  const classFields = el<HTMLDivElement>("classification-fields");
  const openFields = el<HTMLDivElement>("open-fields");
  if (kind === "classification") {
    classFields.classList.remove("hidden");
    openFields.classList.add("hidden");
  } else {
    classFields.classList.add("hidden");
    openFields.classList.remove("hidden");
  }
}

// ---------------------------------------------------------------------------
// Probe id generation
// ---------------------------------------------------------------------------

let probeCounter = 0;

// ---------------------------------------------------------------------------
// Ask-bar kind state + transient "rewriting…" tracking
// ---------------------------------------------------------------------------

type AskKind = "auto" | "open" | "classify";
let askKind: AskKind = "auto";

// The ask-bar fires an async server-side rewrite; while it is in flight we
// disable the send button. It is restored by the next state frame (the rewrite
// broadcasts "state") or by a safety timeout if nothing comes back.
let askPending = false;
let askRestoreTimer: number | undefined;

function setAskPending(pending: boolean): void {
  askPending = pending;
  const btn = document.getElementById("ask-send") as HTMLButtonElement | null;
  if (btn) {
    btn.disabled = pending;
    btn.textContent = pending ? "Rewriting…" : "Add";
  }
  if (!pending && askRestoreTimer !== undefined) {
    clearTimeout(askRestoreTimer);
    askRestoreTimer = undefined;
  }
}

// The Suggest button (B10) fires an off-loop server task that proposes two
// probes; while it is in flight we disable the button and show "Thinking…".
// It is restored by the next suggestions frame or by a safety timeout.
let suggestPending = false;
let suggestRestoreTimer: number | undefined;

function setSuggestPending(pending: boolean): void {
  suggestPending = pending;
  const btn = document.getElementById("ask-suggest") as HTMLButtonElement | null;
  if (btn) {
    btn.disabled = pending;
    btn.textContent = pending ? "Thinking…" : "Suggest";
  }
  if (!pending && suggestRestoreTimer !== undefined) {
    clearTimeout(suggestRestoreTimer);
    suggestRestoreTimer = undefined;
  }
}

// The Split button mirrors Suggest: an off-loop server task decomposes the
// typed request into elementary probes, answered by the same suggestions frame.
let splitPending = false;
let splitRestoreTimer: number | undefined;

function setSplitPending(pending: boolean): void {
  splitPending = pending;
  const btn = document.getElementById("ask-split") as HTMLButtonElement | null;
  if (btn) {
    btn.disabled = pending;
    btn.textContent = pending ? "Splitting…" : "Split";
  }
  if (!pending && splitRestoreTimer !== undefined) {
    clearTimeout(splitRestoreTimer);
    splitRestoreTimer = undefined;
  }
}

/**
 * Render the (up to two) suggested probes as clickable chips in #suggestions.
 * Each chip shows the probe label plus a short hint of its kind/question/
 * instruction. Clicking a chip upserts that probe verbatim (it already carries
 * an id), updates local probeState, and clears the container. A tiny "Dismiss"
 * affordance clears it too. Guards against #suggestions being absent.
 */
function renderSuggestions(probes: ProbeDescriptor[]): void {
  setSuggestPending(false);
  setSplitPending(false);
  const container = document.getElementById("suggestions");
  if (!container) return;
  container.innerHTML = "";

  // Suggest sends 2; Split (decompose) sends up to 4 elementary probes.
  const shown = probes.slice(0, 4);
  for (const probe of shown) {
    if (typeof probe !== "object" || probe === null) continue;

    const chip = document.createElement("button");
    chip.className = "suggest-chip";
    chip.type = "button";

    const label = document.createElement("span");
    label.className = "suggest-chip-label";
    label.textContent = probe.label || probe.id || "(probe)";
    chip.appendChild(label);

    const hintText =
      probe.kind === "classification"
        ? (probe.question ?? "")
        : (probe.instruction ?? "");
    const hint = document.createElement("span");
    hint.className = "suggest-chip-hint";
    hint.textContent = `${probe.kind ?? "open"} · ${hintText}`.trim();
    chip.appendChild(hint);

    chip.addEventListener("click", () => {
      send({ op: "upsert_probe", probe });
      if (typeof probe.id === "string" && probe.id !== "") {
        probeState.set(probe.id, probe);
        syncCardsFromState();
      }
      // Remove only this chip — a Split proposes several probes and the user
      // may want to add more than one. Clear the tray when the last chip goes.
      chip.remove();
      if (!container.querySelector(".suggest-chip")) container.innerHTML = "";
    });

    container.appendChild(chip);
  }

  if (shown.length > 0) {
    const dismiss = document.createElement("button");
    dismiss.className = "suggest-dismiss btn-secondary btn-small";
    dismiss.type = "button";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", () => {
      container.innerHTML = "";
    });
    container.appendChild(dismiss);
  }
}

function labelToId(label: string): string {
  const slug = label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || `probe-${++probeCounter}`;
}

// ---------------------------------------------------------------------------
// Button wiring
// ---------------------------------------------------------------------------

/** Swap a button's label for ~1.4s ("Copied ✓") as inline action feedback. */
function flashButton(btn: HTMLButtonElement, text: string): void {
  if (btn.dataset["flashing"] === "1") return;
  const original = btn.textContent ?? "";
  btn.dataset["flashing"] = "1";
  btn.textContent = text;
  window.setTimeout(() => {
    btn.textContent = original;
    delete btn.dataset["flashing"];
  }, 1400);
}

function wireButtons(): void {
  el("apply-window").addEventListener("click", () => {
    send({
      op: "set_window",
      n: Number(inputVal("n")),
      gap: Number(inputVal("gap")),
      mode: el<HTMLSelectElement>("mode").value,
      dedupe: el<HTMLInputElement>("dedupe").checked,
      merge_authors: el<HTMLInputElement>("merge_authors").checked,
      capacity: Number(inputVal("capacity")),
    });
    showToast("Window settings applied.", "success");
  });

  el("pause").addEventListener("click", () => send({ op: "pause" }));
  el("resume").addEventListener("click", () => send({ op: "resume" }));
  el("clear").addEventListener("click", () => send({ op: "clear" }));
  el("force-run").addEventListener("click", () => send({ op: "force_run" }));

  // Analysis language (header selector): applies immediately; set_model keeps the
  // rest of the model config unchanged (omitted fields fall back to the current).
  el<HTMLSelectElement>("language").addEventListener("change", () => {
    send({ op: "set_model", language: el<HTMLSelectElement>("language").value });
  });

  el("apply-model").addEventListener("click", () => {
    const apiKey = inputVal("api_key");
    const op: Record<string, string | number> = {
      op: "set_model",
      base_url: inputVal("base_url"),
      model: inputVal("model"),
    };
    if (apiKey !== "") {
      op["api_key"] = apiKey;
    }
    // Spending budget (B8): always include it (>= 0; 0 = off). The field is
    // optional in the DOM — default to 0 when absent.
    const budgetEl = document.getElementById("budget") as HTMLInputElement | null;
    op["budget"] = budgetEl ? Math.max(0, Number(budgetEl.value) || 0) : 0;
    send(op);
    // Clear the api_key input after sending
    setInputVal("api_key", "");
    showToast("Model settings applied.", "success");
  });

  el("add-probe").addEventListener("click", () => {
    const kind = el<HTMLSelectElement>("probe-kind").value;
    // Label is optional: slug it when present, else generate a counter id.
    // The server auto-generates a label from the prompt when it is empty.
    const label = inputVal("probe-label");
    const id = labelToId(label);

    let probe: Record<string, unknown>;
    if (kind === "classification") {
      const cats = inputVal("probe-categories")
        .split(",")
        .map((s) => s.trim())
        .filter((s) => s.length > 0);
      probe = {
        kind: "classification",
        id,
        label,
        question: inputVal("probe-question"),
        categories: cats,
      };
    } else {
      probe = {
        kind: "open",
        id,
        label,
        instruction: inputVal("probe-instruction"),
      };
    }

    send({ op: "upsert_probe", probe });

    // Optimistically add to local state so a card appears immediately; the
    // server's state broadcast reconciles it shortly after.
    probeState.set(id, probe as unknown as ProbeDescriptor);
    syncCardsFromState();

    // Reset form
    setInputVal("probe-label", "");
    setInputVal("probe-question", "");
    setInputVal("probe-categories", "");
    setInputVal("probe-instruction", "");
    showToast("Probe added — first analysis runs right away.", "success");
  });

  el("import-json").addEventListener("click", () => {
    const statusEl = el<HTMLSpanElement>("import-json-status");
    const raw = el<HTMLTextAreaElement>("probe-json").value.trim();
    if (!raw) {
      statusEl.textContent = "Paste some JSON first.";
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      statusEl.textContent =
        "Invalid JSON: " + (err instanceof Error ? err.message : "parse error");
      return;
    }

    // Accept a single object or an array of probe objects.
    const items = Array.isArray(parsed) ? parsed : [parsed];
    let added = 0;
    for (const item of items) {
      if (typeof item !== "object" || item === null) continue;
      const probe = item as Record<string, unknown>;
      if (typeof probe["kind"] !== "string") probe["kind"] = "open";
      if (typeof probe["id"] !== "string" || probe["id"] === "") {
        const lbl = typeof probe["label"] === "string" ? probe["label"] : "";
        probe["id"] = labelToId(lbl);
      }
      send({ op: "upsert_probe", probe });
      probeState.set(probe["id"] as string, probe as unknown as ProbeDescriptor);
      added += 1;
    }

    syncCardsFromState();
    el<HTMLTextAreaElement>("probe-json").value = "";
    statusEl.textContent = added > 0 ? `Imported ${added} probe(s).` : "No probes found.";
  });

  // ---- Ask-bar ----

  function setAskKind(kind: AskKind): void {
    askKind = kind;
    el<HTMLButtonElement>("ask-auto").classList.toggle("ask-chip-active", kind === "auto");
    el<HTMLButtonElement>("ask-open").classList.toggle("ask-chip-active", kind === "open");
    el<HTMLButtonElement>("ask-classify").classList.toggle("ask-chip-active", kind === "classify");
    const catsEl = el<HTMLInputElement>("ask-categories");
    // Categories only matter for the explicit "classify" kind.
    if (kind === "classify") {
      catsEl.classList.remove("hidden");
    } else {
      catsEl.classList.add("hidden");
    }
  }

  el("ask-auto").addEventListener("click", () => setAskKind("auto"));
  el("ask-open").addEventListener("click", () => setAskKind("open"));
  el("ask-classify").addEventListener("click", () => setAskKind("classify"));

  function submitAskBar(): void {
    if (askPending) return;
    const text = el<HTMLInputElement>("ask-text").value.trim();
    if (!text) return;

    // The "smart" path: the server rewrites this prompt into a probe spec, then
    // builds + stores the probe (assigning its id) and broadcasts a state frame.
    // In "auto" the LLM also DECIDES the kind (open vs classification).
    const cats = el<HTMLInputElement>("ask-categories").value
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    const kind =
      askKind === "auto" ? "auto" : askKind === "classify" ? "classification" : "open";
    const op: Record<string, unknown> = {
      op: "rewrite_probe",
      kind,
      text,
    };
    if (askKind === "classify" && cats.length > 0) {
      op["categories"] = cats;
    }
    send(op);

    el<HTMLInputElement>("ask-text").value = "";
    el<HTMLInputElement>("ask-categories").value = "";

    // Show a transient "rewriting…" state; restored by the next state frame
    // (handleState) or by this safety timeout, whichever comes first.
    setAskPending(true);
    askRestoreTimer = window.setTimeout(() => {
      setAskPending(false);
    }, 8000);
  }

  el("ask-send").addEventListener("click", submitAskBar);

  // Suggest (B10): ask the server to propose two probes for the current chat +
  // the typed (possibly empty) request. Shows a transient "Thinking…" state,
  // restored on the suggestions frame (renderSuggestions) or this safety timeout.
  function requestSuggestions(): void {
    if (suggestPending) return;
    const text = el<HTMLInputElement>("ask-text").value.trim();
    send({ op: "suggest_probes", text });
    setSuggestPending(true);
    suggestRestoreTimer = window.setTimeout(() => {
      setSuggestPending(false);
    }, 10000);
  }

  const suggestBtn = document.getElementById("ask-suggest");
  if (suggestBtn) suggestBtn.addEventListener("click", requestSuggestions);

  // Split: decompose the typed request into elementary probes (server-side,
  // one cheap LLM call at creation time); answered by a suggestions frame.
  function requestSplit(): void {
    if (splitPending) return;
    const text = el<HTMLInputElement>("ask-text").value.trim();
    if (!text) {
      showToast("Type the broad question to split first.", "info");
      return;
    }
    send({ op: "decompose_probe", text });
    setSplitPending(true);
    splitRestoreTimer = window.setTimeout(() => {
      setSplitPending(false);
    }, 15000);
  }

  const splitBtn = document.getElementById("ask-split");
  if (splitBtn) splitBtn.addEventListener("click", requestSplit);

  el<HTMLInputElement>("ask-text").addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitAskBar();
    }
  });

  // ---- JSON batch file upload ----

  el<HTMLInputElement>("probe-json-file").addEventListener("change", (e: Event) => {
    const input = e.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev: ProgressEvent<FileReader>) => {
      const text = ev.target?.result;
      if (typeof text === "string") {
        el<HTMLTextAreaElement>("probe-json").value = text;
        el<HTMLButtonElement>("import-json").click();
      }
    };
    reader.readAsText(file);
    // Reset file input so the same file can be re-uploaded if needed.
    input.value = "";
  });

  // ---- Reset button ----

  el("reset-state").addEventListener("click", () => {
    if (
      window.confirm("Forget the saved setup (probes, model, window) and reset to defaults?")
    ) {
      send({ op: "reset_state" });
      try {
        const keysToRemove: string[] = [];
        for (let i = 0; i < localStorage.length; i++) {
          const k = localStorage.key(i);
          if (k && k.startsWith("viewlyt.")) keysToRemove.push(k);
        }
        for (const k of keysToRemove) {
          localStorage.removeItem(k);
        }
      } catch {
        // Storage may be unavailable; ignore.
      }
    }
  });

  el("probe-kind").addEventListener("change", updateProbeFieldVisibility);

  el<HTMLSelectElement>("provider").addEventListener("change", () => {
    const key = el<HTMLSelectElement>("provider").value;
    const info = PROVIDERS[key];
    if (!info) return;
    setInputVal("base_url", info.base_url);
    setInputVal("model", info.model);
    el<HTMLInputElement>("api_key").placeholder = info.keyHint;
  });

  el("toggle-key").addEventListener("click", () => {
    const apiKeyEl = el<HTMLInputElement>("api_key");
    const toggleBtn = el<HTMLButtonElement>("toggle-key");
    if (apiKeyEl.type === "password") {
      apiKeyEl.type = "text";
      toggleBtn.textContent = "hide";
    } else {
      apiKeyEl.type = "password";
      toggleBtn.textContent = "show";
    }
  });

  el("copy-snippet").addEventListener("click", () => {
    const text = el<HTMLTextAreaElement>("snippet").value;
    navigator.clipboard
      .writeText(text)
      .then(() => flashButton(el<HTMLButtonElement>("copy-snippet"), "Copied ✓"))
      .catch(() => {
        // Fallback: select the textarea so the user can copy manually
        el<HTMLTextAreaElement>("snippet").select();
      });
  });

  el("copy-bookmarklet").addEventListener("click", () => {
    const bm = el<HTMLTextAreaElement>("bookmarklet");
    navigator.clipboard
      .writeText(bm.value)
      .then(() => flashButton(el<HTMLButtonElement>("copy-bookmarklet"), "Copied ✓"))
      .catch(() => bm.select());
  });
}

// ---------------------------------------------------------------------------
// Snippet loader
// ---------------------------------------------------------------------------

async function loadSnippet(): Promise<void> {
  try {
    const r = await fetch("/snippet.js");
    const text = await r.text();
    el<HTMLTextAreaElement>("snippet").value = text;
    // Build a one-click bookmark ("bookmarklet") from the same snippet.
    const bm = document.getElementById("bookmarklet") as HTMLTextAreaElement | null;
    if (bm) bm.value = "javascript:" + encodeURIComponent(text);
  } catch {
    el<HTMLTextAreaElement>("snippet").value =
      "// Could not load snippet.js from server.";
  }
}

// ---------------------------------------------------------------------------
// localStorage helpers (guarded against restricted contexts)
// ---------------------------------------------------------------------------

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Ignore — storage may be disabled (private mode, sandboxed iframe, etc.)
  }
}

// ---------------------------------------------------------------------------
// Collapsible group wiring
// ---------------------------------------------------------------------------

function wireGroups(): void {
  const headers = document.querySelectorAll<HTMLButtonElement>(".group-header");
  headers.forEach((btn) => {
    const group = btn.closest<HTMLElement>(".group");
    if (!group) return;

    // Restore collapsed state from localStorage when the group has an id.
    if (group.id) {
      const stored = lsGet("viewlyt.collapsed." + group.id);
      if (stored === "1") {
        group.classList.add("collapsed");
      } else {
        // Explicitly remove in case the class was set in HTML — default is expanded.
        group.classList.remove("collapsed");
      }
    }

    btn.addEventListener("click", () => {
      group.classList.toggle("collapsed");
      // Persist the new state when the group has an id.
      if (group.id) {
        lsSet(
          "viewlyt.collapsed." + group.id,
          group.classList.contains("collapsed") ? "1" : "0",
        );
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

updateProbeFieldVisibility();
wireButtons();
wireFeed();
wireGroups();
connectDashboard();
connectControl();
loadSnippet();
