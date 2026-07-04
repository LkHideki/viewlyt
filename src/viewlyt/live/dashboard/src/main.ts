// viewlyt.live — Control Panel main script
// Strict TypeScript, dependency-free.

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
  max_words?: number;
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
// Markdown-lite renderer (safe — escapes HTML first, no raw injection)
// ---------------------------------------------------------------------------

function renderMarkdown(src: string): string {
  // 1. Escape HTML entities so source text is never interpreted as markup.
  const escaped = src
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  // 2. Process line-by-line for block constructs, then apply inline transforms.
  const lines = escaped.split("\n");
  const out: string[] = [];
  let inUl = false;
  let inOl = false;

  const closeList = (): void => {
    if (inUl) { out.push("</ul>"); inUl = false; }
    if (inOl) { out.push("</ol>"); inOl = false; }
  };

  const applyInline = (text: string): string =>
    text
      // Headings already handled at line level; inline bold/italic/code:
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/\*(.+?)\*/g, "<em>$1</em>")
      .replace(/_(.+?)_/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

  for (const line of lines) {
    // Headings: 1–6 hashes at start of line.
    const headingMatch = /^(#{1,6}) (.+)$/.exec(line);
    if (headingMatch) {
      closeList();
      out.push(`<strong>${applyInline(headingMatch[2])}</strong>`);
      continue;
    }
    // Unordered list: lines starting with '- ' or '* '.
    const ulMatch = /^[*-] (.+)$/.exec(line);
    if (ulMatch) {
      if (!inUl) { if (inOl) { out.push("</ol>"); inOl = false; } out.push("<ul>"); inUl = true; }
      out.push(`<li>${applyInline(ulMatch[1])}</li>`);
      continue;
    }
    // Ordered list: lines starting with 'N. '.
    const olMatch = /^\d+\. (.+)$/.exec(line);
    if (olMatch) {
      if (!inOl) { if (inUl) { out.push("</ul>"); inUl = false; } out.push("<ol>"); inOl = true; }
      out.push(`<li>${applyInline(olMatch[1])}</li>`);
      continue;
    }
    // Regular line: close any open list, emit with inline transforms.
    closeList();
    out.push(applyInline(line));
  }
  closeList();

  // 3. Join lines: list tags get no separator; other lines get <br>.
  let html = "";
  for (let i = 0; i < out.length; i++) {
    const cur = out[i];
    html += cur;
    if (i < out.length - 1) {
      const next = out[i + 1];
      // Don't add <br> between/around block-level list tags.
      const curIsBlock = /^<\/?(?:ul|ol|li)/.test(cur);
      const nextIsBlock = /^<\/?(?:ul|ol|li)/.test(next);
      if (!curIsBlock && !nextIsBlock) {
        html += "<br>";
      }
    }
  }
  return html;
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
  | "violin";
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

/** Map a 0–100 value to one of the 8 unicode block characters. */
function sparkline(values: number[]): string {
  const blocks = "▁▂▃▄▅▆▇█";
  return values
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

/**
 * Build the classification .result-display content for the snapshot at `index`:
 * the chosen visualization wrapped in .chart-viz, plus (for every chart EXCEPT
 * "bars") the .cat-stats table underneath it.
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
    default:
      viz.appendChild(renderBars(probeId, views));
      break;
  }
  body.appendChild(viz);

  if (type !== "bars") {
    body.appendChild(buildStats(probeId, views));
  }
  return body;
}

/** Build the open .result-display: a single markdown-rendered snapshot text. */
function buildOpenBody(probeId: string, index: number): HTMLElement {
  const body = document.createElement("div");
  body.className = "result-display";

  const snap = resultHistory.get(probeId)?.[index];
  const textEl = document.createElement("p");
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
  const next =
    kind === "classification"
      ? buildClassificationBody(probeId, index)
      : buildOpenBody(probeId, index);
  const existing = card.querySelector<HTMLDivElement>(".result-display");
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
  const headerMeta = card.querySelector<HTMLSpanElement>(".result-meta");

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
  if (headerMeta && snap) headerMeta.textContent = snapStamp(snap);
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

  // --- header ---
  const header = document.createElement("div");
  header.className = "result-header";

  const badge = document.createElement("span");
  badge.className = "result-kind-badge";
  badge.textContent = cardKind(probeId);
  header.appendChild(badge);

  const titles = document.createElement("div");
  titles.className = "result-titles";
  const labelEl = document.createElement("span");
  labelEl.className = "result-label";
  const promptEl = document.createElement("span");
  promptEl.className = "result-prompt";
  titles.appendChild(labelEl);
  titles.appendChild(promptEl);
  header.appendChild(titles);

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
    send({ op: "upsert_probe", probe });
    probeState.set(probe.id, probe);
    const vs = viewState.get(probeId);
    renderDisplay(card, probeId, domKind(card), vs ? vs.index : 0);
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

  const metaEl = document.createElement("span");
  metaEl.className = "result-meta";

  right.appendChild(metaEl);
  right.appendChild(select);
  right.appendChild(editBtn);
  right.appendChild(removeBtn);
  header.appendChild(right);
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
    if (pid !== undefined && !probeState.has(pid)) card.remove();
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
    if (kind === "classification") {
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
  if (kind === "classification") {
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

// Autoscroll only while the user is already at the bottom; scrolling up to read
// pauses it (new lines pile up behind a "↓ N new" jump button) instead of
// yanking the view back down 4×/s.
const FEED_STICK_PX = 40;
let feedUnread = 0;

function feedAtBottom(feed: HTMLElement): boolean {
  return feed.scrollHeight - feed.scrollTop - feed.clientHeight < FEED_STICK_PX;
}

function updateFeedJump(): void {
  const jump = document.getElementById("feed-jump");
  if (!jump) return;
  jump.classList.toggle("hidden", feedUnread === 0);
  jump.textContent = `↓ ${feedUnread} new`;
}

function jumpFeedToBottom(): void {
  const feed = document.getElementById("feed");
  if (!feed) return;
  feed.scrollTop = feed.scrollHeight;
  feedUnread = 0;
  updateFeedJump();
}

function wireFeed(): void {
  const feed = document.getElementById("feed");
  const jump = document.getElementById("feed-jump");
  if (jump) jump.addEventListener("click", jumpFeedToBottom);
  if (feed) {
    // Scrolling back to the bottom by hand also clears the unread counter.
    feed.addEventListener("scroll", () => {
      if (feedUnread > 0 && feedAtBottom(feed)) {
        feedUnread = 0;
        updateFeedJump();
      }
    });
  }
}

function appendFeed(items: { author: string; text: string }[]): void {
  const feed = document.getElementById("feed");
  if (!feed) return;
  // Drop the "No messages yet…" placeholder on the first real batch.
  const placeholder = feed.querySelector(":scope > .empty-hint");
  if (placeholder) placeholder.remove();
  const stick = feedAtBottom(feed);
  const frag = document.createDocumentFragment();
  for (const { author, text } of items) {
    const line = document.createElement("div");
    const a = document.createElement("span");
    a.className = "feed-author";
    a.textContent = author + ": ";
    line.appendChild(a);
    line.appendChild(document.createTextNode(text));
    frag.appendChild(line);
  }
  feed.appendChild(frag);
  while (feed.childElementCount > 400 && feed.firstChild) {
    feed.removeChild(feed.firstChild);
  }
  if (stick) {
    feed.scrollTop = feed.scrollHeight;
  } else {
    feedUnread += items.length;
    updateFeedJump();
  }
}

// ---------------------------------------------------------------------------
// State message handler
// ---------------------------------------------------------------------------

function setProc(active: boolean, latencyMs?: number | null): void {
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
  setProc(!!msg.processing, msg.latency_ms);

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
        setProc(msg.active, msg.latency_ms);
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

/**
 * Render the (up to two) suggested probes as clickable chips in #suggestions.
 * Each chip shows the probe label plus a short hint of its kind/question/
 * instruction. Clicking a chip upserts that probe verbatim (it already carries
 * an id), updates local probeState, and clears the container. A tiny "Dismiss"
 * affordance clears it too. Guards against #suggestions being absent.
 */
function renderSuggestions(probes: ProbeDescriptor[]): void {
  setSuggestPending(false);
  const container = document.getElementById("suggestions");
  if (!container) return;
  container.innerHTML = "";

  const shown = probes.slice(0, 2);
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
      container.innerHTML = "";
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
