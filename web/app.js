const state = {
  bootstrap: null,
  hand: [2, 8, 9, 3, 12, 12],
  target: 163,
  currentRace: null,
  polling: false,
  pollTimer: null,
  analyzeTimer: null,
  analyzeSeq: 0,
  history: [],
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const STATUS_LABEL = {
  waiting: "waiting",
  running: "running",
  solved: "verified",
  unsolved: "no solution",
  error: "error",
  cancelled: "cancelled",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function parseIso(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function fmtClock(ms) {
  const total = Math.max(0, Math.floor(Number(ms) || 0));
  const minutes = Math.floor(total / 60000);
  const seconds = Math.floor((total % 60000) / 1000);
  const millis = total % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function fmtShort(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}

function fmtTime(iso) {
  const date = parseIso(iso);
  if (!date) return "—";
  return date.toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function fmtDate(iso) {
  const date = parseIso(iso);
  if (!date) return "—";
  return date.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtTokens(value) {
  return new Intl.NumberFormat().format(Number(value) || 0);
}

function fmtCost(value) {
  const cost = Number(value) || 0;
  if (cost === 0) return "$0";
  if (cost < 0.000001) return `$${cost.toExponential(2)}`;
  return `$${cost.toFixed(6)}`;
}

function laneElapsed(lane, now = Date.now()) {
  if (lane.elapsed_ms !== null && lane.elapsed_ms !== undefined) return lane.elapsed_ms;
  const started = parseIso(lane.started_at);
  if (!started) return 0;
  return Math.max(0, now - started.getTime());
}

function raceElapsed(race, now = Date.now()) {
  const started = parseIso(race.started_at);
  if (!started) return 0;
  const finished = parseIso(race.finished_at);
  return Math.max(0, (finished ? finished.getTime() : now) - started.getTime());
}

function isDone(race) {
  return race && ["done", "cancelled", "error"].includes(race.status);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && (data.detail || data.error || data.message);
    throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return data;
}

function showToast(message, bad = false) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.toggle("bad", bad);
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 4200);
}

function rulesFromControls() {
  return {
    must_use_all: $("#must-use-all").checked,
    allow_reuse: false,
    integer_only: $("#integer-only").checked,
  };
}

function renderCards() {
  const container = $("#cards");
  container.innerHTML = state.hand
    .map(
      (value, index) => `
      <div class="card">
        <input class="card-input" data-index="${index}" type="number" min="1" max="99" value="${escapeHtml(value)}" aria-label="Card ${index + 1}">
      </div>`
    )
    .join("");
  container.querySelectorAll(".card-input").forEach((input) => {
    input.addEventListener("change", () => {
      const index = Number(input.dataset.index);
      const value = Math.max(1, Math.min(99, Math.round(Number(input.value) || 1)));
      state.hand[index] = value;
      input.value = value;
      scheduleAnalyze();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") input.blur();
    });
  });
}

function renderRaceHand() {
  const clock = $("#race-clock");
  $("#race-hand").textContent = `${state.hand.join(" · ")} → ${state.target}`;
  if (!state.currentRace) {
    clock.textContent = "00:00.000";
    clock.dataset.startedAt = "";
    clock.dataset.elapsedMs = "";
  }
}

function populateSelects(data) {
  const textSelect = $("#text-model");
  const decisionSelect = $("#decisions-model");
  const textModels = data.nam_models?.length ? data.nam_models : [{ id: data.defaults.text_model }];
  const decisionModels = data.decision_models || [];

  textSelect.innerHTML = textModels
    .map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.id)}</option>`)
    .join("");
  if (!textSelect.querySelector(`option[value="${CSS.escape(data.defaults.text_model)}"]`)) {
    textSelect.insertAdjacentHTML(
      "afterbegin",
      `<option value="${escapeHtml(data.defaults.text_model)}">${escapeHtml(data.defaults.text_model)}</option>`
    );
  }
  textSelect.value = data.defaults.text_model;

  const heuristic = data.heuristic_model || "harness/heuristic";
  const options = decisionModels
    .map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name || model.id)}</option>`)
    .join("");
  decisionSelect.innerHTML = `<optgroup label="Local harness"><option value="${escapeHtml(
    heuristic
  )}">${escapeHtml(heuristic)} · no API</option></optgroup><optgroup label="OpenRouter decisions">${options}</optgroup>`;
  if (
    decisionModels.length &&
    !decisionSelect.querySelector(`option[value="${CSS.escape(data.defaults.decisions_model)}"]`)
  ) {
    decisionSelect.querySelector("optgroup:last-of-type").insertAdjacentHTML(
      "beforeend",
      `<option value="${escapeHtml(data.defaults.decisions_model)}">${escapeHtml(data.defaults.decisions_model)}</option>`
    );
  }
  decisionSelect.value = data.defaults.decisions_model;
}

function renderKeys(keys) {
  const mapping = [
    ["key-nam", keys.nam],
    ["key-openrouter", keys.openrouter],
  ];
  for (const [id, status] of mapping) {
    const node = document.getElementById(id);
    if (!node) continue;
    node.classList.toggle("ok", !!status.ok);
    node.classList.toggle("bad", !status.ok);
    const detail = node.querySelector("strong + span");
    if (detail) detail.textContent = status.detail || (status.ok ? "connected" : "unavailable");
    node.title = status.detail || "";
  }
  if (keys.openrouter && !keys.openrouter.ok) {
    const hint = $("#hint");
    if (hint && !hint.dataset.original) {
      hint.dataset.original = hint.textContent;
      hint.textContent =
        "OpenRouter is not authenticating. The decisions lane will surface the provider error; pick the harness heuristic to exercise the same lane without the API.";
    }
  }
}

function renderDataset(data) {
  const node = $("#dataset-status");
  if (!node) return;
  const count = data.dataset?.count ?? 0;
  node.textContent = `dataset · ${count} hand${count === 1 ? "" : "s"}`;
}

function setDefaults(data) {
  const defaults = data.defaults;
  $("#target").value = defaults.target;
  $("#must-use-all").checked = defaults.rules.must_use_all;
  $("#integer-only").checked = defaults.rules.integer_only;
  $("#attempts").value = defaults.text_max_attempts;
  $("#max-expansions").value = defaults.max_expansions;
  $("#baseline").checked = !!defaults.include_baseline;
  state.hand = defaults.hand.slice();
  state.target = defaults.target;
}

function scheduleAnalyze() {
  clearTimeout(state.analyzeTimer);
  state.analyzeTimer = setTimeout(analyze, 280);
}

async function analyze() {
  const seq = ++state.analyzeSeq;
  const container = $("#ground-truth");
  container.className = "ground-truth";
  container.innerHTML = `<span class="eyebrow">Exact solver</span><div class="gt-line"><span class="spinner tiny"></span> analyzing…</div>`;
  renderRaceHand();
  try {
    const data = await api("/api/analyze", {
      method: "POST",
      body: JSON.stringify({
        hand: state.hand,
        target: state.target,
        rules: rulesFromControls(),
      }),
    });
    if (seq !== state.analyzeSeq) return;
    container.classList.add(data.solvable ? "ok" : "bad");
    if (!data.solvable) {
      container.innerHTML = `<span class="eyebrow">Exact solver</span><div class="gt-line"><strong>Unsolvable</strong> under the current rules.</div>`;
      return;
    }
    const steps = data.solution || [];
    const stepText = steps.map((step) => escapeHtml(step.text)).join(" → ");
    container.innerHTML = `
      <span class="eyebrow">Exact solver</span>
      <div class="gt-line"><strong>Solvable</strong> in ${data.steps} steps · ${fmtTokens(data.nodes)} nodes</div>
      <div class="gt-solution">${stepText || "—"}</div>
      ${steps.length ? `<div class="gt-solution"><span>${escapeHtml(expressionHint(steps))}</span></div>` : ""}
    `;
  } catch (error) {
    if (seq !== state.analyzeSeq) return;
    container.classList.add("bad");
    container.innerHTML = `<span class="eyebrow">Exact solver</span><div class="gt-line">${escapeHtml(error.message)}</div>`;
  }
}

function expressionHint(steps) {
  if (!steps.length) return "";
  const parts = [];
  const available = [];
  for (const step of steps) {
    const leftIndex = available.findIndex((item) => item.value === step.left);
    const left = leftIndex >= 0 ? available.splice(leftIndex, 1)[0] : { value: step.left, expr: String(step.left) };
    const rightIndex = available.findIndex((item) => item.value === step.right);
    const right = rightIndex >= 0 ? available.splice(rightIndex, 1)[0] : { value: step.right, expr: String(step.right) };
    available.push({ value: step.result, expr: `(${left.expr} ${step.op} ${right.expr})` });
  }
  const final = available.find((item) => item.value === steps[steps.length - 1].result) || available[0];
  if (!final) return "";
  const expression = final.expr.startsWith("(") && final.expr.endsWith(")") ? final.expr.slice(1, -1) : final.expr;
  parts.push(expression);
  return parts.join("");
}

function progressFor(lane, race) {
  if (!lane) return 0;
  if (lane.status === "waiting") return 0;
  if (lane.status === "solved") return 100;
  if (lane.status === "unsolved" || lane.status === "error") return 100;
  if (lane.kind === "text") return 55;
  const stepsDone = lane.beam && lane.beam.length ? Math.max(...lane.beam.map((item) => item.history.length)) : 0;
  const total = Math.max(1, (race.config.hand.length || 6) - 1);
  return Math.min(92, Math.max(10, (stepsDone / total) * 100));
}

function laneBody(lane, race) {
  const chunks = [];
  const error = lane.error || (lane.verification && !lane.verification.ok ? lane.verification.error : null);
  if (error) chunks.push(`<div class="lane-error">${escapeHtml(error)}</div>`);

  const solution = lane.solution;
  if (solution && solution.steps && solution.steps.length) {
    chunks.push(
      `<ol class="steps">${solution.steps
        .map(
          (step, index) =>
            `<li><span class="step-index">${index + 1}</span><span>${escapeHtml(step.text)}</span></li>`
        )
        .join("")}</ol>`
    );
    if (solution.expression) {
      chunks.push(
        `<div class="expression"><span class="label">expr</span>${escapeHtml(solution.expression)} · verified</div>`
      );
    }
  } else if (lane.kind !== "text" && lane.beam && lane.beam.length) {
    chunks.push(
      `<div class="beam">${lane.beam
        .slice(0, 4)
        .map(
          (item) =>
            `<div class="beam-line"><b>[${escapeHtml(item.numbers.join(", "))}]</b> ${escapeHtml(
              (item.history || []).slice(-2).join(" · ")
            )}</div>`
        )
        .join("")}</div>`
    );
  } else if (lane.status === "running") {
    chunks.push(`<div class="muted">${lane.kind === "text" ? "waiting for a complete JSON solution…" : "searching…"}</div>`);
  }

  const trace = (lane.trace || []).slice(-3);
  if (trace.length) {
    chunks.push(
      `<div class="trace">${trace
        .map(
          (item) =>
            `<div class="trace-line"><span>${fmtTime(item.at)}</span> ${escapeHtml(item.message)}</div>`
        )
        .join("")}</div>`
    );
  }
  return chunks.join("");
}

function laneHtml(lane, race) {
  const status = lane.status || "waiting";
  const winner = isDone(race) && race.winner === lane.kind;
  const solved = status === "solved";
  const statusClass = solved ? "solved" : status;
  const clockClass = solved ? "solved" : status === "unsolved" || status === "error" ? status : "";
  const progress = progressFor(lane, race);
  const tokens = lane.tokens?.total || 0;
  return `
    <article class="lane ${winner ? "winner" : ""}" data-kind="${escapeHtml(lane.kind)}" data-started-at="${escapeHtml(
    lane.started_at || ""
  )}" data-elapsed-ms="${lane.elapsed_ms ?? ""}">
      <div class="lane-head">
        <div class="lane-title">
          <span class="dot"></span>
          <div>
            <h3 title="${escapeHtml(lane.model)}">${escapeHtml(lane.model)}</h3>
            <span class="provider">${escapeHtml(lane.provider)}</span>
          </div>
        </div>
        <span class="lane-status ${statusClass}">${escapeHtml(STATUS_LABEL[status] || status)}</span>
      </div>
      <div class="lane-stats">
        <div class="lane-clock ${clockClass}" data-clock>${fmtClock(laneElapsed(lane))}</div>
        <div class="lane-meta">
          <span>calls <b>${lane.calls || 0}</b></span>
          <span>tokens <b>${fmtTokens(tokens)}</b></span>
          <span>cost <b>${fmtCost(lane.cost)}</b></span>
        </div>
      </div>
      <div class="lane-bar ${status === "running" ? "running" : ""}"><span style="width:${progress}%"></span></div>
      <div class="lane-body">${laneBody(lane, race)}</div>
    </article>`;
}

function renderRace() {
  const race = state.currentRace;
  if (!race) {
    renderRaceHand();
    return;
  }
  renderRaceHand();
  const lanes = ["text", "decisions", "baseline"].map((kind) => race.lanes[kind]).filter(Boolean);
  $("#lanes").innerHTML = lanes.map((lane) => laneHtml(lane, race)).join("");
  renderCompare(race);
  renderEventLog(race);
  updateClocks();
}

function renderCompare(race) {
  const node = $("#compare");
  if (!isDone(race)) {
    node.classList.remove("show");
    node.innerHTML = "";
    return;
  }
  const lanes = ["text", "decisions", "baseline"].map((kind) => race.lanes[kind]).filter(Boolean);
  const max = Math.max(...lanes.map((lane) => lane.elapsed_ms || 0), 1);
  const winner = race.winner;
  let title = "No verified solution";
  if (winner === "text") title = "Text wins";
  else if (winner === "decisions") title = "Decisions wins";
  else if (winner === "tie") title = "Tie";
  const summary = race.summary || {};
  const textLane = race.lanes.text;
  const decisionsLane = race.lanes.decisions;
  const bothSolved = textLane.status === "solved" && decisionsLane.status === "solved";
  const loserStatus = winner === "text" ? decisionsLane.status : textLane.status;
  let sub = "";
  if (bothSolved && (winner === "text" || winner === "decisions")) {
    const faster = winner === "text" ? "Text" : "Decisions";
    const slower = winner === "text" ? "Decisions" : "Text";
    const winnerMs = winner === "text" ? summary.text_ms : summary.decisions_ms;
    const loserMs = winner === "text" ? summary.decisions_ms : summary.text_ms;
    sub = `${faster} finished ${fmtShort(Math.abs(loserMs - winnerMs))} before ${slower}.`;
    if (summary.speedup) {
      const ratio = winner === "decisions" ? summary.speedup : 1 / summary.speedup;
      if (ratio) sub += ` ${faster} is ${ratio.toFixed(2)}× faster on this hand.`;
    }
  } else if (winner === "text") {
    sub = `Text verified a solution; the decisions lane ${
      loserStatus === "error" ? "hit a provider error" : "did not verify one"
    }.`;
  } else if (winner === "decisions") {
    sub = `Decisions verified a solution; the text lane ${
      loserStatus === "error" ? "hit a provider error" : "did not verify one"
    }.`;
  } else if (winner === "tie") {
    sub = "Both lanes finished within 1 ms of each other.";
  } else {
    sub = "Neither lane produced a verified solution within the budget.";
  }

  const rows = lanes
    .map((lane) => {
      const ms = lane.elapsed_ms || 0;
      const width = Math.max(2, Math.min(100, (ms / max) * 100));
      return `<div class="compare-row" data-kind="${escapeHtml(lane.kind)}">
        <span class="label">${escapeHtml(lane.kind)}</span>
        <span class="bar"><span style="width:${width}%"></span></span>
        <span class="value">${lane.status === "solved" ? fmtShort(ms) : STATUS_LABEL[lane.status] || "—"}</span>
      </div>`;
    })
    .join("");

  node.innerHTML = `
    <div class="compare-head">
      <div>
        <div class="compare-title"><b>${escapeHtml(title)}</b></div>
        <div class="compare-sub">${escapeHtml(sub)}</div>
      </div>
      <div class="muted">${escapeHtml(fmtDate(race.finished_at))}</div>
    </div>
    <div class="compare-bars">${rows}</div>`;
  node.classList.add("show");
}

function renderEventLog(race) {
  const node = $("#event-log");
  const entries = [];
  for (const [kind, lane] of Object.entries(race.lanes)) {
    for (const item of lane.trace || []) {
      entries.push({ at: item.at, kind, message: item.message });
    }
  }
  for (const item of race.events || []) {
    entries.push({ at: item.at, kind: "race", message: item.message });
  }
  entries.sort((a, b) => String(a.at).localeCompare(String(b.at)));
  if (!entries.length) {
    node.innerHTML = `<p class="muted">Waiting for a race…</p>`;
    return;
  }
  node.innerHTML = entries
    .slice(-90)
    .map(
      (item) =>
        `<div class="event-line" data-kind="${escapeHtml(item.kind)}"><span class="time">${fmtTime(
          item.at
        )}</span><span class="who">${escapeHtml(item.kind)}</span><span class="msg">${escapeHtml(
          item.message
        )}</span></div>`
    )
    .join("");
  node.scrollTop = node.scrollHeight;
}

function updateClocks() {
  const race = state.currentRace;
  if (!race) return;
  const now = Date.now();
  for (const node of $$(".lane")) {
    const lane = race.lanes[node.dataset.kind];
    if (!lane) continue;
    const clock = node.querySelector("[data-clock]");
    if (clock) clock.textContent = fmtClock(laneElapsed(lane, now));
  }
  const clock = $("#race-clock");
  if (clock) {
    clock.textContent = fmtClock(raceElapsed(race, now));
    clock.classList.toggle("running", race.status === "running");
  }
}

function stopPolling() {
  state.polling = false;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function startPolling() {
  stopPolling();
  state.polling = true;
  const tick = async () => {
    if (!state.polling || !state.currentRace) return;
    try {
      const race = await api(`/api/races/${state.currentRace.id}`);
      state.currentRace = race;
      renderRace();
      if (isDone(race)) {
        state.polling = false;
        setRunningState(false);
        refreshHistory();
        return;
      }
    } catch (error) {
      showToast(error.message, true);
      state.polling = false;
      setRunningState(false);
      return;
    }
    state.pollTimer = setTimeout(tick, 300);
  };
  state.pollTimer = setTimeout(tick, 180);
}

function setRunningState(running) {
  $("#start").disabled = running;
  $("#cancel").disabled = !running;
  $("#deal-solvable").disabled = running;
  $("#podcast").disabled = running;
}

async function startRace() {
  if (state.hand.length < 2 || state.hand.length > 6) {
    showToast("A hand needs between 2 and 6 cards.", true);
    return;
  }
  const payload = {
    hand: state.hand,
    target: state.target,
    rules: rulesFromControls(),
    text_model: $("#text-model").value,
    decisions_model: $("#decisions-model").value,
    text_max_attempts: Number($("#attempts").value) || 2,
    max_expansions: Number($("#max-expansions").value) || 120,
    include_baseline: $("#baseline").checked,
    seed: Math.floor(Math.random() * 1_000_000),
  };
  try {
    setRunningState(true);
    const race = await api("/api/races", { method: "POST", body: JSON.stringify(payload) });
    state.currentRace = race;
    renderRace();
    startPolling();
  } catch (error) {
    setRunningState(false);
    showToast(error.message, true);
  }
}

async function cancelRace() {
  if (!state.currentRace) return;
  try {
    const data = await api(`/api/races/${state.currentRace.id}/cancel`, { method: "POST" });
    if (data.race) {
      state.currentRace = data.race;
      renderRace();
    }
    stopPolling();
    setRunningState(false);
    refreshHistory();
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadHistory() {
  try {
    const data = await api("/api/races?limit=14");
    state.history = data.races || [];
    renderHistory();
  } catch (error) {
    const node = $("#history");
    node.innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  }
}

function renderHistory() {
  const node = $("#history");
  if (!state.history.length) {
    node.innerHTML = `<p class="muted">No saved races yet.</p>`;
    return;
  }
  node.innerHTML = state.history
    .map((race) => {
      const config = race.config || {};
      const winner = race.winner || "none";
      const label = winner === "none" ? "no winner" : winner;
      return `<button class="history-item" data-race-id="${escapeHtml(race.id)}" style="width:100%;text-align:left">
        <span>
          <span class="history-hand">${escapeHtml((config.hand || []).join(" · ") || "—")} → ${escapeHtml(
        config.target ?? "—"
      )}</span>
          <span class="history-meta">${escapeHtml(fmtDate(race.started_at || race.created_at))} · ${escapeHtml(
        race.status || ""
      )}</span>
        </span>
        <span class="history-result ${escapeHtml(winner)}">${escapeHtml(label)}</span>
      </button>`;
    })
    .join("");
  node.querySelectorAll("[data-race-id]").forEach((button) => {
    button.addEventListener("click", () => openRace(button.dataset.raceId));
  });
}

async function openRace(id) {
  try {
    stopPolling();
    const race = await api(`/api/races/${id}`);
    state.currentRace = race;
    state.hand = (race.config?.hand || state.hand).slice();
    state.target = race.config?.target ?? state.target;
    $("#target").value = state.target;
    renderCards();
    renderRace();
    analyze();
    if (!isDone(race)) {
      setRunningState(true);
      startPolling();
    } else {
      setRunningState(false);
    }
  } catch (error) {
    showToast(error.message, true);
  }
}

async function dealHand(solvable = true) {
  try {
    $("#deal-solvable").disabled = true;
    const data = await api(`/api/hands/random?target=${state.target}&solvable=${solvable}`);
    state.hand = data.hand;
    renderCards();
    analyze();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    $("#deal-solvable").disabled = false;
  }
}

function podcastHand() {
  state.hand = [2, 8, 9, 3, 12, 12];
  state.target = 163;
  $("#target").value = 163;
  renderCards();
  analyze();
}

async function init() {
  renderCards();
  renderRaceHand();
  setInterval(updateClocks, 60);

  $("#deal-solvable").addEventListener("click", () => dealHand(true));
  $("#podcast").addEventListener("click", podcastHand);
  $("#start").addEventListener("click", startRace);
  $("#cancel").addEventListener("click", cancelRace);
  $("#refresh-history").addEventListener("click", loadHistory);
  $("#target").addEventListener("input", () => {
    state.target = Math.max(1, Math.round(Number($("#target").value) || 1));
    scheduleAnalyze();
  });
  for (const id of ["must-use-all", "integer-only"]) {
    $(`#${id}`).addEventListener("change", scheduleAnalyze);
  }

  try {
    const data = await api("/api/bootstrap");
    state.bootstrap = data;
    setDefaults(data);
    renderCards();
    populateSelects(data);
    renderKeys(data.keys || {});
    renderDataset(data);
    analyze();
    loadHistory();
    const raceId = new URLSearchParams(window.location.search).get("race");
    if (raceId) await openRace(raceId);
  } catch (error) {
    showToast(`Could not load bootstrap: ${error.message}`, true);
    analyze();
  }
}

init();
