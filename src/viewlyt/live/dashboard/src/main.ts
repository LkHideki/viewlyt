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
  model: { base_url: string; model: string };
  paused: boolean;
  ingested: number;
  latency_ms?: number | null;
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
}

type InboundMsg = StateMsg | ResultMsg | StatMsg | ErrorMsg | ChatFeedMsg | ProcMsg;

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
// Probe list rendering
// ---------------------------------------------------------------------------

const probeState = new Map<string, ProbeDescriptor>();

function renderProbes(): void {
  const container = el<HTMLDivElement>("probes");
  if (probeState.size === 0) {
    container.innerHTML = '<p class="empty-hint">No probes configured yet.</p>';
    return;
  }
  container.innerHTML = "";
  for (const probe of probeState.values()) {
    const row = document.createElement("div");
    row.className = "probe-row";
    row.dataset["id"] = probe.id;

    const info = document.createElement("div");
    info.className = "probe-info";

    const badge = document.createElement("span");
    badge.className = "probe-kind-badge";
    badge.textContent = probe.kind;

    const name = document.createElement("span");
    name.className = "probe-name";
    name.textContent = probe.label;

    const detail = document.createElement("span");
    detail.className = "probe-detail";
    if (probe.kind === "classification" && probe.categories) {
      detail.textContent = probe.categories.join(", ");
    } else if (probe.kind === "open" && probe.instruction) {
      detail.textContent = probe.instruction;
    }

    info.appendChild(badge);
    info.appendChild(name);
    info.appendChild(detail);

    const removeBtn = document.createElement("button");
    removeBtn.className = "btn-danger btn-small";
    removeBtn.textContent = "Remove";
    removeBtn.addEventListener("click", () => {
      send({ op: "remove_probe", id: probe.id });
      probeState.delete(probe.id);
      renderProbes();
    });

    row.appendChild(info);
    row.appendChild(removeBtn);
    container.appendChild(row);
  }
}

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
  | "delta";
const CHART_TYPES: ChartType[] = [
  "bars",
  "columns",
  "stacked",
  "donut",
  "lines",
  "area",
  "delta",
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
function renderBars(views: CatView[]): HTMLElement {
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
    fill.style.backgroundColor = getCategoryColor(v.i);

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
function buildStats(views: CatView[]): HTMLElement {
  const stats = document.createElement("div");
  stats.className = "cat-stats";

  for (const v of views) {
    const row = document.createElement("div");
    row.className = "cat-stat-row";

    const swatch = document.createElement("span");
    swatch.className = "cat-swatch";
    swatch.style.backgroundColor = getCategoryColor(v.i);

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
function renderColumns(views: CatView[]): Element {
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
        fill: getCategoryColor(v.i),
        rx: 2,
      }),
    );
  });

  return root;
}

/** Single full-width bar split into colored segments. */
function renderStacked(entries: [string, number][]): HTMLElement {
  const bar = document.createElement("div");
  bar.className = "stacked-bar";
  entries.forEach(([cat, pct], i) => {
    const seg = document.createElement("div");
    seg.className = "stacked-seg";
    seg.style.flexBasis = `${clampPct(pct)}%`;
    seg.style.backgroundColor = getCategoryColor(i);
    seg.title = `${cat}: ${pct.toFixed(1)}%`;
    bar.appendChild(seg);
  });
  return bar;
}

/** Inline SVG donut — one arc per category, sized to its pct. */
function renderDonut(entries: [string, number][]): Element {
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
    entries.forEach(([, pct], i) => {
      const frac = Math.max(0, pct) / total;
      const dash = frac * circumference;
      const seg = svg("circle", {
        cx,
        cy,
        r: radius,
        fill: "none",
        stroke: getCategoryColor(i),
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
function renderLines(views: CatView[]): Element {
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
      svg("polyline", { class: "chart-line", stroke: getCategoryColor(v.i), points: pts }),
    );
  }

  return root;
}

/**
 * 100%-stacked area over the snapshot history: shows how the mix evolves. Each
 * category is a filled band between its lower and upper cumulative boundary at
 * every time point. A single time point degrades to one vertical 100% stack.
 */
function renderArea(views: CatView[]): Element {
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
          fill: getCategoryColor(views[i].i),
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
        fill: getCategoryColor(views[i].i),
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

  const entries = Object.entries(snap.pct);
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
      viz.appendChild(renderColumns(views));
      break;
    case "stacked":
      viz.appendChild(renderStacked(entries));
      break;
    case "donut":
      viz.appendChild(renderDonut(entries));
      break;
    case "lines":
      viz.appendChild(renderLines(views));
      break;
    case "area":
      viz.appendChild(renderArea(views));
      break;
    case "delta":
      viz.appendChild(renderDelta(views));
      break;
    default:
      viz.appendChild(renderBars(views));
      break;
  }
  body.appendChild(viz);

  if (type !== "bars") {
    body.appendChild(buildStats(views));
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

function upsertResultCard(msg: ResultMsg): void {
  const container = el<HTMLDivElement>("results");

  // Remove empty-hint on first result
  const hint = container.querySelector(".empty-hint");
  if (hint) hint.remove();

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

  let card = container.querySelector<HTMLDivElement>(
    `[data-probe-id="${CSS.escape(msg.probe_id)}"]`
  );

  if (!card) {
    card = document.createElement("div");
    const cardEl = card; // stable non-null ref for closures below
    // Flat card (R4): just border + radius, no left accent bar.
    card.className = "result-card";
    card.dataset["probeId"] = msg.probe_id;

    // --- header ---
    const header = document.createElement("div");
    header.className = "result-header";

    const labelEl = document.createElement("span");
    labelEl.className = "result-label";
    labelEl.textContent = msg.label;
    header.appendChild(labelEl);

    const metaEl = document.createElement("span");
    metaEl.className = "result-meta";

    if (msg.kind === "classification") {
      // Classification header carries a chart-type selector beside the meta.
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
      // Chart type is server-persisted on the probe descriptor.
      select.value = probeChart(msg.probe_id);
      select.addEventListener("change", () => {
        const chosen = select.value as ChartType;
        const pid = cardEl.dataset["probeId"] ?? "";
        // Persist server-side by re-upserting the existing probe descriptor.
        const existing = probeState.get(pid);
        const probe: ProbeDescriptor = existing
          ? { ...existing, chart: chosen }
          : { id: pid, kind: "classification", label: "", chart: chosen };
        send({ op: "upsert_probe", probe });
        probeState.set(probe.id, probe);
        // Re-render the display at the CURRENT view index.
        const vs = viewState.get(pid);
        renderDisplay(cardEl, pid, "classification", vs ? vs.index : 0);
      });

      right.appendChild(select);
      right.appendChild(metaEl);
      header.appendChild(right);
    } else {
      header.appendChild(metaEl);
    }
    card.appendChild(header);

    // --- scrubber ---
    const scrubber = document.createElement("div");
    scrubber.className = "result-scrubber";

    const range = document.createElement("input");
    range.className = "scrub-range";
    range.type = "range";
    range.min = "0";
    range.max = String(Math.max(0, history.length - 1));
    range.value = String(view.index);
    range.step = "1";

    const scrubMeta = document.createElement("div");
    scrubMeta.className = "scrub-meta";
    // Leading text node (filled by refreshScrubber) + the LIVE/behind badge.
    scrubMeta.appendChild(document.createTextNode(""));
    const liveBadge = document.createElement("span");
    liveBadge.className = "scrub-live";
    liveBadge.textContent = "LIVE";
    scrubMeta.appendChild(liveBadge);

    scrubber.appendChild(range);
    scrubber.appendChild(scrubMeta);
    card.appendChild(scrubber);

    // Wire the scrubber listener ONCE; read the live max from history each time.
    const kind = msg.kind;
    const pid = msg.probe_id;
    range.addEventListener("input", () => {
      const hist = resultHistory.get(pid) ?? [];
      const max = Math.max(0, hist.length - 1);
      const idx = Math.min(max, Math.max(0, Number(range.value)));
      const vs = viewState.get(pid);
      if (vs) {
        vs.index = idx;
        vs.live = idx === max;
      }
      renderDisplay(cardEl, pid, kind, idx);
      refreshScrubber(cardEl, pid, idx);
    });

    // --- display ---
    card.appendChild(
      msg.kind === "classification"
        ? buildClassificationBody(msg.probe_id, view.index)
        : buildOpenBody(msg.probe_id, view.index),
    );

    container.prepend(card);
  }

  // If pinned to latest, jump the view to the newest snapshot.
  if (view.live) view.index = history.length - 1;

  // Keep the selector in sync with the server-persisted chart type.
  if (msg.kind === "classification") {
    const selectEl = card.querySelector<HTMLSelectElement>(".chart-select");
    if (selectEl) selectEl.value = probeChart(msg.probe_id);
  }

  // Render the display at the current view index and refresh chrome.
  renderDisplay(card, msg.probe_id, msg.kind, view.index);
  refreshScrubber(card, msg.probe_id, view.index);
}

// ---------------------------------------------------------------------------
// Error banner
// ---------------------------------------------------------------------------

function showError(message: string): void {
  const container = el<HTMLDivElement>("results");
  const hint = container.querySelector(".empty-hint");
  if (hint) hint.remove();
  let card = container.querySelector<HTMLDivElement>('[data-error="1"]');
  if (!card) {
    card = document.createElement("div");
    // Bordered danger-tinted box (R4): no inline border-left, no left bar.
    card.className = "result-card result-error";
    card.dataset["error"] = "1";
    container.prepend(card);
  }
  card.textContent = "⚠ " + message;
}

// ---------------------------------------------------------------------------
// Live chat feed
// ---------------------------------------------------------------------------

function appendFeed(items: { author: string; text: string }[]): void {
  const feed = document.getElementById("feed");
  if (!feed) return;
  const frag = document.createDocumentFragment();
  for (const { author, text } of items) {
    const line = document.createElement("div");
    const a = document.createElement("span");
    a.textContent = author + ": ";
    a.style.fontWeight = "600";
    a.style.color = "#4c8bf5";
    line.appendChild(a);
    line.appendChild(document.createTextNode(text));
    frag.appendChild(line);
  }
  feed.appendChild(frag);
  while (feed.childElementCount > 400 && feed.firstChild) {
    feed.removeChild(feed.firstChild);
  }
  feed.scrollTop = feed.scrollHeight;
}

// ---------------------------------------------------------------------------
// State message handler
// ---------------------------------------------------------------------------

function setProc(active: boolean, latencyMs?: number | null): void {
  const e = el<HTMLSpanElement>("proc");
  if (active) {
    e.textContent = "analyzing…";
    e.style.color = "#fbbf24";
  } else {
    e.textContent = latencyMs != null ? `${latencyMs} ms` : "idle";
    e.style.color = "";
  }
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

  // Reverse-map base_url -> provider dropdown
  const providerSel = el<HTMLSelectElement>("provider");
  const matchedKey = Object.keys(PROVIDERS).find(
    (k) => PROVIDERS[k].base_url === msg.model.base_url
  );
  if (matchedKey !== undefined) {
    providerSel.value = matchedKey;
  }

  // Stats
  el<HTMLSpanElement>("ingested").textContent = String(msg.ingested);
  setProc(false, msg.latency_ms);

  // Probes
  probeState.clear();
  for (const p of msg.probes) {
    probeState.set(p.id, p);
  }
  renderProbes();

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

type AskKind = "open" | "classify";
let askKind: AskKind = "open";

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
  });

  el("pause").addEventListener("click", () => send({ op: "pause" }));
  el("resume").addEventListener("click", () => send({ op: "resume" }));
  el("clear").addEventListener("click", () => send({ op: "clear" }));

  el("apply-model").addEventListener("click", () => {
    const apiKey = inputVal("api_key");
    const op: Record<string, string> = {
      op: "set_model",
      base_url: inputVal("base_url"),
      model: inputVal("model"),
    };
    if (apiKey !== "") {
      op["api_key"] = apiKey;
    }
    send(op);
    // Clear the api_key input after sending
    setInputVal("api_key", "");
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

    // Optimistically add to local state so UI updates immediately
    probeState.set(id, probe as unknown as ProbeDescriptor);
    renderProbes();

    // Reset form
    setInputVal("probe-label", "");
    setInputVal("probe-question", "");
    setInputVal("probe-categories", "");
    setInputVal("probe-instruction", "");
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

    renderProbes();
    el<HTMLTextAreaElement>("probe-json").value = "";
    statusEl.textContent = added > 0 ? `Imported ${added} probe(s).` : "No probes found.";
  });

  // ---- Ask-bar ----

  function setAskKind(kind: AskKind): void {
    askKind = kind;
    el<HTMLButtonElement>("ask-open").classList.toggle("ask-chip-active", kind === "open");
    el<HTMLButtonElement>("ask-classify").classList.toggle("ask-chip-active", kind === "classify");
    const catsEl = el<HTMLInputElement>("ask-categories");
    if (kind === "classify") {
      catsEl.classList.remove("hidden");
    } else {
      catsEl.classList.add("hidden");
    }
  }

  el("ask-open").addEventListener("click", () => setAskKind("open"));
  el("ask-classify").addEventListener("click", () => setAskKind("classify"));

  function submitAskBar(): void {
    if (askPending) return;
    const text = el<HTMLInputElement>("ask-text").value.trim();
    if (!text) return;

    // The "smart" path: the server rewrites this prompt into a probe spec, then
    // builds + stores the probe (assigning its id) and broadcasts a state frame.
    const cats = el<HTMLInputElement>("ask-categories").value
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s.length > 0);

    const op: Record<string, unknown> = {
      op: "rewrite_probe",
      kind: askKind === "classify" ? "classification" : "open",
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
    navigator.clipboard.writeText(text).catch(() => {
      // Fallback: select the textarea so the user can copy manually
      el<HTMLTextAreaElement>("snippet").select();
    });
  });

  el("copy-bookmarklet").addEventListener("click", () => {
    const bm = el<HTMLTextAreaElement>("bookmarklet");
    navigator.clipboard.writeText(bm.value).catch(() => bm.select());
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
wireGroups();
connectDashboard();
connectControl();
loadSnippet();
