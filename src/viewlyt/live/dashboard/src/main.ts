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
    model: "openai/gpt-4o-mini",
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

function upsertResultCard(msg: ResultMsg): void {
  const container = el<HTMLDivElement>("results");

  // Remove empty-hint on first result
  const hint = container.querySelector(".empty-hint");
  if (hint) hint.remove();

  let card = container.querySelector<HTMLDivElement>(
    `[data-probe-id="${CSS.escape(msg.probe_id)}"]`
  );
  if (!card) {
    card = document.createElement("div");
    card.className = "result-card";
    card.dataset["probeId"] = msg.probe_id;
    container.prepend(card);
  }

  card.innerHTML = "";

  const header = document.createElement("div");
  header.className = "result-header";

  const labelEl = document.createElement("span");
  labelEl.className = "result-label";
  labelEl.textContent = msg.label;

  const meta = document.createElement("span");
  meta.className = "result-meta";
  meta.textContent = `n=${msg.n}  ·  ${new Date(msg.ts * 1000).toLocaleTimeString()}`;

  header.appendChild(labelEl);
  header.appendChild(meta);
  card.appendChild(header);

  if (msg.kind === "classification" && msg.pct) {
    const barsEl = document.createElement("div");
    barsEl.className = "bars";
    const categories = Object.entries(msg.pct);
    categories.forEach(([cat, pct], i) => {
      const row = document.createElement("div");
      row.className = "bar-row";

      const catLabel = document.createElement("span");
      catLabel.className = "bar-label";
      catLabel.textContent = cat;

      const track = document.createElement("div");
      track.className = "bar-track";

      const fill = document.createElement("div");
      fill.className = "bar-fill";
      fill.style.width = `${Math.max(0, Math.min(100, pct))}%`;
      fill.style.backgroundColor = getCategoryColor(i);

      const pctLabel = document.createElement("span");
      pctLabel.className = "bar-pct";
      pctLabel.textContent = `${pct.toFixed(1)}%`;

      track.appendChild(fill);
      row.appendChild(catLabel);
      row.appendChild(track);
      row.appendChild(pctLabel);
      barsEl.appendChild(row);
    });
    card.appendChild(barsEl);
  } else if (msg.kind === "open" && msg.text) {
    const textEl = document.createElement("p");
    textEl.className = "result-text";
    textEl.innerHTML = renderMarkdown(msg.text);
    card.appendChild(textEl);
  }
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
    card.className = "result-card";
    card.dataset["error"] = "1";
    card.style.borderLeft = "3px solid #ea4335";
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
    const label = inputVal("probe-label");
    if (!label) {
      el<HTMLInputElement>("probe-label").focus();
      return;
    }
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
// Bootstrap
// ---------------------------------------------------------------------------

updateProbeFieldVisibility();
wireButtons();
connectDashboard();
connectControl();
loadSnippet();
