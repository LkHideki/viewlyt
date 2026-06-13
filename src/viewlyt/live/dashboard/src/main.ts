// viewlyt.live — Control Panel main script
// Strict TypeScript, dependency-free.

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
  window: { n: number; overlap: number; gap: number; mode: string };
  model: { base_url: string; model: string };
  paused: boolean;
  ingested: number;
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

interface ProbeDescriptor {
  id: string;
  kind: string;
  label: string;
  question?: string;
  categories?: string[];
  instruction?: string;
}

type InboundMsg = StateMsg | ResultMsg | StatMsg;

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
    textEl.textContent = msg.text;
    card.appendChild(textEl);
  }
}

// ---------------------------------------------------------------------------
// State message handler
// ---------------------------------------------------------------------------

function handleState(msg: StateMsg): void {
  // Window inputs
  setInputVal("n", msg.window.n);
  setInputVal("overlap", msg.window.overlap);
  setInputVal("gap", msg.window.gap);
  el<HTMLSelectElement>("mode").value = msg.window.mode;

  // Model inputs
  setInputVal("base_url", msg.model.base_url);
  setInputVal("model", msg.model.model);
  // api_key is intentionally not filled (write-only)

  // Stats
  el<HTMLSpanElement>("ingested").textContent = String(msg.ingested);

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
      overlap: Number(inputVal("overlap")),
      gap: Number(inputVal("gap")),
      mode: el<HTMLSelectElement>("mode").value,
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

  el("copy-snippet").addEventListener("click", () => {
    const text = el<HTMLTextAreaElement>("snippet").value;
    navigator.clipboard.writeText(text).catch(() => {
      // Fallback: select the textarea so the user can copy manually
      el<HTMLTextAreaElement>("snippet").select();
    });
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
