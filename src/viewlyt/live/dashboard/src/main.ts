// viewlyt.live — Control Panel main script
// Strict TypeScript. Runtime deps: marked + DOMPurify (bundled, no CDN).

import { marked } from "marked";
import DOMPurify from "dompurify";

// Phosphor (MIT) regular-weight icons, imported raw so only these ship.
import iconTranslate from "@phosphor-icons/core/assets/regular/translate.svg?raw";
import iconGear from "@phosphor-icons/core/assets/regular/gear.svg?raw";
import iconSun from "@phosphor-icons/core/assets/regular/sun.svg?raw";
import iconMoon from "@phosphor-icons/core/assets/regular/moon.svg?raw";
import iconDesktop from "@phosphor-icons/core/assets/regular/desktop.svg?raw";
import iconPause from "@phosphor-icons/core/assets/regular/pause.svg?raw";
import iconSparkle from "@phosphor-icons/core/assets/regular/sparkle.svg?raw";
import iconGitFork from "@phosphor-icons/core/assets/regular/git-fork.svg?raw";
import iconPlus from "@phosphor-icons/core/assets/regular/plus.svg?raw";
import iconCaretDown from "@phosphor-icons/core/assets/regular/caret-down.svg?raw";
import iconLightning from "@phosphor-icons/core/assets/regular/lightning.svg?raw";
import iconStop from "@phosphor-icons/core/assets/regular/stop.svg?raw";
import iconDownload from "@phosphor-icons/core/assets/regular/download-simple.svg?raw";
import iconX from "@phosphor-icons/core/assets/regular/x.svg?raw";
import iconCopy from "@phosphor-icons/core/assets/regular/copy.svg?raw";
import iconChat from "@phosphor-icons/core/assets/regular/chat-circle-text.svg?raw";
import iconLightbulb from "@phosphor-icons/core/assets/regular/lightbulb.svg?raw";
import iconCheckCircle from "@phosphor-icons/core/assets/regular/check-circle.svg?raw";
import iconWarningCircle from "@phosphor-icons/core/assets/regular/warning-circle.svg?raw";
import iconInfo from "@phosphor-icons/core/assets/regular/info.svg?raw";
import iconDotsThree from "@phosphor-icons/core/assets/regular/dots-three.svg?raw";
import iconGrip from "@phosphor-icons/core/assets/regular/dots-six-vertical.svg?raw";
import iconPencil from "@phosphor-icons/core/assets/regular/pencil-simple.svg?raw";
import iconTrash from "@phosphor-icons/core/assets/regular/trash.svg?raw";
import iconArrowUp from "@phosphor-icons/core/assets/regular/arrow-up.svg?raw";
import iconArrowDown from "@phosphor-icons/core/assets/regular/arrow-down.svg?raw";
import iconChartBar from "@phosphor-icons/core/assets/regular/chart-bar.svg?raw";
import iconChartBarH from "@phosphor-icons/core/assets/regular/chart-bar-horizontal.svg?raw";
import iconChatText from "@phosphor-icons/core/assets/regular/chat-text.svg?raw";
import iconOutH from "@phosphor-icons/core/assets/regular/arrows-out-line-horizontal.svg?raw";
import iconInH from "@phosphor-icons/core/assets/regular/arrows-in-line-horizontal.svg?raw";
import iconOutV from "@phosphor-icons/core/assets/regular/arrows-out-line-vertical.svg?raw";
import iconInV from "@phosphor-icons/core/assets/regular/arrows-in-line-vertical.svg?raw";

const ICONS: Record<string, string> = {
  translate: iconTranslate,
  gear: iconGear,
  sun: iconSun,
  moon: iconMoon,
  desktop: iconDesktop,
  pause: iconPause,
  sparkle: iconSparkle,
  "git-fork": iconGitFork,
  plus: iconPlus,
  "caret-down": iconCaretDown,
  lightning: iconLightning,
  stop: iconStop,
  "download-simple": iconDownload,
  x: iconX,
  copy: iconCopy,
  "chat-circle-text": iconChat,
  lightbulb: iconLightbulb,
  "check-circle": iconCheckCircle,
  "warning-circle": iconWarningCircle,
  info: iconInfo,
  "dots-three": iconDotsThree,
  "dots-six-vertical": iconGrip,
  "pencil-simple": iconPencil,
  trash: iconTrash,
  "arrow-up": iconArrowUp,
  "arrow-down": iconArrowDown,
  "chart-bar": iconChartBar,
  "chart-bar-horizontal": iconChartBarH,
  "chat-text": iconChatText,
  "arrows-out-line-horizontal": iconOutH,
  "arrows-in-line-horizontal": iconInH,
  "arrows-out-line-vertical": iconOutV,
  "arrows-in-line-vertical": iconInV,
};

/** Inline SVG markup for an icon (decorative: hidden from assistive tech). */
function iconSvg(name: string): string {
  const svg = ICONS[name] ?? "";
  return svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ');
}

/** Fill every `[data-icon]` placeholder under `root` with its SVG. */
function injectIcons(root: ParentNode = document): void {
  root.querySelectorAll<HTMLElement>("[data-icon]").forEach((node) => {
    const name = node.dataset["icon"];
    if (name && !node.firstElementChild) node.innerHTML = iconSvg(name);
  });
}

/** Set a button's visible label without touching its icon (.btn-label child). */
function setBtnLabel(btn: HTMLElement, text: string): void {
  (btn.querySelector<HTMLElement>(".btn-label") ?? btn).textContent = text;
}

function getBtnLabel(btn: HTMLElement): string {
  return (btn.querySelector<HTMLElement>(".btn-label") ?? btn).textContent ?? "";
}

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
  // Chat is OpenAI-compatible; the same host serves the decisions API (decider).
  perplexity: {
    base_url: "https://api.perplexity.ai",
    model: "sonar",
    keyHint: "pplx-...",
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

type DeciderState = "off" | "inactive" | "ok" | "degraded" | "open";

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
  model: {
    base_url: string;
    model: string;
    decider_model?: string;
    /** Decider configured AND the provider has a decisions endpoint (older servers omit it). */
    decider_active?: boolean;
    budget?: number;
    language?: string;
  };
  /** Health of the decision model (older servers omit it). */
  decider_state?: DeciderState;
  paused: boolean;
  processing?: boolean;
  budget_blocked?: boolean;
  ingested: number;
  latency_ms?: number | null;
  avg_latency_ms?: number | null;
  analyzing_probe_ids?: string[];
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
  probe_ids?: string[];
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
  // CSS hook: the Live dot only pulses while running AND connected.
  document.body.dataset["connected"] = connected ? "1" : "0";
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
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS: MD_ALLOWED_TAGS,
    ALLOWED_ATTR: ["href", "title", "start", "target", "rel"],
  });
  // Keep the page outline logical: model output must never add an h1/h2 next to the
  // page's own headings (card titles are h3), so its headings sit below them (h4-h6).
  return clean.replace(/<(\/?)h([1-6])(?=[\s>])/g, (_m, slash: string, n: string) => `<${slash}h${Math.min(6, Number(n) + 3)}`);
}

// ---------------------------------------------------------------------------
// Result cards
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Theme + data palette. Every colour lives in CSS custom properties (style.css
// tokens, per theme). TS reads them back through getComputedStyle as #rrggbb
// (the per-category edit <input type="color"> only accepts hex) and re-reads
// them whenever the theme changes. Category colour follows the category's
// INDEX in the probe's own list, fixed slot order, never cycled: slots 1-8 are
// the validated palette, 9+ use the neutral "other" gray.
// ---------------------------------------------------------------------------

const SERIES_SLOTS = 8;
const SEQ_STOPS = 9;
let seriesColors: string[] = [];
let seriesOther = "#808080";
let seqColors: string[] = [];
let chartAxisColor = "#808080";

const HEX6 = /^#[0-9a-fA-F]{6}$/;

function readToken(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return HEX6.test(v) ? v.toLowerCase() : fallback;
}

function readThemeColors(): void {
  seriesColors = [];
  for (let i = 1; i <= SERIES_SLOTS; i++) {
    seriesColors.push(readToken(`--series-${i}`, "#3987e5"));
  }
  seriesOther = readToken("--series-other", "#808080");
  seqColors = [];
  for (let i = 0; i < SEQ_STOPS; i++) seqColors.push(readToken(`--seq-${i}`, "#3987e5"));
  chartAxisColor = readToken("--chart-axis", "#808080");
}

function hexToRgb(hex: string): [number, number, number] {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ];
}

function relLuminance(hex: string): number {
  const lin = hexToRgb(hex).map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  });
  return 0.2126 * lin[0]! + 0.7152 * lin[1]! + 0.0722 * lin[2]!;
}

/** Sequential-ramp colour for t in [0, 1] (linear between the --seq-N stops). */
function seqColor(t: number): string {
  const stops = seqColors.length > 1 ? seqColors : ["#1f2a3a", "#6da7ec"];
  const x = Math.min(1, Math.max(0, t)) * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(x));
  const f = x - i;
  const a = hexToRgb(stops[i]!);
  const b = hexToRgb(stops[i + 1]!);
  const mix = (k: number): string =>
    Math.round(a[k]! + (b[k]! - a[k]!) * f)
      .toString(16)
      .padStart(2, "0");
  return `#${mix(0)}${mix(1)}${mix(2)}`;
}

/** Legible ink for text drawn on `bg`: whichever of near-black / near-white contrasts more. */
function inkOn(bg: string): string {
  const L = relLuminance(bg);
  const dark = (L + 0.05) / (relLuminance("#0b0b0b") + 0.05);
  const light = (relLuminance("#fdfdfc") + 0.05) / (L + 0.05);
  return dark >= light ? "#0b0b0b" : "#fdfdfc";
}

function getCategoryColor(index: number): string {
  if (seriesColors.length === 0) readThemeColors();
  return index < SERIES_SLOTS ? seriesColors[index]! : seriesOther;
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

// ---------------------------------------------------------------------------
// Chart toolkit shared by every renderer: tiny DOM helpers, tooltip data,
// the plot frame (gridlines + y ticks), sparkline, delta badge, accessibility
// summaries. Colours are never hard-coded here: category marks come from
// colorFor (palette slots / probe overrides), chrome comes from CSS tokens.
// ---------------------------------------------------------------------------

/** What every renderer needs to know about the snapshot it draws. */
interface ChartCtx {
  probeId: string;
  /** Messages in the snapshot being viewed (drives the "about N msgs" tooltip). */
  n: number;
  /** Pixel width the chart may use (the card's content box). */
  width: number;
}

/** Friendly chart names (option.value stays the ChartType id). */
const CHART_LABELS: Record<ChartType, string> = {
  bars: "Bars",
  columns: "Columns",
  stacked: "Stacked bar",
  donut: "Donut",
  lines: "Lines",
  area: "Area",
  delta: "Change",
  gauge: "Gauge",
  heatmap: "Heatmap",
  podium: "Podium",
  violin: "Violin",
  descartes: "Grid (2D)",
};

function mk<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  className = "",
  text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** A 16px (via CSS) icon holder with a Phosphor SVG inside. */
function iconNode(name: string, extraClass = ""): HTMLSpanElement {
  const node = mk("span", extraClass ? `icon ${extraClass}` : "icon");
  node.innerHTML = iconSvg(name);
  return node;
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

/** Root <svg> for a chart: real pixel viewBox, decorative to assistive tech. */
function svgRoot(cls: string, width: number, height: number): SVGSVGElement {
  return svg("svg", {
    class: `chart-svg ${cls}`,
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    "aria-hidden": "true",
    focusable: "false",
  });
}

function svgText(
  x: number,
  y: number,
  text: string,
  cls: string,
  anchor: "start" | "middle" | "end" = "middle",
): SVGTextElement {
  const node = svg("text", { class: cls, x: x.toFixed(1), y: y.toFixed(1), "text-anchor": anchor });
  node.textContent = text;
  return node;
}

/** Shorten `text` to roughly `maxChars` characters with an ellipsis. */
function clip(text: string, maxChars: number): string {
  const max = Math.max(1, Math.floor(maxChars));
  return text.length <= max ? text : `${text.slice(0, Math.max(1, max - 1))}…`;
}

/** Clamp a percentage into the 0-100 range. */
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

function contrastRatio(a: string, b: string): number {
  const la = relLuminance(a);
  const lb = relLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}

/** True when text on `bg` cannot reach 7:1 with either near-black or near-white. */
function needsChip(bg: string): boolean {
  return contrastRatio(inkOn(bg), bg) < 7;
}

// ----- tooltip data ---------------------------------------------------------

/**
 * Mark a chart element as hoverable/inspectable. The tooltip (see
 * wireChartTips) reads these data attributes, so the marks stay plain nodes
 * and survive in-place updates.
 */
function setTip(
  node: Element,
  ctx: ChartCtx,
  v: { cat: string; pct: number; i?: number },
  opts: { note?: string; color?: string; nonav?: boolean } = {},
): void {
  node.setAttribute("data-tip-name", v.cat);
  node.setAttribute("data-tip-color", opts.color ?? colorFor(ctx.probeId, v.cat, v.i ?? 0));
  node.setAttribute("data-tip-pct", `${v.pct.toFixed(1)}%`);
  if (ctx.n > 0) {
    node.setAttribute(
      "data-tip-count",
      `about ${Math.round((v.pct / 100) * ctx.n)} of ${ctx.n} msgs`,
    );
  }
  if (opts.note) node.setAttribute("data-tip-note", opts.note);
  if (opts.nonav) node.setAttribute("data-tip-nonav", "");
}

// ----- delta badge + sparkline ----------------------------------------------

function fmtDelta(delta: number | null): string {
  if (delta === null || Math.abs(delta) < 0.05) return "0.0";
  return (delta > 0 ? "+" : "") + delta.toFixed(1);
}

/** Delta vs previous snapshot: arrow icon + signed number, neutral ink (a category rising is not "good"). */
function buildDeltaBadge(delta: number | null): HTMLElement {
  const flat = delta === null || Math.abs(delta) < 0.05;
  const badge = mk("span", `delta-badge ${flat ? "delta-zero" : (delta as number) > 0 ? "delta-up" : "delta-down"}`);
  if (!flat) badge.appendChild(iconNode((delta as number) > 0 ? "arrow-up" : "arrow-down", "delta-icon"));
  if (delta !== null) badge.appendChild(document.createTextNode(fmtDelta(delta)));
  badge.title = "Change vs the previous snapshot, in percentage points";
  return badge;
}

// The sparkline is a compact accent (48x18), not the chart: cap its points so
// a long session (up to 60 snapshots) keeps its shape readable.
const SPARK_POINTS = 16;

function sparkline(values: number[], color: string): SVGSVGElement {
  const w = 48;
  const h = 18;
  const pad = 2;
  const root = svg("svg", {
    class: "spark-svg",
    viewBox: `0 0 ${w} ${h}`,
    width: w,
    height: h,
    "aria-hidden": "true",
    focusable: "false",
  });
  const pts = values.slice(-SPARK_POINTS);
  if (pts.length === 0) return root;
  let lo = Math.min(...pts);
  let hi = Math.max(...pts);
  if (hi - lo < 10) {
    const mid = (hi + lo) / 2;
    lo = mid - 5;
    hi = mid + 5;
  }
  const xFor = (i: number): number =>
    pts.length === 1 ? w - pad - 1.5 : pad + (i / (pts.length - 1)) * (w - 2 * pad);
  const yFor = (v: number): number => pad + (1 - (v - lo) / (hi - lo)) * (h - 2 * pad);
  if (pts.length === 1) {
    root.appendChild(svg("circle", { cx: xFor(0).toFixed(1), cy: yFor(pts[0]!).toFixed(1), r: 1.5, fill: color }));
  } else {
    root.appendChild(
      svg("polyline", {
        class: "spark-line",
        stroke: color,
        points: pts.map((p, i) => `${xFor(i).toFixed(1)},${yFor(p).toFixed(1)}`).join(" "),
      }),
    );
  }
  return root;
}

function sparkCell(cls: string, v: CatView, color: string): HTMLElement {
  const cell = mk("span", cls);
  cell.appendChild(sparkline(v.series, color));
  return cell;
}

// ----- accessibility twins --------------------------------------------------

/** One-sentence summary of a chart (the role=img label). */
function chartSummary(type: ChartType, label: string, views: CatView[]): string {
  const name = label || "Probe";
  if (views.length === 0) return `${name}: ${CHART_LABELS[type]} chart, no data yet.`;
  const byChange = type === "delta";
  const ranked = [...views].sort((a, b) =>
    byChange ? Math.abs(b.delta ?? 0) - Math.abs(a.delta ?? 0) : b.pct - a.pct,
  );
  const part = (v: CatView): string =>
    byChange ? `${v.cat} ${fmtDelta(v.delta)} points` : `${v.cat} ${v.pct.toFixed(1)}%`;
  const top = ranked.slice(0, 3).map(part).join(", ");
  const more = ranked.length > 3 ? `, and ${ranked.length - 3} more` : "";
  const lead = byChange ? "Change since the previous snapshot. Largest" : "Top";
  return `${name}: ${CHART_LABELS[type]} chart of ${views.length} categories. ${lead}: ${top}${more}.`;
}

/** Visually hidden data table: the screen-reader twin of the drawn chart. */
function buildDataTable(views: CatView[], caption: string): HTMLElement {
  // A table ignores width:1px (it never shrinks below its content), so the
  // visually-hidden clip lives on a wrapper div.
  const wrap = mk("div", "sr-only chart-table");
  const table = mk("table");
  table.appendChild(mk("caption", "", caption));
  const head = mk("tr");
  for (const t of ["Category", "Share", "Change in points"]) head.appendChild(mk("th", "", t));
  table.appendChild(mk("thead")).appendChild(head);
  const body = mk("tbody");
  for (const v of views) {
    const row = mk("tr");
    row.appendChild(mk("th", "", v.cat));
    row.appendChild(mk("td", "", `${v.pct.toFixed(1)}%`));
    row.appendChild(mk("td", "", v.delta === null ? "none" : fmtDelta(v.delta)));
    body.appendChild(row);
  }
  table.appendChild(body);
  wrap.appendChild(table);
  return wrap;
}

// ----- plot frame (gridlines at 0/50/100 + y tick labels) -------------------

interface Frame {
  x0: number;
  x1: number;
  yTop: number;
  yBase: number;
  yFor: (pct: number) => number;
}

function chartFrame(
  root: SVGSVGElement,
  width: number,
  height: number,
  pad: { left: number; right: number; top: number; bottom: number },
): Frame {
  const x0 = pad.left;
  const x1 = width - pad.right;
  const yTop = pad.top;
  const yBase = height - pad.bottom;
  const yFor = (pct: number): number => yBase - (clampPct(pct) / 100) * (yBase - yTop);
  for (const p of [100, 50, 0]) {
    const y = yFor(p) + 0.5;
    root.appendChild(
      svg("line", {
        class: p === 0 ? "chart-baseline" : "chart-grid-line",
        x1: x0,
        y1: y.toFixed(1),
        x2: x1,
        y2: y.toFixed(1),
      }),
    );
    const t = svgText(x0 - 8, y, `${p}%`, "chart-tick", "end");
    t.setAttribute("dy", "0.35em");
    root.appendChild(t);
  }
  return { x0, x1, yTop, yBase, yFor };
}

/** The last point of a series: an 8px dot ringed by 2px of surface. */
function lastDot(root: SVGElement, x: number, y: number, color: string): void {
  root.appendChild(svg("circle", { class: "chart-dot-ring", cx: x.toFixed(1), cy: y.toFixed(1), r: 6 }));
  root.appendChild(svg("circle", { cx: x.toFixed(1), cy: y.toFixed(1), r: 4, fill: color }));
}

/** Rect with only the top corners rounded (marks anchored at a baseline). */
function topRoundPath(x: number, y: number, w: number, hgt: number, r: number): string {
  const rr = Math.max(0, Math.min(r, w / 2, hgt));
  const f = (n: number): string => n.toFixed(1);
  return (
    `M${f(x)} ${f(y + hgt)}V${f(y + rr)}Q${f(x)} ${f(y)} ${f(x + rr)} ${f(y)}` +
    `H${f(x + w - rr)}Q${f(x + w)} ${f(y)} ${f(x + w)} ${f(y + rr)}V${f(y + hgt)}Z`
  );
}

/** Transparent, full-height hit area for one slot of an SVG chart. */
function hitRect(x: number, y: number, w: number, hgt: number): SVGRectElement {
  return svg("rect", {
    class: "chart-hit",
    x: x.toFixed(1),
    y: y.toFixed(1),
    width: Math.max(1, w).toFixed(1),
    height: Math.max(1, hgt).toFixed(1),
    fill: "transparent",
  });
}

/** Label drawn on a cell of colour `bg`: plain ink when it reaches 7:1, else on a surface chip. */
function cellLabel(root: SVGElement, cx: number, cy: number, text: string, bg: string): void {
  if (needsChip(bg)) {
    const w = text.length * 6.8 + 8;
    root.appendChild(
      svg("rect", {
        class: "chart-chip",
        x: (cx - w / 2).toFixed(1),
        y: (cy - 8).toFixed(1),
        width: w.toFixed(1),
        height: 16,
        rx: 4,
      }),
    );
    const t = svgText(cx, cy, text, "chart-chip-text");
    t.setAttribute("dy", "0.35em");
    root.appendChild(t);
  } else {
    const t = svgText(cx, cy, text, "chart-cell-text");
    t.setAttribute("dy", "0.35em");
    t.setAttribute("fill", inkOn(bg));
    root.appendChild(t);
  }
}

// ---------------------------------------------------------------------------
// Display renderers (one snapshot at a time; the scrubber picks the snapshot)
// ---------------------------------------------------------------------------

/** Default: thin horizontal % bars + delta vs previous + per-category sparkline. */
function renderBars(ctx: ChartCtx, views: CatView[]): HTMLElement {
  const barsEl = mk("div", "bars");
  for (const v of views) {
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const row = mk("div", "bar-row");

    const catLabel = mk("span", "bar-label", v.cat);
    catLabel.title = v.cat;

    // The track is the hit area (20px tall); the fill is a thin 10px mark.
    const track = mk("div", "bar-track");
    setTip(track, ctx, v, { note: v.delta === null ? "" : `${fmtDelta(v.delta)} pts vs previous` });
    const fill = mk("div", "bar-fill");
    fill.style.width = `${clampPct(v.pct)}%`;
    fill.style.backgroundColor = color;
    track.appendChild(fill);

    row.appendChild(catLabel);
    row.appendChild(track);
    row.appendChild(mk("span", "bar-pct", `${v.pct.toFixed(1)}%`));
    row.appendChild(buildDeltaBadge(v.delta));
    row.appendChild(sparkCell("spark", v, color));
    barsEl.appendChild(row);
  }
  return barsEl;
}

/**
 * The legend, reused under EVERY non-bars chart: one row per category with a
 * swatch, name, pct, delta and sparkline (the 5-child grid contract). The delta
 * chart passes withDelta=false: its rows already carry the change, so the legend
 * drops that column (a 4-child "lean" variant) instead of repeating it.
 */
function buildStats(ctx: ChartCtx, views: CatView[], withDelta = true): HTMLElement {
  const stats = mk("div", withDelta ? "cat-stats" : "cat-stats cat-stats-lean");
  for (const v of views) {
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const row = mk("div", "cat-stat-row");
    const swatch = mk("span", "cat-swatch");
    swatch.style.backgroundColor = color;
    const name = mk("span", "cat-name", v.cat);
    name.title = v.cat;
    row.appendChild(swatch);
    row.appendChild(name);
    row.appendChild(mk("span", "cat-pct", `${v.pct.toFixed(1)}%`));
    if (withDelta) row.appendChild(buildDeltaBadge(v.delta));
    row.appendChild(sparkCell("cat-spark", v, color));
    stats.appendChild(row);
  }
  return stats;
}

/** Vertical columns (SVG): one rounded-top bar per category on a 0/50/100 grid. */
function renderColumns(ctx: ChartCtx, views: CatView[]): Element {
  const W = ctx.width;
  const H = 156;
  const root = svgRoot("chart-columns", W, H);
  const f = chartFrame(root, W, H, { left: 40, right: 4, top: 8, bottom: 22 });
  const n = Math.max(1, views.length);
  const slot = (f.x1 - f.x0) / n;
  const barW = Math.max(6, Math.min(44, slot - 6));

  views.forEach((v, idx) => {
    const cx = f.x0 + slot * idx + slot / 2;
    const top = f.yFor(v.pct);
    const hgt = f.yBase - top;
    if (hgt > 0.4) {
      root.appendChild(
        svg("path", {
          d: topRoundPath(cx - barW / 2, top, barW, Math.max(hgt, 1.5), 4),
          fill: colorFor(ctx.probeId, v.cat, v.i),
        }),
      );
    }
    if (slot >= 40) {
      root.appendChild(svgText(cx, f.yBase + 16, clip(v.cat, (slot - 4) / 6.4), "chart-xlabel"));
    }
    const hit = hitRect(f.x0 + slot * idx, f.yTop, slot, f.yBase - f.yTop + 24);
    setTip(hit, ctx, v);
    root.appendChild(hit);
  });
  return root;
}

/** Single full-width bar split into segments with 2px surface gaps. */
function renderStacked(ctx: ChartCtx, entries: [string, number][]): HTMLElement {
  const bar = mk("div", "stacked-bar");
  entries.forEach(([cat, pct], i) => {
    if (pct <= 0.05) return;
    const seg = mk("div", "stacked-seg");
    seg.style.flexBasis = `${clampPct(pct)}%`;
    seg.style.backgroundColor = colorFor(ctx.probeId, cat, i);
    setTip(seg, ctx, { cat, pct, i });
    bar.appendChild(seg);
  });
  return bar;
}

function polar(cx: number, cy: number, r: number, a: number): [number, number] {
  return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
}

/** Ring segment between radii r..R from angle a0 to a1 (radians, 0 = 12 o'clock, clockwise). */
function wedgePath(cx: number, cy: number, R: number, r: number, a0: number, a1: number): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = polar(cx, cy, R, a0);
  const [x1, y1] = polar(cx, cy, R, a1);
  const [x2, y2] = polar(cx, cy, r, a1);
  const [x3, y3] = polar(cx, cy, r, a0);
  const f = (n: number): string => n.toFixed(2);
  return (
    `M${f(x0)} ${f(y0)}A${R} ${R} 0 ${large} 1 ${f(x1)} ${f(y1)}` +
    `L${f(x2)} ${f(y2)}A${r} ${r} 0 ${large} 0 ${f(x3)} ${f(y3)}Z`
  );
}

/** Donut: a 16px ring of wedges separated by 2px gaps, leading category in the centre. */
function renderDonut(ctx: ChartCtx, entries: [string, number][]): Element {
  const size = 152;
  const c = size / 2;
  const R = 70;
  const r = 54;
  const rMid = (R + r) / 2;
  const root = svgRoot("chart-donut", size, size);

  const total = entries.reduce((acc, [, pct]) => acc + Math.max(0, pct), 0);
  const gap = 2 / rMid;
  const live = entries
    .map(([cat, pct], i) => ({ cat, pct, i }))
    .filter((e) => e.pct > 0 && total > 0);
  let acc = 0;
  for (const e of live) {
    const sweep = (Math.max(0, e.pct) / total) * Math.PI * 2;
    const color = colorFor(ctx.probeId, e.cat, e.i);
    if (live.length === 1) {
      const full = svg("circle", {
        cx: c,
        cy: c,
        r: rMid,
        fill: "none",
        stroke: color,
        "stroke-width": R - r,
      });
      setTip(full, ctx, e);
      root.appendChild(full);
    } else if (sweep > gap + 0.004) {
      const wedge = svg("path", {
        d: wedgePath(c, c, R, r, acc + gap / 2, acc + sweep - gap / 2),
        fill: color,
      });
      setTip(wedge, ctx, e);
      root.appendChild(wedge);
    }
    acc += sweep;
  }

  let top: { cat: string; pct: number } | null = null;
  for (const [cat, pct] of entries) if (top === null || pct > top.pct) top = { cat, pct };
  if (top && total > 0) {
    root.appendChild(svgText(c, c + 4, `${top.pct.toFixed(0)}%`, "chart-donut-pct"));
    root.appendChild(svgText(c, c + 22, clip(top.cat, 13), "chart-donut-name"));
  }
  return root;
}

/** Multi-line chart over each category's snapshot series (2px strokes, last-point dots). */
function renderLines(ctx: ChartCtx, views: CatView[]): Element {
  const W = ctx.width;
  const H = 148;
  const root = svgRoot("chart-lines", W, H);
  const f = chartFrame(root, W, H, { left: 40, right: 10, top: 8, bottom: 8 });
  const xFor = (idx: number, len: number): number =>
    len <= 1 ? f.x1 : f.x0 + (idx / (len - 1)) * (f.x1 - f.x0);

  const hits: SVGElement[] = [];
  for (const v of views) {
    const series = v.series;
    if (series.length === 0) continue;
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const pts = series.map((p, idx) => `${xFor(idx, series.length).toFixed(1)},${f.yFor(p).toFixed(1)}`);
    if (series.length > 1) {
      root.appendChild(
        svg("polyline", { class: "chart-line", stroke: color, points: pts.join(" ") }),
      );
    }
    const hit = svg("polyline", { class: "chart-hit-line", points: pts.join(" ") });
    setTip(hit, ctx, v);
    hits.push(hit);
  }
  for (const v of views) {
    if (v.series.length === 0) continue;
    lastDot(root, xFor(v.series.length - 1, v.series.length), f.yFor(v.series[v.series.length - 1]!), colorFor(ctx.probeId, v.cat, v.i));
  }
  for (const hit of hits) root.appendChild(hit);
  return root;
}

/**
 * 100%-stacked area over the snapshot history: how the mix evolves. Each
 * category is a band at 16% fill under a 2px boundary line; a single time
 * point degrades to one stack of gapped bands.
 */
function renderArea(ctx: ChartCtx, views: CatView[]): Element {
  const W = ctx.width;
  const H = 148;
  const root = svgRoot("chart-area", W, H);
  const f = chartFrame(root, W, H, { left: 40, right: 10, top: 8, bottom: 8 });
  if (views.length === 0) return root;

  const steps = views.reduce((m, v) => Math.max(m, v.series.length), 0);
  if (steps === 0) return root;

  // At each time point compute the normalized (to 100%) cumulative boundaries.
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
      const share = total > 0 ? (vals[i]! / total) * 100 : 0;
      lowers[i]![t] = cum;
      cum += share;
      uppers[i]![t] = cum;
    }
  }

  if (steps === 1) {
    views.forEach((v, i) => {
      const yTop = f.yFor(uppers[i]![0]!);
      const yBot = f.yFor(lowers[i]![0]!);
      if (yBot - yTop < 0.5) return;
      const band = svg("rect", {
        x: f.x0,
        y: (yTop + 1).toFixed(1),
        width: f.x1 - f.x0,
        height: Math.max(0.5, yBot - yTop - 2).toFixed(1),
        rx: 3,
        fill: colorFor(ctx.probeId, v.cat, v.i),
      });
      setTip(band, ctx, v);
      root.appendChild(band);
    });
    return root;
  }

  const xFor = (t: number): number => f.x0 + (t / (steps - 1)) * (f.x1 - f.x0);
  const lines: SVGElement[] = [];
  views.forEach((v, i) => {
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const top: string[] = [];
    const bottom: string[] = [];
    for (let t = 0; t < steps; t++) {
      top.push(`${xFor(t).toFixed(1)},${f.yFor(uppers[i]![t]!).toFixed(1)}`);
      bottom.push(`${xFor(t).toFixed(1)},${f.yFor(lowers[i]![t]!).toFixed(1)}`);
    }
    const band = svg("polygon", {
      class: "chart-area-band",
      points: top.concat(bottom.reverse()).join(" "),
      fill: color,
      "fill-opacity": 0.16,
    });
    setTip(band, ctx, v);
    root.appendChild(band);
    // The last band's upper edge IS the 100% boundary: a series-coloured line there
    // would just outline the chart frame, so only the interior boundaries get a stroke.
    if (i < views.length - 1) {
      lines.push(svg("polyline", { class: "chart-line", stroke: color, points: top.join(" ") }));
    }
  });
  for (const l of lines) root.appendChild(l);
  views.forEach((v, i) => {
    if (i === views.length - 1) return;
    lastDot(root, xFor(steps - 1), f.yFor(uppers[i]![steps - 1]!), colorFor(ctx.probeId, v.cat, v.i));
  });
  return root;
}

/** Diverging bars: change vs the previous snapshot (left = down, right = up), category colour. */
function renderDelta(ctx: ChartCtx, views: CatView[]): HTMLElement {
  const wrap = mk("div", "delta-chart");
  const deltas = views.map((v) => v.delta ?? 0);
  const maxAbs = Math.max(1, ...deltas.map((d) => Math.abs(d)));

  views.forEach((v, idx) => {
    const delta = deltas[idx]!;
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const row = mk("div", "delta-row");

    const label = mk("span", "delta-label", v.cat);
    label.title = v.cat;

    const track = mk("div", "delta-track");
    setTip(track, ctx, v, { note: `Change ${fmtDelta(v.delta)} pts vs previous` });
    const neg = mk("div", "delta-half delta-half-neg");
    const pos = mk("div", "delta-half delta-half-pos");
    const bar = mk("div", delta >= 0 ? "delta-bar delta-bar-pos" : "delta-bar delta-bar-neg");
    bar.style.width = `${(Math.abs(delta) / maxAbs) * 100}%`;
    bar.style.backgroundColor = color;
    (delta >= 0 ? pos : neg).appendChild(bar);
    track.appendChild(neg);
    track.appendChild(pos);

    const value = buildDeltaBadge(v.delta);
    value.classList.add("delta-value");

    row.appendChild(label);
    row.appendChild(track);
    row.appendChild(value);
    wrap.appendChild(row);
  });
  return wrap;
}

/**
 * Semicircular gauge for the leading category: a 12px track, the arc filling
 * pct/100, the percentage and name in the middle (text tokens, never series ink).
 */
function renderGauge(ctx: ChartCtx, views: CatView[]): Element {
  const width = 200;
  const height = 118;
  const cx = width / 2;
  const cy = 96;
  const radius = 80;
  const stroke = 12;
  const root = svgRoot("chart-gauge", width, height);

  const pointAt = (frac: number): [number, number] => {
    const angle = Math.PI * (1 - Math.max(0, Math.min(1, frac)));
    return [cx + radius * Math.cos(angle), cy - radius * Math.sin(angle)];
  };
  const arcPath = (frac: number): string => {
    const [sx, sy] = pointAt(0);
    const [ex, ey] = pointAt(frac);
    // The gauge is a semicircle, so any value arc spans at most 180 degrees: the
    // large-arc flag must stay 0 (setting it above 50% drew the long way round).
    return `M${sx.toFixed(1)} ${sy.toFixed(1)}A${radius} ${radius} 0 0 1 ${ex.toFixed(1)} ${ey.toFixed(1)}`;
  };

  let top: CatView | null = null;
  for (const v of views) if (top === null || v.pct > top.pct) top = v;
  const pct = top ? clampPct(top.pct) : 0;

  const track = svg("path", {
    class: "chart-gauge-track",
    d: arcPath(1),
    fill: "none",
    "stroke-width": stroke,
    "stroke-linecap": "round",
  });
  root.appendChild(track);
  if (top && pct > 0) {
    const arc = svg("path", {
      class: "chart-gauge-arc",
      d: arcPath(pct / 100),
      fill: "none",
      stroke: colorFor(ctx.probeId, top.cat, top.i),
      "stroke-width": stroke,
      "stroke-linecap": "round",
    });
    setTip(arc, ctx, top);
    root.appendChild(arc);
  } else if (top) {
    setTip(track, ctx, top);
  }

  root.appendChild(svgText(cx, cy - 24, `${pct.toFixed(0)}%`, "chart-gauge-pct"));
  root.appendChild(svgText(cx, cy - 4, top ? clip(top.cat, 22) : "No data", "chart-gauge-name"));
  const lo = svgText(cx - radius, cy + 18, "0%", "chart-tick");
  const hi = svgText(cx + radius, cy + 18, "100%", "chart-tick");
  root.appendChild(lo);
  root.appendChild(hi);
  return root;
}

/**
 * Heatmap: one ROW per category, columns = the last up-to-24 snapshots of that
 * category's series, filled from the sequential blue ramp (value / max). Row
 * names sit in a left gutter because colour no longer identifies the row.
 */
function renderHeatmap(ctx: ChartCtx, views: CatView[]): Element {
  const W = ctx.width;
  const gap = 2;
  const cellH = 20;
  const rows = views.length;
  const maxLen = views.reduce((m, v) => Math.max(m, v.series.length), 0);
  const cols = Math.max(1, Math.min(24, maxLen));
  const labelW = Math.min(112, Math.max(64, Math.round(W * 0.3)));
  const gridW = W - labelW - 4;
  const cellW = Math.max(3, Math.floor((gridW - gap * (cols - 1)) / cols));
  const axisH = 20;
  const H = Math.max(1, rows) * (cellH + gap) + axisH;
  const root = svgRoot("chart-heatmap", W, H);

  let maxV = 1;
  for (const v of views) {
    for (const value of v.series.slice(-cols)) maxV = Math.max(maxV, clampPct(value));
  }

  views.forEach((v, r) => {
    const y = r * (cellH + gap);
    root.appendChild(svgText(labelW - 8, y + cellH / 2, clip(v.cat, (labelW - 12) / 6.6), "chart-heat-label", "end")).setAttribute("dy", "0.35em");
    const series = v.series;
    const start = Math.max(0, series.length - cols);
    for (let c = 0; c < cols; c++) {
      const value = series[start + c];
      const x = labelW + c * (cellW + gap);
      if (value === undefined) {
        root.appendChild(svg("rect", { class: "chart-cell-empty", x, y, width: cellW, height: cellH, rx: 3 }));
        continue;
      }
      const fill = seqColor(clampPct(value) / maxV);
      const cell = svg("rect", { x, y, width: cellW, height: cellH, rx: 3, fill });
      const age = cols - 1 - c;
      const last = c === cols - 1;
      setTip(cell, ctx, { cat: v.cat, pct: value, i: v.i }, {
        note: last ? "Latest snapshot" : `${age} snapshot${age === 1 ? "" : "s"} earlier`,
        nonav: !last,
      });
      root.appendChild(cell);
      if (cellW >= 30) cellLabel(root, x + cellW / 2, y + cellH / 2, `${Math.round(value)}`, fill);
    }
  });
  const axisY = Math.max(1, rows) * (cellH + gap) + 14;
  root.appendChild(svgText(labelW, axisY, "Older", "chart-tick", "start"));
  root.appendChild(svgText(labelW + cols * (cellW + gap) - gap, axisY, "Now", "chart-tick", "end"));
  return root;
}

/** "Less to more" ramp legend shared by heatmap and descartes. */
function buildRampLegend(): HTMLElement {
  const legend = mk("div", "ramp-legend");
  legend.appendChild(mk("span", "", "Less"));
  legend.appendChild(mk("span", "ramp-bar"));
  legend.appendChild(mk("span", "", "More"));
  return legend;
}

/**
 * Winner's podium: the top 3 categories as blocks ordered 2nd, 1st, 3rd, heights
 * proportional to pct (1st tallest). Fewer than three simply show what exists.
 */
function renderPodium(ctx: ChartCtx, views: CatView[]): Element {
  const ranked = [...views].sort((a, b) => b.pct - a.pct).slice(0, 3);
  const W = ctx.width;
  const H = 156;
  const root = svgRoot("chart-podium", W, H);
  if (ranked.length === 0) return root;

  const slotOrder = [1, 0, 2].filter((r) => r < ranked.length);
  const slotW = W / slotOrder.length;
  const blockW = Math.min(76, slotW * 0.6);
  const baseline = H - 24;
  const maxPct = Math.max(1, ...ranked.map((v) => clampPct(v.pct)));
  const maxBarH = baseline - 26;

  root.appendChild(svg("line", { class: "chart-baseline", x1: 0, y1: baseline + 0.5, x2: W, y2: baseline + 0.5 }));
  slotOrder.forEach((rank, slot) => {
    const v = ranked[rank]!;
    const hgt = Math.max(2, (clampPct(v.pct) / maxPct) * maxBarH);
    const cx = slot * slotW + slotW / 2;
    const y = baseline - hgt;
    root.appendChild(
      svg("path", {
        d: topRoundPath(cx - blockW / 2, y, blockW, hgt, 4),
        fill: colorFor(ctx.probeId, v.cat, v.i),
      }),
    );
    root.appendChild(svgText(cx, y - 6, `#${rank + 1}  ${v.pct.toFixed(0)}%`, "chart-podium-value"));
    root.appendChild(svgText(cx, baseline + 16, clip(v.cat, (slotW - 8) / 6.4), "chart-xlabel"));
    const hit = hitRect(slot * slotW, 0, slotW, H);
    setTip(hit, ctx, v, { note: `Rank ${rank + 1}` });
    root.appendChild(hit);
  });
  return root;
}

/**
 * Violin plot: per category, a ~9-bin histogram of its series mirrored into a
 * symmetric shape, with a ringed dot at the current value. Categories with
 * fewer than 3 points degrade to a thin rounded bar.
 */
function renderViolin(ctx: ChartCtx, views: CatView[]): Element {
  const BINS = 9;
  const W = ctx.width;
  const H = 164;
  const root = svgRoot("chart-violin", W, H);
  if (views.length === 0) return root;
  const f = chartFrame(root, W, H, { left: 40, right: 4, top: 8, bottom: 22 });
  const slot = (f.x1 - f.x0) / views.length;
  const maxHalf = Math.min(30, slot * 0.4);

  views.forEach((v, idx) => {
    const cx = f.x0 + slot * idx + slot / 2;
    const color = colorFor(ctx.probeId, v.cat, v.i);
    const series = v.series;

    if (series.length < 3) {
      const y = f.yFor(v.pct);
      root.appendChild(
        svg("path", { d: topRoundPath(cx - 3, y, 6, Math.max(2, f.yBase - y), 3), fill: color }),
      );
    } else {
      const counts = new Array<number>(BINS).fill(0);
      for (const raw of series) {
        const b = Math.min(BINS - 1, Math.floor((clampPct(raw) / 100) * BINS));
        counts[b]! += 1;
      }
      const maxCount = Math.max(1, ...counts);
      // Span only the occupied value range (plus one empty bin each side) so a
      // narrow distribution is a compact shape, not a spike stretched to 0..100.
      let first = counts.findIndex((c) => c > 0);
      let last = BINS - 1 - [...counts].reverse().findIndex((c) => c > 0);
      first = Math.max(0, first - 1);
      last = Math.min(BINS - 1, last + 1);
      const rightPts: string[] = [];
      const leftPts: string[] = [];
      for (let b = first; b <= last; b++) {
        const y = f.yFor(((b + 0.5) / BINS) * 100);
        const half = (counts[b]! / maxCount) * maxHalf;
        rightPts.push(`${(cx + half).toFixed(1)},${y.toFixed(1)}`);
        leftPts.push(`${(cx - half).toFixed(1)},${y.toFixed(1)}`);
      }
      leftPts.reverse();
      root.appendChild(
        svg("path", {
          class: "chart-violin-shape",
          d: `M${rightPts.concat(leftPts).join("L")}Z`,
          fill: color,
          "fill-opacity": 0.85,
          stroke: color,
          "stroke-width": 1.5,
          "stroke-linejoin": "round",
        }),
      );
    }
    lastDot(root, cx, f.yFor(v.pct), color);
    if (slot >= 40) {
      root.appendChild(svgText(cx, f.yBase + 16, clip(v.cat, (slot - 4) / 6.4), "chart-xlabel"));
    }
    const hit = hitRect(f.x0 + slot * idx, f.yTop - 4, slot, f.yBase - f.yTop + 28);
    setTip(hit, ctx, v);
    root.appendChild(hit);
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
    showToast(`At most ${MAX_AXIS} ${axis === "x" ? "columns" : "rows"}.`, "info");
    return;
  }
  const base = nextAxisLabel(target, side) || (axis === "x" ? "new column" : "new row");
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
      `There is already a ${axis === "x" ? "column" : "row"} named "${trimmed}". Pick another name.`,
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
      `The last ${axis === "x" ? "column" : "row"} cannot be removed: the grid would be empty.`,
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

const TICK_ARM_MS = 3000;

/** One axis tick: a click-to-rename label + a hover-reveal "✕" to remove the
 *  whole column/row. Used for both the X (bottom) and Y (left) gutters. */
function buildAxisTick(probeId: string, axis: "x" | "y", label: string, gridClass: string): HTMLElement {
  const wrap = document.createElement("span");
  wrap.className = `${gridClass} descartes-tick`;

  const renderReadState = (): void => {
    wrap.classList.remove("is-editing");
    const text = document.createElement("button");
    text.type = "button";
    text.className = "descartes-tick-label";
    text.dataset["axis"] = axis;
    text.dataset["label"] = label;
    text.dataset["fk"] = `tick:${axis}:${label}`;
    text.textContent = label;
    text.title = `${label}. Click to rename`;
    text.addEventListener("click", enterEditState);

    const del = document.createElement("button");
    del.type = "button";
    del.className = "descartes-tick-del";
    del.dataset["fk"] = `del:${axis}:${label}`;
    del.innerHTML = iconSvg("x");
    del.title = axis === "x" ? "Remove column" : "Remove row";
    const delName = `${del.title} ${label}`;
    del.setAttribute("aria-label", delName);
    // Two-step remove: the first activation only ARMS the button (the removal
    // goes straight to the server with no undo, so a stray tap must never
    // delete a whole row/column). The second activation within TICK_ARM_MS
    // removes it. Esc, a tap elsewhere, or the timeout disarm it.
    let armTimer = 0;
    const disarm = (): void => {
      window.clearTimeout(armTimer);
      document.removeEventListener("pointerdown", onOutside, true);
      if (!del.classList.contains("is-armed")) return;
      del.classList.remove("is-armed");
      del.setAttribute("aria-label", delName);
      del.title = axis === "x" ? "Remove column" : "Remove row";
    };
    const onOutside = (ev: Event): void => {
      if (ev.target instanceof Node && del.contains(ev.target)) return;
      disarm();
    };
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      if (!del.classList.contains("is-armed")) {
        del.classList.add("is-armed");
        const what = axis === "x" ? "column" : "row";
        del.setAttribute("aria-label", `Confirm remove ${what} ${label}`);
        del.title = `Confirm remove ${what}`;
        showToast(`Press again to remove the ${what} "${label}".`, "info", TICK_ARM_MS);
        document.addEventListener("pointerdown", onOutside, true);
        armTimer = window.setTimeout(disarm, TICK_ARM_MS);
        return;
      }
      disarm();
      descartesRemoveAxis(probeId, axis, label);
    });
    del.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && del.classList.contains("is-armed")) {
        e.stopPropagation();
        disarm();
      }
    });
    del.addEventListener("blur", disarm);
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
        wrap.querySelector<HTMLElement>(".descartes-tick-label")?.focus();
      }
    });
    input.addEventListener("blur", commit);
    // The label and remove button stay in the flow (hidden) so the tick keeps
    // its width; the input overlays them, so the plot never shifts while editing.
    wrap.classList.add("is-editing");
    wrap.appendChild(input);
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
  btn.innerHTML = iconSvg("plus");
  btn.dataset["fk"] = `add:${axis}-${side}`;
  btn.title =
    axis === "x"
      ? side === "before"
        ? "Add column before"
        : "Add column after"
      : side === "before"
        ? "Add row below"
        : "Add row above";
  btn.setAttribute("aria-label", btn.title);
  btn.addEventListener("click", () => descartesAddAxis(probeId, axis, side));
  return btn;
}

function renderDescartes(views: CatView[], probeId: string, n: number): HTMLElement {
  const root = document.createElement("div");
  root.className = "descartes";
  const ctx: ChartCtx = { probeId, n, width: chartWidth(probeId) };

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
      "Grid (2D) places each message on an (x, y) coordinate of a Cartesian plane.";
    const gen = document.createElement("button");
    gen.type = "button";
    gen.className = "btn-secondary btn-small";
    gen.textContent = "Generate 3×3 grid";
    gen.addEventListener("click", () => {
      // Switching an existing (non-pair) classification probe to descartes must
      // never silently wipe its real categories.
      if (
        categories.length > 0 &&
        !window.confirm("This replaces the probe's current categories. Continue?")
      ) {
        return;
      }
      const seeded = deriveAxes(seedGridCategories());
      applyDescartesAxes(probeId, seeded.xs, seeded.ys);
      showToast("3×3 grid generated.", "success");
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
        cell.setAttribute("role", "img");
        cell.setAttribute("aria-label", `${x}, ${y}: no data`);
        setTip(cell, ctx, { cat: `(${x}, ${y})`, pct: 0, i: 0 }, { color: "transparent" });
      } else {
        const cellFill = seqColor(pct / maxPct);
        cell.style.background = cellFill;
        if (key === modalKey && modalPct > 0) cell.classList.add("is-modal");
        const abs = Math.round((pct / 100) * n);
        const value =
          valueMode === "abs" ? String(abs) : `${pct.toFixed(decimals)}%`;
        // Show the number in every cell that actually got a hit. The old
        // `pct >= 2` gate blanked single-vote cells on sparse grids — 2 of 130
        // messages is 1.5% < 2%, yet that count is exactly what to show. Unvoted
        // cells still carry pct 0 (they render at the min-intensity floor), so
        // gate on the real value: a non-zero count (# mode) or percentage (% mode).
        const hasValue =
          valueMode === "abs" ? abs >= 1 : Number(pct.toFixed(decimals)) > 0;
        if (hasValue) {
          const t = document.createElement("span");
          t.className = "descartes-pct";
          // One label treatment for every cell: a surface chip in text ink. No ink
          // reaches 7:1 on every ramp step, so the chip keeps contrast uniform.
          t.classList.add("is-chip");
          t.textContent = value;
          cell.appendChild(t);
        }
        setTip(cell, ctx, { cat: `(${x}, ${y})`, pct }, { color: cellFill });
        cell.setAttribute("role", "img");
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
  // Hover-reveal "+" on all 4 edges: left/right add a column before/after,
  // bottom/top add a row before(origin)/after — mirrors the axis-derivation order.
  // DOM (= Tab) order follows the screen positions around the plot: top, left,
  // plot, right, bottom. The buttons are absolutely positioned, so this is free.
  main.appendChild(buildAxisAddButton(probeId, "y", "after"));
  main.appendChild(buildAxisAddButton(probeId, "x", "before"));
  main.appendChild(plot);
  main.appendChild(buildAxisAddButton(probeId, "x", "after"));
  main.appendChild(buildAxisAddButton(probeId, "y", "before"));

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
  legend.appendChild(document.createTextNode("Less"));
  const bar = document.createElement("span");
  bar.className = "ramp-bar";
  legend.appendChild(bar);
  legend.appendChild(document.createTextNode("More"));
  const modeBtn = document.createElement("button");
  modeBtn.type = "button";
  modeBtn.className = "descartes-valuemode";
  modeBtn.textContent = valueMode === "abs" ? "#" : "%";
  modeBtn.dataset["fk"] = "valuemode";
  modeBtn.title = valueMode === "abs" ? "Show as percentage" : "Show as count (#)";
  modeBtn.setAttribute("aria-label", modeBtn.title);
  modeBtn.addEventListener("click", () => descartesToggleValueMode(probeId));
  legend.appendChild(modeBtn);
  foot.appendChild(legend);
  if (outro > 0.05) {
    const o = document.createElement("span");
    o.className = "descartes-outro";
    o.textContent = `Outside the grid: ${outro.toFixed(0)}%`;
    foot.appendChild(o);
  }
  root.appendChild(foot);

  return root;
}

/**
 * Build the classification .result-display content for the snapshot at `index`:
 * the chosen visualization wrapped in .chart-viz (role=img with a text summary,
 * one tab stop, arrow keys walk the marks), a visually hidden data table twin
 * and, under every chart EXCEPT "bars"/"descartes", the legend (.cat-stats).
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
  const ctx: ChartCtx = { probeId, n: snap.n, width: chartWidth(probeId) };
  const summary = chartSummary(type, probeState.get(probeId)?.label ?? "", views);

  const viz = document.createElement("div");
  viz.className = "chart-viz";
  viz.setAttribute("aria-label", summary);
  switch (type) {
    case "columns":
      viz.appendChild(renderColumns(ctx, views));
      break;
    case "stacked":
      viz.appendChild(renderStacked(ctx, entries));
      break;
    case "donut":
      viz.appendChild(renderDonut(ctx, entries));
      break;
    case "lines":
      viz.appendChild(renderLines(ctx, views));
      break;
    case "area":
      viz.appendChild(renderArea(ctx, views));
      break;
    case "delta":
      viz.appendChild(renderDelta(ctx, views));
      break;
    case "gauge":
      viz.appendChild(renderGauge(ctx, views));
      break;
    case "heatmap":
      viz.appendChild(renderHeatmap(ctx, views));
      viz.appendChild(buildRampLegend());
      break;
    case "podium":
      viz.appendChild(renderPodium(ctx, views));
      break;
    case "violin":
      viz.appendChild(renderViolin(ctx, views));
      break;
    case "descartes":
      viz.appendChild(renderDescartes(views, probeId, snap.n));
      break;
    default:
      viz.appendChild(renderBars(ctx, views));
      break;
  }
  body.appendChild(viz);

  if (type === "descartes") {
    // The grid carries interactive controls (rename, add, remove): a group, not an image.
    viz.setAttribute("role", "group");
  } else {
    viz.setAttribute("role", "img");
    viz.tabIndex = 0;
    viz.dataset["fk"] = "viz";
    // Same chart + same categories next frame: update the nodes in place.
    body.dataset["sig"] = `${probeId}|${type}|${ordered.join("\u0001")}`;
    body.appendChild(buildDataTable(views, summary));
  }

  // "bars" carries its own per-row stats; "descartes" IS a grid of every
  // category (a stats table would just duplicate all 16+ scorelines).
  if (type !== "bars" && type !== "descartes") {
    body.appendChild(buildStats(ctx, views, type !== "delta"));
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

/**
 * Placeholder shown until a probe's first result lands: the rough shape of the
 * chart (rows of bars) or of the text (lines), static under reduced motion.
 */
function buildSkeleton(kind: string, rows: number): HTMLDivElement {
  const box = document.createElement("div");
  box.className = "result-display is-skeleton";
  box.dataset["sk"] = `${kind}:${rows}`;
  const sk = mk("div", "skeleton");
  sk.setAttribute("aria-hidden", "true");
  if (kind === "classification") {
    const widths = [78, 52, 36, 24, 18, 12];
    for (let i = 0; i < rows; i++) {
      const row = mk("div", "sk-row");
      row.appendChild(mk("span", "sk-line sk-label"));
      const bar = mk("span", "sk-line sk-bar");
      bar.style.width = `${widths[i % widths.length]}%`;
      row.appendChild(bar);
      sk.appendChild(row);
    }
  } else {
    for (const w of [100, 94, 97, 58]) {
      const line = mk("span", "sk-line sk-text");
      line.style.width = `${w}%`;
      sk.appendChild(line);
    }
  }
  box.appendChild(sk);
  // Announced to assistive tech; sighted users read the same words in the footer.
  box.appendChild(mk("span", "sr-only", "Waiting for the first result"));
  return box;
}

/** Keep a not-yet-rendered card's skeleton in step with its kind and category count. */
function syncSkeleton(card: HTMLDivElement, probeId: string): void {
  const display = card.querySelector<HTMLDivElement>(".result-display");
  if (!display || !display.classList.contains("is-skeleton")) return;
  const kind = cardKind(probeId);
  const cats = probeState.get(probeId)?.categories?.length ?? 4;
  const rows = Math.min(6, Math.max(3, cats));
  if (display.dataset["sk"] !== `${kind}:${rows}`) display.replaceWith(buildSkeleton(kind, rows));
}

// ---------------------------------------------------------------------------
// In-place update. Rebuilding the chart every frame would drop hover, focus and
// any running CSS transition; when the next frame has the same shape (same
// chart, same categories) the new tree is merged into the live nodes instead.
// ---------------------------------------------------------------------------

function syncAttrs(from: Element, to: Element): void {
  for (const a of Array.from(from.attributes)) {
    if (!to.hasAttribute(a.name)) from.removeAttribute(a.name);
  }
  for (const a of Array.from(to.attributes)) {
    if (a.name === "class") {
      // Runtime-only highlight survives the merge.
      const next = from.classList.contains("is-active") ? `${a.value} is-active` : a.value;
      if (from.getAttribute("class") !== next) from.setAttribute("class", next);
    } else if (from.getAttribute(a.name) !== a.value) {
      from.setAttribute(a.name, a.value);
    }
  }
}

function morph(from: Element, to: Element): void {
  syncAttrs(from, to);
  const a = Array.from(from.childNodes);
  const b = Array.from(to.childNodes);
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const x = a[i];
    const y = b[i];
    if (!y) {
      if (x) from.removeChild(x);
    } else if (!x) {
      from.appendChild(y);
    } else if (x.nodeType === Node.TEXT_NODE && y.nodeType === Node.TEXT_NODE) {
      if (x.nodeValue !== y.nodeValue) x.nodeValue = y.nodeValue;
    } else if (
      x.nodeType === Node.ELEMENT_NODE &&
      y.nodeType === Node.ELEMENT_NODE &&
      x.nodeName === y.nodeName
    ) {
      morph(x as Element, y as Element);
    } else {
      from.replaceChild(y, x);
    }
  }
}

/** (Re)render a card's single .result-display at the given snapshot index. */
function renderDisplay(card: HTMLDivElement, probeId: string, kind: string, index: number): void {
  const existing = card.querySelector<HTMLDivElement>(".result-display");
  // Never clobber an in-progress descartes axis-tick rename (an <input> mid-edit,
  // e.g. because a periodic snapshot/result frame landed while the user was
  // typing) — the next render, once the user commits/cancels, picks up the
  // latest data anyway.
  if (existing?.querySelector(".descartes-tick-input:focus, .descartes-tick-del.is-armed")) return;
  const next =
    kind === "classification"
      ? buildClassificationBody(probeId, index)
      : buildOpenBody(probeId, index);
  const sig = existing?.dataset["sig"];
  if (existing && sig !== undefined && sig === next.dataset["sig"]) {
    morph(existing, next);
  } else if (existing) {
    const active = document.activeElement;
    const fk = active instanceof HTMLElement && existing.contains(active) ? active.dataset["fk"] : undefined;
    existing.replaceWith(next);
    if (fk) next.querySelector<HTMLElement>(`[data-fk="${CSS.escape(fk)}"]`)?.focus({ preventScroll: true });
  } else {
    card.insertBefore(next, card.querySelector(".result-scrubber"));
  }
  refreshTip();
}

/** Format the timestamp + message count string shown in the card footer. */
function snapStamp(snap: Snapshot): string {
  return `${new Date(snap.ts * 1000).toLocaleTimeString()} · ${snap.n} msgs`;
}

/**
 * Refresh the footer timeline (range bounds/value/fill, the stamp text node and
 * the Live toggle) to reflect the snapshot currently being viewed.
 */
function refreshScrubber(card: HTMLDivElement, probeId: string, index: number): void {
  const history = resultHistory.get(probeId) ?? [];
  const max = Math.max(0, history.length - 1);
  const snap = history[index];

  const scrubber = card.querySelector<HTMLDivElement>(".result-scrubber");
  const range = card.querySelector<HTMLInputElement>(".scrub-range");
  const meta = card.querySelector<HTMLDivElement>(".scrub-meta");
  const live = card.querySelector<HTMLButtonElement>(".scrub-live");

  if (scrubber) {
    scrubber.classList.toggle("is-single", history.length <= 1);
    scrubber.classList.toggle("is-empty", history.length === 0);
  }
  if (range) {
    range.min = "0";
    range.max = String(max);
    range.value = String(index);
    range.style.setProperty("--fill", max > 0 ? `${(index / max) * 100}%` : "100%");
    range.setAttribute("aria-valuetext", snap ? snapStamp(snap) : "");
  }
  if (meta) {
    // The stamp lives in the FIRST child text node; the Live toggle is the next child.
    const label = snap ? snapStamp(snap) : "";
    if (meta.firstChild && meta.firstChild.nodeType === Node.TEXT_NODE) {
      meta.firstChild.textContent = label;
    } else {
      meta.insertBefore(document.createTextNode(label), meta.firstChild);
    }
  }
  if (live) {
    const atLatest = index === max;
    live.setAttribute("aria-pressed", String(atLatest));
    live.classList.toggle("scrub-behind", !atLatest);
    live.title = atLatest
      ? "Following the latest snapshot"
      : `${max - index} snapshot${max - index === 1 ? "" : "s"} behind. Jump to the latest`;
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

// Two 380px cards + the 16px grid gap: mirrors the @container
// (max-width: 775px) breakpoint in style.css that collapses "span 2" back to
// one column when the results column can't actually fit two side by side.
const TWO_COL_MIN_WIDTH = 776;

// ---------------------------------------------------------------------------
// Drag-and-drop card reordering (native HTML5 DnD). Only the small grip handle
// in each card's header is draggable — never the whole card — so clicks,
// selects and inputs inside a card keep working. The order is client-only:
// no state frame carries it, and syncCardsFromState/upsertResultCard never
// reposition an existing card, so a plain DOM move survives every later
// frame. Persisted to localStorage so it also survives a page reload.
// ---------------------------------------------------------------------------

const CARD_ORDER_KEY = "viewlyt.cardorder";
let draggedCard: HTMLDivElement | null = null;

/** Snapshot the current on-screen card order (probe ids, top to bottom). */
function saveCardOrder(): void {
  const container = document.getElementById("results");
  if (!container) return;
  const ids = Array.from(
    container.querySelectorAll<HTMLDivElement>(".result-card[data-probe-id]"),
  )
    .map((c) => c.dataset["probeId"])
    .filter((id): id is string => id !== undefined);
  lsSet(CARD_ORDER_KEY, JSON.stringify(ids));
}

/** Reapply a previously-saved order to whatever cards exist right now. Cards
 *  not in the saved list (e.g. just added) are left wherever they already are. */
function applyStoredCardOrder(): void {
  const raw = lsGet(CARD_ORDER_KEY);
  if (!raw) return;
  let order: unknown;
  try {
    order = JSON.parse(raw);
  } catch {
    return;
  }
  if (!Array.isArray(order)) return;
  const container = document.getElementById("results");
  if (!container) return;
  for (const id of order) {
    if (typeof id !== "string") continue;
    const card = container.querySelector<HTMLDivElement>(
      `.result-card[data-probe-id="${CSS.escape(id)}"]`,
    );
    if (card) container.appendChild(card);
  }
}

/**
 * The card (other than the one being dragged) whose center is closest to
 * (x, y), plus whether the dragged card should land after it — needed because
 * the results grid can have several columns and span-2 cards, so a plain
 * "compare Y" rule isn't enough.
 */
function closestCard(
  container: HTMLElement,
  x: number,
  y: number,
): { el: HTMLDivElement; after: boolean } | null {
  const cards = Array.from(
    container.querySelectorAll<HTMLDivElement>(".result-card[data-probe-id]:not(.dragging)"),
  );
  let best: { el: HTMLDivElement; cx: number; cy: number; dist: number } | null = null;
  for (const card of cards) {
    const r = card.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    const dist = (x - cx) ** 2 + (y - cy) ** 2;
    if (!best || dist < best.dist) best = { el: card, cx, cy, dist };
  }
  if (!best) return null;
  const after = y > best.cy || (Math.abs(y - best.cy) < 12 && x > best.cx);
  return { el: best.el, after };
}

/** Wire the container-level drag-over: live-moves the dragged card as the
 *  pointer crosses other cards (the standard vanilla-JS sortable pattern). */
function wireCardDragAndDrop(): void {
  const container = el<HTMLDivElement>("results");
  container.addEventListener("dragover", (e: DragEvent) => {
    if (!draggedCard) return;
    e.preventDefault();
    const target = closestCard(container, e.clientX, e.clientY);
    if (!target || target.el === draggedCard) return;
    if (target.after) target.el.after(draggedCard);
    else target.el.before(draggedCard);
  });
  container.addEventListener("drop", (e: DragEvent) => {
    e.preventDefault();
  });
}

/**
 * Find (or build) the .result-card for a probe. A freshly-built card has the
 * FULL skeleton and its static handlers wired exactly once; the kind-specific
 * chrome (kind tag, label, prompt, chart-select / edit-categories visibility)
 * is applied later by updateCardChrome from state.
 *
 * Anatomy: progress bar, header (grip, title + kind tag + prompt, chart select,
 * one overflow menu), inline editor, inline error, display, footer timeline.
 */
let cardMenuSeq = 0;

function menuItem(cls: string, icon: string, label: string): HTMLButtonElement {
  const item = mk("button", `menu-item ${cls}`);
  item.type = "button";
  item.setAttribute("role", "menuitem");
  item.tabIndex = -1;
  item.appendChild(iconNode(icon));
  item.appendChild(mk("span", "menu-label", label));
  return item;
}

function setMenuItem(item: HTMLElement, icon: string, label: string): void {
  const holder = item.querySelector<HTMLElement>(".icon");
  if (holder) holder.innerHTML = iconSvg(icon);
  const text = item.querySelector<HTMLElement>(".menu-label");
  if (text) text.textContent = label;
}

/** Show/hide the chart-type control (select + its icon wrapper) together. */
function setChartSelectHidden(card: HTMLElement, hidden: boolean): void {
  card.querySelector(".chart-select")?.classList.toggle("hidden", hidden);
  card.querySelector(".chart-select-wrap")?.classList.toggle("hidden", hidden);
}

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

  // In-flight indicator: a 2px indeterminate bar on the top edge (.analyzing).
  const progress = mk("div", "card-progress");
  progress.setAttribute("aria-hidden", "true");
  card.appendChild(progress);

  // --- header ---
  const header = mk("div", "result-header");
  const headerTop = mk("div", "result-header-top");

  // Drag handle: reorders cards (see the drag-and-drop block above). Only this
  // element is draggable, never the card itself. Visible on hover/focus only.
  const dragHandle = mk("span", "card-drag-handle");
  dragHandle.draggable = true;
  dragHandle.title = "Drag to reorder";
  dragHandle.setAttribute("aria-hidden", "true");
  dragHandle.innerHTML = iconSvg("dots-six-vertical");
  dragHandle.addEventListener("dragstart", (e: DragEvent) => {
    draggedCard = card;
    e.dataTransfer?.setData("text/plain", probeId); // required by Firefox to start the drag
    if (e.dataTransfer) e.dataTransfer.effectAllowed = "move";
    e.dataTransfer?.setDragImage(card, 16, 16); // drag a preview of the whole card, not just the handle
    requestAnimationFrame(() => card.classList.add("dragging"));
  });
  dragHandle.addEventListener("dragend", () => {
    card.classList.remove("dragging");
    draggedCard = null;
    saveCardOrder();
  });
  headerTop.appendChild(dragHandle);

  // Title column: label + kind tag on one line, the prompt as a 2-line subtitle.
  const titles = mk("div", "result-titles");
  const titleRow = mk("div", "result-title-row");
  const labelEl = mk("h3", "result-label");
  const badge = mk("span", "result-kind-badge");
  badge.appendChild(iconNode(cardKind(probeId) === "classification" ? "chart-bar-horizontal" : "chat-text"));
  badge.appendChild(mk("span", "kind-text", kindName(cardKind(probeId))));
  titleRow.appendChild(labelEl);
  titleRow.appendChild(badge);
  const promptEl = mk("span", "result-prompt");
  titles.appendChild(titleRow);
  headerTop.appendChild(titles);

  const right = mk("div", "result-header-right");

  // Chart type (classification only): icon + friendly name; option.value is the id.
  const selectWrap = mk("div", "chart-select-wrap");
  selectWrap.appendChild(iconNode("chart-bar", "chart-select-icon"));
  const select = mk("select", "chart-select");
  select.setAttribute("aria-label", "Chart type");
  for (const t of CHART_TYPES) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = CHART_LABELS[t];
    select.appendChild(opt);
  }
  select.value = probeChart(probeId);
  select.title = CHART_LABELS[select.value as ChartType] ?? "Chart type";
  selectWrap.appendChild(select);
  selectWrap.appendChild(iconNode("caret-down", "chart-select-caret"));
  select.addEventListener("change", () => {
    const chosen = select.value as ChartType;
    select.title = CHART_LABELS[chosen] ?? "Chart type";
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
  right.appendChild(selectWrap);

  // The ONE overflow menu: Edit, Widen/Narrow, Collapse/Expand, Remove.
  const menuId = `card-menu-${++cardMenuSeq}`;
  const menuWrap = mk("div", "menu-wrap card-menu-wrap");
  const menuBtn = mk("button", "card-menu-btn");
  menuBtn.type = "button";
  menuBtn.setAttribute("aria-haspopup", "menu");
  menuBtn.setAttribute("aria-expanded", "false");
  menuBtn.setAttribute("aria-controls", menuId);
  menuBtn.setAttribute("aria-label", "Probe actions");
  menuBtn.title = "Probe actions";
  menuBtn.appendChild(iconNode("dots-three"));
  const menu = mk("div", "menu card-menu hidden");
  menu.id = menuId;
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Probe actions");

  const editBtn = menuItem("card-edit", "pencil-simple", "Edit");
  editBtn.addEventListener("click", () => {
    openEditor(probeId);
    card.querySelector<HTMLInputElement>(".edit-label")?.focus();
  });

  const spanBtn = menuItem("card-span", "arrows-out-line-horizontal", "Widen");

  const collapseBtn = menuItem("card-collapse", "arrows-in-line-vertical", "Collapse");
  const collapsedKey = "viewlyt.cardcollapsed." + probeId;
  const setCollapsed = (collapsed: boolean): void => {
    card.classList.toggle("collapsed", collapsed);
    setMenuItem(
      collapseBtn,
      collapsed ? "arrows-out-line-vertical" : "arrows-in-line-vertical",
      collapsed ? "Expand" : "Collapse",
    );
  };
  setCollapsed(lsGet(collapsedKey) === "1");
  collapseBtn.addEventListener("click", () => {
    const next = !card.classList.contains("collapsed");
    setCollapsed(next);
    lsSet(collapsedKey, next ? "1" : "0");
  });

  const removeBtn = menuItem("card-remove menu-item-danger", "trash", "Remove");
  removeBtn.addEventListener("click", () => {
    send({ op: "remove_probe", id: probeId });
    probeState.delete(probeId);
    resultHistory.delete(probeId);
    viewState.delete(probeId);
    cardWidths.delete(probeId);
    cardObserver?.unobserve(card);
    card.remove();
    // The focused item just left the DOM: park focus on a stable control nearby.
    document.getElementById("force-run")?.focus();
  });

  const sep = mk("div", "menu-sep");
  sep.setAttribute("role", "separator");
  menu.append(editBtn, spanBtn, collapseBtn, sep, removeBtn);
  menuWrap.append(menuBtn, menu);
  right.appendChild(menuWrap);
  headerTop.appendChild(right);
  header.appendChild(headerTop);
  // The subtitle is its own full-width row under the header controls.
  header.appendChild(promptEl);
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
  labelInput.setAttribute("aria-label", "Label");
  labelField.appendChild(labelInput);
  editor.appendChild(labelField);

  const promptField = document.createElement("div");
  promptField.className = "field-group";
  const promptInput = document.createElement("textarea");
  promptInput.className = "edit-prompt";
  promptInput.rows = 3;
  promptInput.placeholder = "Prompt";
  promptInput.setAttribute("aria-label", "Prompt");
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
  settingsHint.textContent = "Blank or 0 follows the global Window config";
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
  editor.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !ev.defaultPrevented) {
      ev.preventDefault();
      ev.stopPropagation();
      closeEditor(probeId);
    }
  });
  card.appendChild(editor);

  // --- inline error for this probe's last failed analysis (cleared by the next result) ---
  const errorEl = mk("div", "card-error hidden");
  errorEl.appendChild(iconNode("warning-circle"));
  errorEl.appendChild(mk("span", "card-error-text"));
  card.appendChild(errorEl);

  // --- display (skeleton until the first result arrives) ---
  const kindNow = cardKind(probeId);
  card.appendChild(
    buildSkeleton(kindNow, Math.min(6, Math.max(3, probeState.get(probeId)?.categories?.length ?? 4))),
  );

  // --- footer timeline: range + stamp + Live toggle, one row ---
  const scrubber = mk("div", "result-scrubber is-single is-empty");

  const range = document.createElement("input");
  range.className = "scrub-range";
  range.setAttribute("aria-label", "Snapshot history");
  range.type = "range";
  range.min = "0";
  range.max = "0";
  range.value = "0";
  range.step = "1";

  const scrubMeta = mk("div", "scrub-meta");
  // Leading text node (filled by refreshScrubber) + the Live toggle.
  scrubMeta.appendChild(document.createTextNode("Waiting for the first result"));
  const liveBadge = mk("button", "scrub-live", "Live");
  liveBadge.type = "button";
  liveBadge.setAttribute("aria-pressed", "true");
  liveBadge.title = "Following the latest snapshot";
  // Clicking Live snaps the scrubber back to the latest snapshot.
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
  // rAF-throttled: dragging fires `input` continuously, so render at most once
  // per frame (the handler reads range.value at frame time).
  let scrubQueued = false;
  range.addEventListener("input", () => {
    {
      const hist = resultHistory.get(probeId) ?? [];
      const max = Math.max(1, hist.length - 1);
      range.style.setProperty("--fill", `${(Number(range.value) / max) * 100}%`);
    }
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

  // --- column-span toggle (menu item): flips the card between one and two grid
  // columns. Purely a client display preference (like collapse), persisted per
  // probe in localStorage. ---
  const spanKey = "viewlyt.cardspan." + probeId;
  const setSpan = (wide: boolean): void => {
    card.classList.toggle("span-2", wide);
    spanBtn.classList.toggle("is-wide", wide);
    setMenuItem(
      spanBtn,
      wide ? "arrows-in-line-horizontal" : "arrows-out-line-horizontal",
      wide ? "Narrow" : "Widen",
    );
  };
  setSpan(lsGet(spanKey) === "1");
  spanBtn.addEventListener("click", () => {
    const next = !card.classList.contains("span-2");
    setSpan(next);
    lsSet(spanKey, next ? "1" : "0");
    // Below TWO_COL_MIN_WIDTH the @container query in style.css (kept in sync
    // by hand) forces every card back to one column. The menu hides this item
    // there, but keep the explanation for a window shrunk after it was opened.
    const resultsWidth = document.getElementById("results")?.clientWidth ?? Infinity;
    if (next && resultsWidth < TWO_COL_MIN_WIDTH) {
      showToast(
        "The window is too narrow for 2 columns right now. Your choice is saved " +
          "and applies once you widen the window.",
        "info",
      );
    }
  });

  // Widen/Narrow only exists where two columns fit (or when undoing a wide card).
  wireMenu(menuBtn, menu, () => {
    const fits = (document.getElementById("results")?.clientWidth ?? 0) >= TWO_COL_MIN_WIDTH;
    spanBtn.classList.toggle("hidden", !fits && !card.classList.contains("span-2"));
  });

  // Drop the top-level "Waiting for results…" hint (a DIRECT child of #results;
  // never a card's own nested placeholder) the moment the first card appears.
  const topHint = container.querySelector(":scope > .empty-hint");
  if (topHint) topHint.remove();

  container.prepend(card);
  cardObserver?.observe(card);
  return card;
}

function kindName(kind: string): string {
  return kind === "classification" ? "Classify" : "Open";
}

/**
 * Apply the read-only chrome from state: kind tag, label, prompt line, and
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
  if (badge) {
    const text = badge.querySelector<HTMLElement>(".kind-text");
    if (text && text.textContent !== kindName(kind)) {
      text.textContent = kindName(kind);
      const holder = badge.querySelector<HTMLElement>(".icon");
      if (holder) holder.innerHTML = iconSvg(kind === "classification" ? "chart-bar-horizontal" : "chat-text");
    }
  }

  const labelEl = card.querySelector<HTMLElement>(".result-label");
  if (labelEl) labelEl.textContent = probe?.label ?? "";

  const promptEl = card.querySelector<HTMLSpanElement>(".result-prompt");
  if (promptEl) {
    const prompt = kind === "classification" ? (probe?.question ?? "") : (probe?.instruction ?? "");
    promptEl.textContent = prompt;
    promptEl.title = prompt;
  }

  const select = card.querySelector<HTMLSelectElement>(".chart-select");
  if (select) {
    setChartSelectHidden(card, kind !== "classification");
    select.value = probeChart(probeId);
    select.title = CHART_LABELS[select.value as ChartType] ?? "Chart type";
  }

  // Editor's category rows only make sense for classification — but never
  // rebuild them while the editor is open mid-edit.
  if (!editorOpen(card)) {
    const editCats = card.querySelector<HTMLDivElement>(".edit-categories");
    if (editCats) editCats.classList.toggle("hidden", kind !== "classification");
  }
  syncSkeleton(card, probeId);
}

// ---------------------------------------------------------------------------
// Chart width: charts are drawn at the card's real pixel width (no stretched
// viewBox, so text and strokes keep their size). A ResizeObserver keeps the
// per-card width and re-renders a card when it actually changes (window resize,
// widen/narrow). Renders are batched in rAF, never inside the observer callback.
// ---------------------------------------------------------------------------

const cardWidths = new Map<string, number>();
const rerenderQueue = new Set<string>();
let rerenderQueued = false;

function chartWidth(probeId: string): number {
  return Math.max(220, cardWidths.get(probeId) ?? 340);
}

function flushRerenders(): void {
  rerenderQueued = false;
  for (const id of rerenderQueue) {
    const card = document.querySelector<HTMLDivElement>(
      `.result-card[data-probe-id="${CSS.escape(id)}"]`,
    );
    const history = resultHistory.get(id);
    if (!card || !history || history.length === 0) continue;
    renderDisplay(card, id, domKind(card), viewState.get(id)?.index ?? history.length - 1);
  }
  rerenderQueue.clear();
}

const cardObserver: ResizeObserver | null =
  typeof ResizeObserver === "undefined"
    ? null
    : new ResizeObserver((entries) => {
        for (const entry of entries) {
          const card = entry.target as HTMLElement;
          const id = card.dataset["probeId"];
          const w = Math.floor(entry.contentRect.width);
          if (id === undefined || w < 120) continue;
          if (Math.abs((cardWidths.get(id) ?? 0) - w) < 2) continue;
          cardWidths.set(id, w);
          rerenderQueue.add(id);
        }
        if (rerenderQueue.size > 0 && !rerenderQueued) {
          rerenderQueued = true;
          requestAnimationFrame(flushRerenders);
        }
      });

// ---------------------------------------------------------------------------
// Chart tooltips. One floating node; marks carry data-tip-* attributes, so the
// delegated listeners on #results survive every in-place chart update. Mouse and
// touch show it on the mark under the pointer; keyboard users get one tab stop
// per chart (.chart-viz) and the arrow keys walk the marks.
// ---------------------------------------------------------------------------

let tipEl: HTMLDivElement | null = null;
let tipTarget: Element | null = null;
let tipTimer = 0;
const vizIndex = new WeakMap<Element, number>();

function tipNode(): HTMLDivElement {
  if (!tipEl) {
    tipEl = mk("div", "chart-tip hidden");
    tipEl.setAttribute("aria-hidden", "true");
    document.body.appendChild(tipEl);
  }
  return tipEl;
}

function fillTip(mark: Element): void {
  const tip = tipNode();
  tip.replaceChildren();
  const head = mk("div", "chart-tip-head");
  const swatch = mk("span", "chart-tip-swatch");
  swatch.style.backgroundColor = mark.getAttribute("data-tip-color") ?? "transparent";
  head.appendChild(swatch);
  head.appendChild(mk("span", "chart-tip-name", mark.getAttribute("data-tip-name") ?? ""));
  tip.appendChild(head);
  const val = mk("div", "chart-tip-val");
  val.appendChild(mk("strong", "", mark.getAttribute("data-tip-pct") ?? ""));
  const count = mark.getAttribute("data-tip-count");
  if (count) val.appendChild(mk("span", "", count));
  tip.appendChild(val);
  const note = mark.getAttribute("data-tip-note");
  if (note) tip.appendChild(mk("div", "chart-tip-note", note));
}

function placeTip(x: number, y: number): void {
  const tip = tipNode();
  tip.classList.remove("hidden");
  const w = tip.offsetWidth;
  const hgt = tip.offsetHeight;
  const vw = document.documentElement.clientWidth;
  let left = x + 14;
  if (left + w > vw - 8) left = x - 14 - w;
  left = Math.max(8, left);
  let top = y - hgt - 12;
  if (top < 8) top = y + 18;
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

function showTipFor(mark: Element, x?: number, y?: number): void {
  window.clearTimeout(tipTimer);
  tipTarget = mark;
  fillTip(mark);
  if (x === undefined || y === undefined) {
    const r = mark.getBoundingClientRect();
    x = r.left + r.width / 2;
    y = r.top;
  }
  placeTip(x, y);
}

function hideTip(): void {
  window.clearTimeout(tipTimer);
  tipTarget = null;
  tipEl?.classList.add("hidden");
}

/** After an in-place chart update: keep a visible tooltip in step, or drop it if its mark is gone. */
function refreshTip(): void {
  if (!tipTarget) return;
  if (tipTarget.isConnected) fillTip(tipTarget);
  else hideTip();
}

function navMarks(viz: Element): Element[] {
  return Array.from(viz.querySelectorAll("[data-tip-name]:not([data-tip-nonav])"));
}

function activateMark(viz: Element, idx: number): void {
  const marks = navMarks(viz);
  if (marks.length === 0) return;
  const i = ((idx % marks.length) + marks.length) % marks.length;
  vizIndex.set(viz, i);
  viz.querySelector(".is-active")?.classList.remove("is-active");
  const mark = marks[i]!;
  mark.classList.add("is-active");
  showTipFor(mark);
}

function clearActive(viz: Element): void {
  viz.querySelector(".is-active")?.classList.remove("is-active");
  hideTip();
}

let tipScrollGraceUntil = 0;

function wireChartTips(): void {
  const root = el<HTMLDivElement>("results");
  const markAt = (target: EventTarget | null): Element | null => {
    const m = target instanceof Element ? target.closest("[data-tip-name]") : null;
    return m && root.contains(m) ? m : null;
  };

  root.addEventListener("pointermove", (e: PointerEvent) => {
    if (e.pointerType === "touch") return;
    const mark = markAt(e.target);
    if (mark) showTipFor(mark, e.clientX, e.clientY);
    else if (!root.querySelector(".chart-viz:focus-within .is-active")) hideTip();
  });
  root.addEventListener("pointerleave", (e: PointerEvent) => {
    if (e.pointerType === "touch") return;
    if (!root.querySelector(".chart-viz:focus-within .is-active")) hideTip();
  });
  root.addEventListener("pointerdown", (e: PointerEvent) => {
    if (e.pointerType !== "touch") return;
    const mark = markAt(e.target);
    if (!mark) {
      hideTip();
      return;
    }
    showTipFor(mark, e.clientX, e.clientY);
    tipTimer = window.setTimeout(hideTip, 2500);
  });

  root.addEventListener("focusin", (e: FocusEvent) => {
    const viz = e.target;
    if (viz instanceof HTMLElement && viz.classList.contains("chart-viz") && viz.matches(":focus-visible")) {
      // Focusing an off-screen chart scrolls it into view: that scroll must not dismiss the tip.
      tipScrollGraceUntil = performance.now() + 300;
      activateMark(viz, vizIndex.get(viz) ?? 0);
    }
  });
  root.addEventListener("focusout", (e: FocusEvent) => {
    const viz = e.target;
    if (viz instanceof HTMLElement && viz.classList.contains("chart-viz")) {
      // A tap on another chart moves focus here after it opened its own tip: only
      // dismiss the tip when it belongs to the chart that is losing focus.
      if (tipTarget && viz.contains(tipTarget)) clearActive(viz);
      else viz.querySelector(".is-active")?.classList.remove("is-active");
    }
  });
  root.addEventListener("keydown", (e: KeyboardEvent) => {
    const viz = e.target;
    if (!(viz instanceof HTMLElement) || !viz.classList.contains("chart-viz")) return;
    const cur = vizIndex.get(viz) ?? 0;
    const count = navMarks(viz).length;
    let next: number | null = null;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = cur + 1;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = cur - 1;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = count - 1;
    else if (e.key === "Escape") {
      clearActive(viz);
      return;
    }
    if (next === null) return;
    e.preventDefault();
    activateMark(viz, next);
  });
  // Page scroll moves marks out from under a fixed tooltip; inner scrollers
  // (the live chat feed scrolls constantly) must not dismiss it.
  window.addEventListener(
    "scroll",
    (e) => {
      if (e.target === document && performance.now() > tipScrollGraceUntil) hideTip();
    },
    { passive: true, capture: true },
  );
  window.addEventListener("blur", hideTip);
}

// ---------------------------------------------------------------------------
// Per-probe failure. The server's error frames carry no probe id, but a batch
// is bracketed by proc frames that list its probes: any probe in the batch that
// produced no result before the batch ended failed. The card shows it inline
// (on top of the toast) until its next successful result.
// ---------------------------------------------------------------------------

const batchPending = new Set<string>();
let batchCancelled = false;

function setCardError(probeId: string, message: string | null): void {
  const card = document.querySelector<HTMLDivElement>(
    `.result-card[data-probe-id="${CSS.escape(probeId)}"]`,
  );
  const box = card?.querySelector<HTMLDivElement>(".card-error");
  if (!box) return;
  box.classList.toggle("hidden", message === null);
  const text = box.querySelector<HTMLElement>(".card-error-text");
  if (text) text.textContent = message ?? "";
}

function trackBatch(msg: ProcMsg): void {
  if (msg.active) {
    batchPending.clear();
    batchCancelled = false;
    for (const id of msg.probe_ids ?? []) batchPending.add(id);
    return;
  }
  if (!batchCancelled) {
    for (const id of msg.probe_ids ?? []) {
      if (batchPending.has(id)) {
        setCardError(id, "The last analysis failed. This probe will run again on the next pass.");
      }
    }
  }
  batchPending.clear();
  batchCancelled = false;
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
  const count = probeState.size;
  const countEl = document.getElementById("probe-count");
  if (countEl) countEl.textContent = `${count} ${count === 1 ? "probe" : "probes"}`;
  renderResultsEmpty();
}

// ---------------------------------------------------------------------------
// Empty state for the probes grid: a DIRECT child .empty-hint of #results (the
// contract ensureCard relies on to drop it when the first card appears).
// ---------------------------------------------------------------------------

const EXAMPLE_PROMPTS = [
  "How is the crowd feeling?",
  "What are people complaining about?",
  "Which player gets the most praise?",
];

function renderResultsEmpty(): void {
  const container = el<HTMLDivElement>("results");
  const hint = container.querySelector<HTMLElement>(":scope > .empty-hint");
  if (container.querySelector(".result-card[data-probe-id]")) {
    hint?.remove();
    return;
  }
  if (hint?.classList.contains("empty-state")) return;
  hint?.remove();

  const box = document.createElement("div");
  box.className = "empty-hint empty-state";
  const icon = document.createElement("span");
  icon.className = "empty-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = iconSvg("lightbulb");
  const title = document.createElement("h3");
  title.textContent = "Ask the chat anything";
  const lead = document.createElement("p");
  lead.textContent =
    "Type a question above and viewlyt turns it into a live probe that reads the chat. Try one of these.";
  const prompts = document.createElement("div");
  prompts.className = "empty-prompts";
  for (const text of EXAMPLE_PROMPTS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "prompt-chip";
    chip.textContent = text;
    chip.addEventListener("click", () => {
      const input = el<HTMLInputElement>("ask-text");
      input.value = text;
      input.focus();
    });
    prompts.appendChild(chip);
  }
  const manual = document.createElement("button");
  manual.type = "button";
  manual.className = "link-btn";
  manual.textContent = "Add a probe manually";
  manual.addEventListener("click", () => openSettings("probes", manual));
  box.append(icon, title, lead, prompts, manual);
  container.appendChild(box);
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
  name.setAttribute("aria-label", "Category name");
  color.setAttribute("aria-label", `Colour for ${cat || "new category"}`);

  const up = document.createElement("button");
  up.className = "cat-up btn-secondary btn-small";
  up.type = "button";
  up.textContent = "↑";
  up.title = "Move up";
  up.setAttribute("aria-label", "Move category up");
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
  down.setAttribute("aria-label", "Move category down");
  down.addEventListener("click", () => {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("edit-cat-row")) row.parentElement?.insertBefore(next, row);
  });

  const del = document.createElement("button");
  del.className = "cat-del btn-danger btn-small";
  del.type = "button";
  del.textContent = "✕";
  del.title = "Delete category";
  del.setAttribute("aria-label", "Delete category");
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
      hint.textContent = "Edit columns and rows directly on the card's grid.";
      editCats.appendChild(hint);

      const axisRow = document.createElement("div");
      axisRow.className = "edit-axis-row";
      const xLabelInput = document.createElement("input");
      xLabelInput.className = "edit-xlabel";
      xLabelInput.type = "text";
      xLabelInput.placeholder = "What the X axis represents";
      xLabelInput.setAttribute("aria-label", "X axis title");
      xLabelInput.value = probe?.x_label ?? "";
      const yLabelInput = document.createElement("input");
      yLabelInput.className = "edit-ylabel";
      yLabelInput.type = "text";
      yLabelInput.placeholder = "What the Y axis represents";
      yLabelInput.setAttribute("aria-label", "Y axis title");
      yLabelInput.value = probe?.y_label ?? "";
      axisRow.appendChild(xLabelInput);
      axisRow.appendChild(yLabelInput);
      editCats.appendChild(axisRow);

      const decimalsField = document.createElement("label");
      decimalsField.className = "edit-setting";
      const decimalsSpan = document.createElement("span");
      decimalsSpan.textContent = "Decimal places (%)";
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
  if (!editor) return;
  // Hiding the editor would drop keyboard focus to <body>: hand it back to the
  // card's menu button when focus was inside the editor.
  const hadFocus = editor.contains(document.activeElement);
  editor.classList.add("hidden");
  if (hadFocus) card.querySelector<HTMLElement>(".card-menu-btn")?.focus();
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

  // This probe answered: it did not fail this batch, and any inline error is stale.
  batchPending.delete(msg.probe_id);
  setCardError(msg.probe_id, null);

  // The kind comes from state when known; fall back to the frame's kind so a
  // result that arrives before its state still renders correctly.
  const kind = probeState.has(msg.probe_id) ? cardKind(msg.probe_id) : msg.kind;
  card.dataset["kind"] = kind;
  // Make sure the chart-select is visible for classification even before state.
  const select = card.querySelector<HTMLSelectElement>(".chart-select");
  setChartSelectHidden(card, kind !== "classification");

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
  const toastIcon = document.createElement("span");
  toastIcon.className = "icon toast-icon";
  toastIcon.innerHTML = iconSvg(
    kind === "error" ? "warning-circle" : kind === "success" ? "check-circle" : "info",
  );
  toast.appendChild(toastIcon);
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  toast.appendChild(text);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.innerHTML = '<span class="icon">' + iconSvg("x") + "</span>";
  close.setAttribute("aria-label", "Dismiss notification");
  close.title = "Dismiss";
  close.addEventListener("click", () => removeToast(toast));
  toast.appendChild(close);
  container.appendChild(toast);
  while (container.childElementCount > TOAST_MAX && container.firstChild) {
    container.removeChild(container.firstChild);
  }
  window.setTimeout(() => removeToast(toast), ttlMs);
}

// The control that had focus before a toast took it: where focus returns when
// the toast (and its Dismiss button) goes away instead of dropping to <body>.
let focusBeforeToast: HTMLElement | null = null;
document.addEventListener("focusin", (e) => {
  const t = e.target;
  if (t instanceof HTMLElement && !t.closest("#toasts")) focusBeforeToast = t;
});

function removeToast(toast: HTMLElement): void {
  const had = toast.contains(document.activeElement);
  const sibling = (toast.nextElementSibling ?? toast.previousElementSibling) as HTMLElement | null;
  toast.remove();
  if (!had) return;
  const back =
    sibling?.querySelector<HTMLElement>(".toast-close") ??
    (focusBeforeToast?.isConnected ? focusBeforeToast : null) ??
    document.getElementById("ask-text");
  back?.focus({ preventScroll: true });
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
  const jump = document.getElementById("feed-jump");
  const hadFocus = !!jump && document.activeElement === jump;
  updateFeedJump();
  // The pill hides itself on activation: keep keyboard focus in the feed.
  if (hadFocus) feed.focus({ preventScroll: true });
}

function makeFeedLine(author: string, text: string): HTMLDivElement {
  const line = document.createElement("div");
  line.className = "feed-line";
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
  const stopBtn = document.getElementById("stop-batch") as HTMLButtonElement | null;
  if (stopBtn) {
    // A disabled button cannot keep focus: hand it to the neighbouring "Analyze
    // now" control instead of stranding it on <body>.
    const hadFocus = document.activeElement === stopBtn;
    stopBtn.disabled = !active;
    if (hadFocus && !active) document.getElementById("force-run")?.focus({ preventScroll: true });
  }
  if (active) {
    e.textContent = "analyzing…";
    e.classList.add("is-busy");
  } else {
    e.textContent = latencyMs != null ? `${latencyMs} ms` : "idle";
    e.classList.remove("is-busy");
  }
  const avgEl = document.getElementById("avg-latency");
  if (avgEl && avgLatencyMs != null) avgEl.textContent = `${avgLatencyMs} ms`;
}

/** Run-state pill: shows "Live" while running and "Paused" while paused, and
 *  toggles global pause on click. aria-pressed carries the paused state (the
 *  click handler reads it); the accessible name starts with the visible text
 *  and names the click's EFFECT. */
function setGlobalPause(paused: boolean): void {
  const btn = document.getElementById("global-pause") as HTMLButtonElement | null;
  if (!btn) return;
  btn.title = paused ? "Resume analysis" : "Pause analysis";
  // Accessible name carries the visible text ("Live" / "Paused") plus the action.
  btn.setAttribute("aria-label", paused ? "Paused, resume analysis" : "Live, pause analysis");
  btn.classList.toggle("is-paused", paused);
  btn.setAttribute("aria-pressed", String(paused));
  btn.querySelector(".run-live")?.classList.toggle("hidden", paused);
  document.getElementById("brand-accent")?.classList.toggle("is-paused", paused);
  document.getElementById("paused-badge")?.classList.toggle("hidden", !paused);
}

/**
 * Glow the border of exactly the cards IN THIS BATCH — each probe can run on
 * its own cadence, so "analyzing" is per-card, never a single global light.
 * Called from the transient 'proc' frame (batch start/end) and from 'state'
 * (so a dashboard that connects/reconnects mid-batch still sees it).
 */
function setAnalyzingProbes(probeIds: string[], active: boolean): void {
  const ids = new Set(probeIds);
  document.querySelectorAll<HTMLDivElement>(".result-card").forEach((card) => {
    const id = card.dataset["probeId"];
    card.classList.toggle("analyzing", active && id !== undefined && ids.has(id));
  });
}

// ---------------------------------------------------------------------------
// Cost frame (A5): update the Cost stat card. Always shows USD (OpenRouter
// reports real spend on usage.cost; local providers render "$0.0000"). Both
// elements are optional — bail quietly if the stats-bar hasn't got the Cost card.
// ---------------------------------------------------------------------------

// Budget cap (B8): the last-known spending budget in USD (0 = off) and the most
// recent cost frame. handleState refreshes the cost label's "/budget" suffix on
// a budget change by re-rendering from this cached frame.
let currentBudget = 0;
let lastCost: CostMsg | null = null;

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
    // Budget mode: show spent / budget (0 spend renders as "$0.0000 / $<budget>").
    main.textContent =
      "$" + msg.cost_total.toFixed(4) + " / $" + currentBudget.toFixed(2);
  } else {
    // Always dollars — even before any spend (renders "$0.0000"), so a fresh /
    // paused session never flips the Cost card to a confusing token count.
    main.textContent = "$" + msg.cost_total.toFixed(4);
  }

  delta.textContent = signed(msg.cost_delta, (n) => "$" + n.toFixed(4));
  updateBudgetMeter();
}

/** Thin budget meter under the Cost KPI: hidden without a budget; accent while
 *  under 80%, warning from 80%, critical at 100%. */
function updateBudgetMeter(): void {
  const meter = document.getElementById("budget-meter");
  if (!meter) return;
  if (currentBudget <= 0) {
    meter.classList.add("hidden");
    return;
  }
  const spent = lastCost?.cost_total ?? 0;
  const ratio = Math.min(1, Math.max(0, spent / currentBudget));
  const pct = Math.round(ratio * 100);
  meter.classList.remove("hidden");
  meter.style.setProperty("--meter", String(ratio));
  meter.setAttribute("aria-valuenow", String(pct));
  meter.setAttribute("aria-valuetext", `${pct}% of the budget used`);
  meter.dataset["level"] = ratio >= 1 ? "full" : ratio >= 0.8 ? "near" : "ok";
}

/** Messages KPI + the live-chat rail counter. */
function setIngested(n: number): void {
  el<HTMLSpanElement>("ingested").textContent = String(n);
  const chat = document.getElementById("chat-count");
  if (chat) chat.textContent = `${n} ${n === 1 ? "msg" : "msgs"}`;
}

// ---------------------------------------------------------------------------
// Decider status (Model tab line + top-bar chip). Icon AND text carry the state,
// never colour alone. Both server fields are optional (older servers omit them);
// nothing renders when neither is present.
// ---------------------------------------------------------------------------

const DECIDER_ICON: Record<DeciderState, string> = {
  off: "info",
  inactive: "info",
  ok: "check-circle",
  degraded: "warning-circle",
  open: "warning-circle",
};

function setStateIcon(host: HTMLElement | null, name: string): void {
  if (host && host.dataset["icon"] !== name) {
    host.dataset["icon"] = name;
    host.innerHTML = iconSvg(name);
  }
}

function renderDecider(msg: StateMsg): void {
  const line = document.getElementById("decider-status");
  const lineText = document.getElementById("decider-status-text");
  const chip = document.getElementById("decider-chip");
  const chipText = document.getElementById("decider-chip-text");
  const known = msg.decider_state !== undefined || msg.model.decider_active !== undefined;
  let state: DeciderState = msg.decider_state ?? (msg.model.decider_active ? "ok" : "off");
  const active =
    msg.model.decider_active ?? (state === "ok" || state === "degraded" || state === "open");
  if (active && (state === "off" || state === "inactive")) state = "ok";
  const model = (msg.model.decider_model ?? "").trim();

  const text: Record<DeciderState, string> = {
    off: "Decider off. Classification uses the chat model.",
    inactive:
      "Decider inactive: this provider has no decisions endpoint, classification uses the chat model.",
    ok: model ? `Decider active. Classification runs on ${model}.` : "Decider active.",
    degraded: "Decider degraded. Some classifications fall back to the chat model.",
    open: "Decider failing, falling back to the chat model.",
  };
  if (line && lineText) {
    line.classList.toggle("hidden", !known);
    line.dataset["state"] = state;
    lineText.textContent = text[state];
    setStateIcon(line.querySelector<HTMLElement>(".icon"), DECIDER_ICON[state]);
  }
  if (chip && chipText) {
    chip.classList.toggle("hidden", !(known && active));
    chip.dataset["state"] = state;
    chip.title = text[state];
    chipText.textContent =
      state === "degraded" ? "Decider degraded" : state === "open" ? "Decider failing" : "Decider on";
    setStateIcon(chip.querySelector<HTMLElement>(".icon"), DECIDER_ICON[state]);
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
  setInputVal("decider_model", typeof msg.model.decider_model === "string" ? msg.model.decider_model : "");
  renderDecider(msg);
  // api_key is intentionally not filled (write-only)

  // Spending budget (B8): remember it, mirror it onto the #budget field (when
  // present) and re-render the Cost card so its "/budget" suffix tracks changes.
  currentBudget = Number(msg.model.budget) || 0;
  const budgetEl = document.getElementById("budget") as HTMLInputElement | null;
  if (budgetEl) budgetEl.value = String(currentBudget);
  if (lastCost) handleCost(lastCost);
  updateBudgetMeter();

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
  setIngested(msg.ingested);
  setProc(!!msg.processing, msg.latency_ms, msg.avg_latency_ms);

  // Budget stop is sticky state, not a transient error: show a persistent header
  // chip while analyses are paused by the spending cap (late joiners see it too).
  const budgetFlag = document.getElementById("budget-flag");
  if (budgetFlag) budgetFlag.classList.toggle("hidden", !msg.budget_blocked);
  setGlobalPause(msg.paused);
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
  // Reapply any user drag-reorder — syncCardsFromState only creates/removes
  // cards, it never repositions existing ones, so this is safe to redo on
  // every state frame (idempotent once the DOM already matches it).
  applyStoredCardOrder();
  // Cards now exist (ensureCard, just above) — safe to toggle their glow. Runs
  // AFTER syncCardsFromState so a dashboard connecting mid-batch sees it too.
  const analyzingIds = msg.analyzing_probe_ids ?? [];
  setAnalyzingProbes(analyzingIds, analyzingIds.length > 0);

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
        setIngested(msg.ingested);
        el<HTMLSpanElement>("buffer").textContent = String(msg.buffer);
        break;
      case "error":
        // An LLM failure (bad model, no key, rate limit, ...) answers suggest_probes/
        // decompose_probe with this frame instead of 'suggestions' — without resetting
        // here too, the button stays stuck on "Thinking…"/"Splitting…" (disabled) until
        // its 10s/15s safety timeout, which reads as the button being broken.
        setSuggestPending(false);
        setSplitPending(false);
        showError(msg.message);
        break;
      case "chat":
        appendFeed(msg.items);
        break;
      case "proc":
        setProc(msg.active, msg.latency_ms, msg.avg_latency_ms);
        setAnalyzingProbes(msg.probe_ids ?? [], msg.active);
        trackBatch(msg);
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

/** Pending buttons stay focusable (aria-disabled, not `disabled`): a disabled
 *  button that holds focus drops it to <body>. The click handlers already
 *  ignore activation while their pending flag is set. */
function setPendingAttr(btn: HTMLButtonElement, pending: boolean): void {
  if (pending) btn.setAttribute("aria-disabled", "true");
  else btn.removeAttribute("aria-disabled");
}

function setAskPending(pending: boolean): void {
  askPending = pending;
  const btn = document.getElementById("ask-send") as HTMLButtonElement | null;
  if (btn) {
    setPendingAttr(btn, pending);
    setBtnLabel(btn, pending ? "Rewriting…" : "Add");
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
    setPendingAttr(btn, pending);
    setBtnLabel(btn, pending ? "Thinking…" : "Suggest");
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
    setPendingAttr(btn, pending);
    setBtnLabel(btn, pending ? "Splitting…" : "Split");
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
      const hadFocus = document.activeElement === chip;
      const nextFocus = (chip.nextElementSibling ?? chip.previousElementSibling) as HTMLElement | null;
      chip.remove();
      if (!container.querySelector(".suggest-chip")) container.innerHTML = "";
      // The chip held focus and is gone: move to the next chip/Dismiss, or
      // back to the ask field once the tray is cleared.
      if (hadFocus) (nextFocus?.isConnected ? nextFocus : document.getElementById("ask-text"))?.focus();
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
      document.getElementById("ask-text")?.focus();
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
  const original = getBtnLabel(btn);
  btn.dataset["flashing"] = "1";
  setBtnLabel(btn, text);
  window.setTimeout(() => {
    setBtnLabel(btn, original);
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
  el<HTMLButtonElement>("global-pause").addEventListener("click", () => {
    const paused = el<HTMLButtonElement>("global-pause").getAttribute("aria-pressed") === "true";
    send({ op: paused ? "resume" : "pause" });
  });
  el("clear").addEventListener("click", () => send({ op: "clear" }));
  el("force-run").addEventListener("click", () => send({ op: "force_run" }));
  el("stop-batch").addEventListener("click", () => {
    send({ op: "stop_batch" });
    batchCancelled = true;
    showToast("Analysis stopped.", "info");
  });

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
    op["decider_model"] = inputVal("decider_model").trim();
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
    showToast("Probe added. First analysis runs right away.", "success");
  });

  el("import-json").addEventListener("click", () => {
    const statusEl = el<HTMLSpanElement>("import-json-status");
    // Inline text stays for sighted context; the toast is the one live-region announcement.
    const report = (msg: string, kind: ToastKind): void => {
      statusEl.textContent = msg;
      showToast(msg, kind);
    };
    const raw = el<HTMLTextAreaElement>("probe-json").value.trim();
    if (!raw) {
      report("Paste some JSON first.", "info");
      return;
    }

    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      report("Invalid JSON: " + (err instanceof Error ? err.message : "parse error"), "error");
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
    if (added > 0) report(`Imported ${added} probe(s).`, "success");
    else report("No probes found.", "info");
  });

  // ---- Ask-bar ----

  function setAskKind(kind: AskKind): void {
    askKind = kind;
    el<HTMLButtonElement>("ask-auto").classList.toggle("ask-chip-active", kind === "auto");
    el<HTMLButtonElement>("ask-open").classList.toggle("ask-chip-active", kind === "open");
    el<HTMLButtonElement>("ask-classify").classList.toggle("ask-chip-active", kind === "classify");
    const kinds: AskKind[] = ["auto", "open", "classify"];
    kinds.forEach((k) =>
      el<HTMLButtonElement>("ask-" + k).setAttribute("aria-pressed", String(k === kind)),
    );
    // Slide the selected-segment indicator (CSS reads --seg-i).
    document.querySelector<HTMLElement>(".seg")?.style.setProperty("--seg-i", String(kinds.indexOf(kind)));
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
      setBtnLabel(toggleBtn, "Hide");
    } else {
      apiKeyEl.type = "password";
      setBtnLabel(toggleBtn, "Show");
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
// Theme toggle: System -> Light -> Dark. Stored in 'viewlyt.theme' (the
// 'viewlyt.' prefix matters: Reset purges that prefix). System = no data-theme
// attribute, so the CSS follows prefers-color-scheme.
// ---------------------------------------------------------------------------

type ThemePref = "system" | "light" | "dark";
const THEME_KEY = "viewlyt.theme";
const THEME_NEXT: Record<ThemePref, ThemePref> = {
  system: "light",
  light: "dark",
  dark: "system",
};
const THEME_LABEL: Record<ThemePref, string> = {
  system: "System",
  light: "Light",
  dark: "Dark",
};
const THEME_ICON: Record<ThemePref, string> = {
  system: "desktop",
  light: "sun",
  dark: "moon",
};

function themePref(): ThemePref {
  const stored = lsGet(THEME_KEY);
  return stored === "light" || stored === "dark" ? stored : "system";
}

/** Re-paint every rendered chart so JS-coloured marks follow the new theme. */
function rerenderAllDisplays(): void {
  document.querySelectorAll<HTMLDivElement>(".result-card[data-probe-id]").forEach((card) => {
    const id = card.dataset["probeId"];
    if (id === undefined) return;
    const history = resultHistory.get(id);
    if (!history || history.length === 0) return;
    const index = viewState.get(id)?.index ?? history.length - 1;
    renderDisplay(card, id, domKind(card), index);
  });
}

function applyTheme(pref: ThemePref): void {
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const next = THEME_NEXT[pref];
    btn.setAttribute(
      "aria-label",
      `Theme: ${THEME_LABEL[pref]}. Switch to ${THEME_LABEL[next]}`,
    );
    btn.title = `Theme: ${THEME_LABEL[pref]}. Click for ${THEME_LABEL[next]}`;
    const holder = btn.querySelector<HTMLElement>(".icon");
    if (holder) holder.innerHTML = iconSvg(THEME_ICON[pref]);
  }
  readThemeColors();
  rerenderAllDisplays();
}

function wireTheme(): void {
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.addEventListener("click", () => {
      const next = THEME_NEXT[themePref()];
      if (next === "system") {
        try {
          localStorage.removeItem(THEME_KEY);
        } catch {
          // Storage may be unavailable; the choice just won't persist.
        }
      } else {
        lsSet(THEME_KEY, next);
      }
      applyTheme(next);
    });
  }
  // Following the OS: repaint JS-coloured marks when the system theme flips.
  window
    .matchMedia("(prefers-color-scheme: dark)")
    .addEventListener("change", () => {
      if (themePref() === "system") applyTheme("system");
    });
  applyTheme(themePref());
}

// ---------------------------------------------------------------------------
// Settings drawer (<dialog>): Window / Model / Probes / Capture tabs.
// ---------------------------------------------------------------------------

type SettingsTab = "window" | "model" | "probes" | "capture";
const SETTINGS_TABS: SettingsTab[] = ["window", "model", "probes", "capture"];
const SETTINGS_TAB_KEY = "viewlyt.settingsTab";
let drawerOpener: HTMLElement | null = null;

function storedSettingsTab(): SettingsTab {
  const stored = lsGet(SETTINGS_TAB_KEY);
  return (SETTINGS_TABS as string[]).includes(stored ?? "") ? (stored as SettingsTab) : "window";
}

function selectSettingsTab(tab: SettingsTab, focusTab = false): void {
  for (const t of SETTINGS_TABS) {
    const btn = el<HTMLButtonElement>("tab-" + t);
    const on = t === tab;
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1;
    el<HTMLElement>("panel-" + t).hidden = !on;
    if (on && focusTab) btn.focus();
  }
  lsSet(SETTINGS_TAB_KEY, tab);
}

function openSettings(tab?: SettingsTab, opener?: HTMLElement): void {
  const dlg = el<HTMLDialogElement>("settings-drawer");
  const active = document.activeElement;
  drawerOpener =
    opener ?? (active instanceof HTMLElement && active !== document.body ? active : null);
  selectSettingsTab(tab ?? storedSettingsTab());
  if (!dlg.open) dlg.showModal();
  // Toasts must stay readable above the modal: park the stack inside the dialog
  // while it is open (the top layer paints over anything left in the page).
  dlg.appendChild(el("toasts"));
  const selected = SETTINGS_TABS.find((t) => !el<HTMLElement>("panel-" + t).hidden);
  if (selected) el<HTMLButtonElement>("tab-" + selected).focus();
}

function wireSettingsDrawer(): void {
  const dlg = el<HTMLDialogElement>("settings-drawer");
  const settingsBtn = el<HTMLButtonElement>("settings-open");
  settingsBtn.addEventListener("click", () => openSettings(undefined, settingsBtn));
  el<HTMLButtonElement>("settings-close").addEventListener("click", () => dlg.close());

  // Backdrop click closes, but only when the press STARTED on the backdrop too
  // (a text-selection drag that ends outside must not dismiss the drawer).
  let pressOnBackdrop = false;
  dlg.addEventListener("pointerdown", (e) => {
    pressOnBackdrop = e.target === dlg;
  });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg && pressOnBackdrop) dlg.close();
    pressOnBackdrop = false;
  });

  dlg.addEventListener("close", () => {
    document.body.appendChild(el("toasts"));
    const back =
      drawerOpener && drawerOpener.isConnected && !drawerOpener.hasAttribute("disabled")
        ? drawerOpener
        : settingsBtn;
    drawerOpener = null;
    back.focus();
  });

  // Tabs: click + roving arrow keys (APG tabs pattern).
  for (const t of SETTINGS_TABS) {
    const btn = el<HTMLButtonElement>("tab-" + t);
    btn.addEventListener("click", () => selectSettingsTab(t));
    btn.addEventListener("keydown", (e: KeyboardEvent) => {
      const i = SETTINGS_TABS.indexOf(t);
      let next = -1;
      if (e.key === "ArrowRight") next = (i + 1) % SETTINGS_TABS.length;
      else if (e.key === "ArrowLeft") next = (i - 1 + SETTINGS_TABS.length) % SETTINGS_TABS.length;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = SETTINGS_TABS.length - 1;
      if (next < 0) return;
      e.preventDefault();
      selectSettingsTab(SETTINGS_TABS[next]!, true);
    });
  }
  selectSettingsTab(storedSettingsTab());

  // Feed empty state -> Capture tab.
  const openCapture = document.getElementById("open-capture");
  if (openCapture) {
    openCapture.addEventListener("click", () => openSettings("capture", openCapture));
  }
}

// ---------------------------------------------------------------------------
// Chat rail: sticky column >= 1200px; below that a collapsible panel
// (remembered; closed by default under 1000px). The feed keeps its own scroll,
// so reopening snaps to the newest edge instead of leaving a stale offset.
// ---------------------------------------------------------------------------

const CHAT_OPEN_KEY = "viewlyt.chatOpen";

/**
 * DOM order = visual order = Tab order at every width. At >= 1200px the rail is the
 * right column, so it comes after the probes; below that it is the collapsible panel
 * between the composer and the probes, so it moves in front of them. Moving a node
 * resets its scroll offset, so the feed's scrollTop is carried across.
 */
let railFeedStuck = true;

function placeChatRail(rail: HTMLElement, wide: boolean): void {
  const results = document.querySelector<HTMLElement>(".results-panel");
  if (!results) return;
  const inPlace = wide ? results.nextElementSibling === rail : rail.nextElementSibling === results;
  if (inPlace) return;
  const feed = document.getElementById("feed");
  const top = feed ? feed.scrollTop : 0;
  // The breakpoint has already re-laid the feed out by now, so "was it following the
  // newest line" comes from the last scroll event, not from the current geometry.
  const stuck = feed ? railFeedStuck : false;
  // Moving a node drops focus to <body>: put it back on the same control.
  const focused = rail.contains(document.activeElement) ? (document.activeElement as HTMLElement) : null;
  if (wide) results.after(rail);
  else results.before(rail);
  if (feed) feed.scrollTop = top;
  if (stuck && feed) {
    // Layout changed height while detached: keep following the newest line.
    feed.scrollTop = feedNewestFirst ? 0 : feed.scrollHeight;
  }
  focused?.focus({ preventScroll: true });
}

function wireChatRail(): void {
  const rail = el<HTMLElement>("chat-rail");
  const toggle = el<HTMLButtonElement>("chat-toggle");
  const wideQuery = window.matchMedia("(min-width: 1200px)");
  placeChatRail(rail, wideQuery.matches);
  const feedEl = document.getElementById("feed");
  feedEl?.addEventListener(
    "scroll",
    () => {
      railFeedStuck = feedAtStickEdge(feedEl);
    },
    { passive: true },
  );
  const stored = lsGet(CHAT_OPEN_KEY);
  let open = stored === null ? window.innerWidth >= 1000 : stored === "1";

  const apply = (): void => {
    rail.classList.toggle("is-collapsed", !open);
    toggle.setAttribute("aria-expanded", String(open));
    const label = toggle.querySelector(".chat-toggle-label");
    if (label) label.textContent = open ? "Hide" : "Show";
  };
  apply();

  toggle.addEventListener("click", () => {
    open = !open;
    lsSet(CHAT_OPEN_KEY, open ? "1" : "0");
    apply();
    if (open) jumpFeedToEdge();
  });

  wideQuery.addEventListener("change", (e) => {
    placeChatRail(rail, e.matches);
    if (e.matches) jumpFeedToEdge();
  });
}

// ---------------------------------------------------------------------------
// Menu button (Export). Proper menu pattern: aria-haspopup/expanded, arrow keys,
// Home/End, Esc closes and returns focus, click outside closes.
// ---------------------------------------------------------------------------

function wireMenu(btn: HTMLButtonElement, menu: HTMLElement, beforeOpen?: () => void): void {
  const items = (): HTMLElement[] =>
    Array.from(menu.querySelectorAll<HTMLElement>('[role="menuitem"]')).filter(
      (item) => !item.classList.contains("hidden"),
    );
  const isOpen = (): boolean => !menu.classList.contains("hidden");
  /** Flip upward / shift sideways when the menu would leave the viewport. */
  const place = (): void => {
    menu.classList.remove("menu-up", "menu-left");
    const b = btn.getBoundingClientRect();
    const m = menu.getBoundingClientRect();
    const below = window.innerHeight - b.bottom - 8;
    const above = b.top - 8;
    if (m.height + 6 > below && above > below) menu.classList.add("menu-up");
    if (m.left < 8) menu.classList.add("menu-left");
  };
  const open = (focusIndex: number): void => {
    beforeOpen?.();
    menu.classList.remove("hidden");
    place();
    btn.setAttribute("aria-expanded", "true");
    const list = items();
    list[(focusIndex + list.length) % list.length]?.focus();
  };
  const close = (returnFocus: boolean): void => {
    if (!isOpen()) return;
    menu.classList.add("hidden");
    btn.setAttribute("aria-expanded", "false");
    if (returnFocus) btn.focus();
  };

  btn.addEventListener("click", () => (isOpen() ? close(true) : open(0)));
  btn.addEventListener("keydown", (e: KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      open(0);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      open(-1);
    }
  });
  menu.addEventListener("keydown", (e: KeyboardEvent) => {
    const list = items();
    const i = list.indexOf(document.activeElement as HTMLElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      list[(i + 1) % list.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      list[(i - 1 + list.length) % list.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      list[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      list[list.length - 1]?.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      close(true);
    } else if (e.key === "Tab") {
      close(false);
    }
  });
  // After an item runs: hand focus back to the trigger, unless the action moved
  // it somewhere meaningful (e.g. into an editor field).
  menu.addEventListener("click", (e) => {
    if (!(e.target as Element).closest('[role="menuitem"]')) return;
    // Safari does not focus a button on mouse click: treat <body> as "unmoved" too.
    const a = document.activeElement;
    close(!a || a === document.body || menu.contains(a));
  });
  // A resize can strand an open menu off-screen: re-measure while it is open.
  const onResize = (): void => {
    if (!btn.isConnected) window.removeEventListener("resize", onResize);
    else if (isOpen()) place();
  };
  window.addEventListener("resize", onResize);
  const outside = (e: MouseEvent): void => {
    // A removed card takes its menu with it: drop the listener too.
    if (!btn.isConnected) {
      document.removeEventListener("click", outside);
      return;
    }
    const target = e.target as Node;
    if (isOpen() && !menu.contains(target) && !btn.contains(target)) close(false);
  };
  document.addEventListener("click", outside);
}

/** Copy buttons wired by attribute: <button data-copy-target="elementId">. */
function wireCopyButtons(): void {
  document.querySelectorAll<HTMLButtonElement>("[data-copy-target]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = document.getElementById(btn.dataset["copyTarget"] ?? "");
      if (!target) return;
      const text = target instanceof HTMLTextAreaElement ? target.value : (target.textContent ?? "");
      navigator.clipboard
        .writeText(text)
        .then(() => flashButton(btn, "Copied ✓"))
        .catch(() => undefined);
    });
  });
}

/**
 * The KPI strip never hides a metric behind an invisible sideways scroll on
 * desktop: when its content does not fit next to the brand and the controls, the
 * top bar wraps to two rows (`.is-wrapped`, with hysteresis so it cannot flap).
 * Where the strip still scrolls (phones) it gets an edge fade and is a keyboard
 * stop (WCAG 2.1.1 for scrollers).
 */
function wireKpiStrip(): void {
  const strip = document.querySelector<HTMLElement>(".kpi-strip");
  const inner = strip?.closest<HTMLElement>(".topbar-inner") ?? null;
  if (!strip || !inner) return;
  const shown = (e: Element): boolean => e instanceof HTMLElement && e.offsetWidth > 0;
  const sync = (): void => {
    const wrapped = inner.classList.contains("is-wrapped");
    if (!wrapped) {
      if (strip.scrollWidth > strip.clientWidth + 1) inner.classList.add("is-wrapped");
    } else {
      const icss = getComputedStyle(inner);
      const gap = parseFloat(icss.columnGap) || 0;
      const others = Array.from(inner.children).filter((c) => c !== strip && shown(c));
      const othersW = others.reduce((n, c) => n + (c as HTMLElement).offsetWidth, 0) + gap * others.length;
      const avail = inner.clientWidth - parseFloat(icss.paddingLeft) - parseFloat(icss.paddingRight) - othersW;
      const kids = Array.from(strip.children).filter(shown);
      const sgap = parseFloat(getComputedStyle(strip).columnGap) || 0;
      const need = kids.reduce((n, c) => n + (c as HTMLElement).offsetWidth, 0) + sgap * Math.max(0, kids.length - 1) + 8;
      if (need + 24 <= avail) inner.classList.remove("is-wrapped");
    }
    const scrolls = strip.scrollWidth > strip.clientWidth + 1;
    strip.classList.toggle("is-scrollable", scrolls);
    if (scrolls) strip.setAttribute("tabindex", "0");
    else strip.removeAttribute("tabindex");
  };
  const bar = inner.closest<HTMLElement>(".topbar");
  const syncBar = (): void => {
    if (bar) document.documentElement.style.setProperty("--topbar-h", `${bar.offsetHeight}px`);
  };
  const ro = new ResizeObserver(() => {
    sync();
    syncBar();
  });
  if (bar) ro.observe(bar);
  ro.observe(strip);
  ro.observe(inner);
  Array.from(strip.children).forEach((c) => ro.observe(c));
  sync();
  syncBar();
}

/** Shorter composer placeholder where the long one would be cut mid-word. */
function wireComposerPlaceholder(): void {
  const input = document.getElementById("ask-text") as HTMLInputElement | null;
  if (!input) return;
  const long = input.placeholder;
  const q = window.matchMedia("(max-width: 520px)");
  const apply = (): void => {
    input.placeholder = q.matches ? "Ask the chat anything" : long;
  };
  q.addEventListener("change", apply);
  apply();
}

function wireShell(): void {
  wireComposerPlaceholder();
  wireKpiStrip();
  wireSettingsDrawer();
  wireChatRail();
  wireMenu(el<HTMLButtonElement>("export-menu-btn"), el<HTMLElement>("export-menu"));
  wireCopyButtons();
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

readThemeColors();
injectIcons();
wireTheme();
updateProbeFieldVisibility();
wireButtons();
wireFeed();
wireShell();
wireCardDragAndDrop();
wireChartTips();
connectDashboard();
connectControl();
loadSnippet();
