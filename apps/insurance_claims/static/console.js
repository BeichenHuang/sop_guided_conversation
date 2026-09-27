"use strict";

// The SOP console: watches the conversation in this browser (the same session cookie as the
// chat page) and holds the demo controls. It reads the session without counting as activity.

const POLL_MS = 4000;

const PHASES = [
  ["VERIFY_ID", "Verify identity"],
  ["RESOLVE_INTENT", "Resolve intent"],
  ["PROCESS_CASE", "Process case"],
  ["POST_PROCESS", "Post-process"],
];
const PHASE_LABELS = Object.fromEntries(PHASES);
const GATE_STATES = { passed: "Passed", waiting: "Waiting", blocked: "Blocked", not_needed: "Not needed" };

// Pause between played steps, so each reply can be read before the next message.
const STEP_PAUSE_MS = 1500;
const CONSENT_LABELS = { default: "approves", timeout: "times out" };
const DATE_LABELS = { demo: "demo date", real: "real date" };
// What the agent's last question asked the caller, as the Workflow card says it.
const AWAITING_LABELS = {
  id_type: "which ID the last 4 digits belong to",
  dob_format: "which date of birth they meant",
  representative: "who they are calling for",
  choose_case: "which claim",
  confirm_case: "whether this is the right claim",
  anything_else: "whether there's anything else",
  email_consent: "whether to send the email summary",
};
const STATUS_LABELS = {
  active: "Active",
  ended: "Ended",
  handed_off: "Handed to a person",
  cancelled: "Caller left",
};
const TAB_KEY = "sop.consoleTab";
// From this width, Memory sits beside the tabs instead of being a tab itself.
const WIDE = window.matchMedia("(min-width: 1100px)");

const $ = (id) => document.getElementById(id);
const els = {
  settings: $("settings"),
  settingsSummary: $("settings-summary"),
  newConversation: $("new-conversation"),
  dateMode: $("date-mode"),
  consent: $("consent-scenario"),
  dateBadge: $("date-badge"),
  modelBadge: $("model-badge"),
  banner: $("dev-banner"),
  noSession: $("no-session"),
  subflow: $("subflow"),
  sessionStatus: $("session-status"),
  statusMeta: $("status-meta"),
  countTurns: $("count-turns"),
  countOutbox: $("count-outbox"),
  modelCard: $("model-card"),
  modelStatus: $("model-status"),
  keyForm: $("key-form"),
  keyProvider: $("key-provider"),
  keyModel: $("key-model"),
  keyModels: $("key-models"),
  apiKey: $("api-key"),
  keySave: $("key-save"),
  keyRemove: $("key-remove"),
  keyError: $("key-error"),
  error: $("error"),
  scenarios: $("scenarios"),
  scenario: $("scenario"),
  scenarioDescription: $("scenario-description"),
  scenarioPlay: $("scenario-play"),
  scenarioNext: $("scenario-next"),
  scenarioRestart: $("scenario-restart"),
  scenarioStatus: $("scenario-status"),
  scenarioSteps: $("scenario-steps"),
  phases: $("phases"),
  gates: $("gates"),
  turns: $("turns"),
  turnPending: $("turn-pending"),
  memoryCaller: $("memory-caller"),
  memoryRecords: $("memory-records"),
  outbox: $("outbox"),
  outboxEmpty: $("outbox-empty"),
};

let lastSnapshot = "";
let latestTurn = null;
let settingsChosen = false;
let hasOwnKey = false;
let providers = [];

// The scenario player: complete conversations from scenarios.json, sent step by step.
const player = {
  scenarios: [],
  current: null,
  // Steps of the current scenario already sent in this conversation.
  sent: 0,
  // A step put in the chat's message box to edit; it counts as sent once the chat sends it.
  filled: null,
  playing: false,
  busy: false,
};

const channel = openChannel((message) => {
  if (message.type === "turn_started") {
    els.turnPending.hidden = false;
    if (player.filled !== null) {
      player.sent = player.filled + 1;
      player.filled = null;
      renderScenario();
    }
  } else if (message.type === "changed") {
    // A failed turn changes nothing, so clear the notice here rather than when the data changes.
    els.turnPending.hidden = true;
    refresh();
  }
});

function definitions(target, rows) {
  target.replaceChildren(...rows.flatMap(([term, value]) => [node("dt", term), node("dd", value)]));
}

function renderSession(view) {
  // An ended conversation has finished every phase.
  const current =
    view.status === "ended" ? PHASES.length : PHASES.findIndex(([phase]) => phase === view.phase);
  els.phases.replaceChildren(
    ...PHASES.map(([, label], index) => {
      const item = node("li", undefined, "phase");
      const text = node("span", label, "phase__label");
      text.title = label;
      item.append(node("span", index < current ? "✓" : String(index + 1), "step-marker"), text);
      if (index < current) item.classList.add("phase--done");
      if (index === current) {
        item.classList.add("phase--current");
        item.setAttribute("aria-current", "step");
      }
      return item;
    }),
  );
  // A step inside the phase, such as a consent request, gets its own line so the bar stays short.
  els.subflow.hidden = !view.subflow || view.status === "ended";
  els.subflow.textContent = view.subflow ? `Current step: ${view.subflow}` : "";
  els.sessionStatus.textContent = STATUS_LABELS[view.status] || view.status;
  els.sessionStatus.className = `pill pill--${view.status}`;
  // The first claim of a conversation is cycle 1; a later one hides the earlier claims from the model.
  const meta = [];
  if (view.awaiting && view.status === "active") {
    meta.push(`Waiting for the caller: ${AWAITING_LABELS[view.awaiting] || view.awaiting.replaceAll("_", " ")}`);
  }
  if (view.case_cycle_id > 1) meta.push(`Claim cycle ${view.case_cycle_id}`);
  els.statusMeta.textContent = meta.join(" · ");
  els.statusMeta.hidden = meta.length === 0;

  els.gates.replaceChildren(
    ...view.gates.map((gate) => {
      const item = node("li", undefined, `gate gate--${gate.state}`);
      item.append(
        node("span", gate.name, "gate__name"),
        node("span", GATE_STATES[gate.state] || gate.state, "gate__state"),
        node("span", gate.detail, "gate__detail"),
      );
      return item;
    }),
  );

  const told = view.memory.filter((item) => item.source === "caller");
  const records = view.memory.filter((item) => item.source === "records");
  definitions(els.memoryCaller, told.length ? told.map((i) => [i.label, i.value]) : [["–", "Nothing yet"]]);
  definitions(
    els.memoryRecords,
    records.length ? records.map((i) => [i.label, i.value]) : [["–", view.verified ? "Nothing yet" : "Locked"]],
  );

  els.dateBadge.textContent =
    view.date_mode === "demo" ? `Demo date: ${view.as_of_date}` : `Date: ${view.as_of_date}`;
  // The selectors hold the settings for the next conversation, so polling never overwrites a choice.
  if (!settingsChosen) {
    els.dateMode.value = view.date_mode;
    els.consent.value = view.consent_scenario;
    summarizeSettings();
  }
}

// Folded, the Demo settings card still says what the next conversation will use.
function summarizeSettings() {
  const date = DATE_LABELS[els.dateMode.value] || els.dateMode.value;
  const consent = CONSENT_LABELS[els.consent.value] || els.consent.value;
  els.settingsSummary.textContent = `Next conversation: ${date}, policyholder consent ${consent}`;
}

function planText(plan) {
  if (!plan) return "No plan recorded.";
  const shown = Object.fromEntries(
    Object.entries(plan).filter(([, value]) => value !== null && !(Array.isArray(value) && value.length === 0)),
  );
  return JSON.stringify(shown, null, 2);
}

function said(label, message, who) {
  const block = node("div", undefined, `turn__said turn__said--${who}`);
  block.append(node("span", label, "turn__label"), node("p", message.text, "turn__text"));
  return block;
}

function turnCard(turn, open) {
  const card = node("details", undefined, "turn");
  card.dataset.turn = String(turn.turn);
  card.open = open;

  const built = turn.trace.find((event) => event.event === "plan_built" || event.event === "session_opened");
  const summary = node("summary");
  summary.append(node("span", turn.turn === 0 ? "Welcome" : `Turn ${turn.turn}`, "turn__num"));
  if (built && built.rule) summary.append(node("span", built.rule, "turn__rule"));
  if (turn.trace.some((event) => event.event === "reply_realized" && event.status === "fallback")) {
    summary.append(node("span", "template fallback", "tag tag--warn"));
  }
  if (turn.trace.some((event) => event.event === "invariant_violated")) {
    summary.append(node("span", "invariant", "tag tag--error"));
  }
  if (built && built.phase) summary.append(node("span", PHASE_LABELS[built.phase] || built.phase, "tag"));
  card.append(summary);

  const body = node("div", undefined, "turn__body");
  if (turn.caller) body.append(said("Caller", turn.caller, "caller"));
  if (turn.reply) body.append(said("Agent", turn.reply, "agent"));
  body.append(node("h3", "Reply plan"), node("pre", planText(turn.plan), "code"));
  const trace = node("ol", undefined, "trace");
  trace.replaceChildren(
    ...turn.trace.map((event) => {
      const item = node("li", event.event);
      [event.rule, event.tool, event.status].filter(Boolean).forEach((detail) => {
        item.append(node("span", detail, "trace__meta"));
      });
      return item;
    }),
  );
  body.append(node("h3", "Trace"), trace);
  card.append(body);
  return card;
}

function renderTurns(turns) {
  // The welcome is turn 0; the count is of the caller's turns.
  setCount(els.countTurns, turns.length - 1);
  const newest = turns.length ? turns[turns.length - 1].turn : null;
  const arrived = newest !== latestTurn;
  const open = new Set([...els.turns.querySelectorAll("details[open]")].map((card) => card.dataset.turn));
  els.turns.replaceChildren(
    ...turns
      .slice()
      .reverse()
      .map((turn) => {
        const item = node("li");
        // A new turn opens itself; otherwise cards keep the state you left them in.
        item.append(turnCard(turn, arrived ? turn.turn === newest : open.has(String(turn.turn))));
        return item;
      }),
  );
  latestTurn = newest;
}

function renderModel(model) {
  if (!model) {
    els.modelStatus.textContent = "The model couldn't be checked.";
    els.modelBadge.textContent = "Model: unavailable";
    return;
  }
  if (!providers.length && model.providers) {
    providers = model.providers;
    els.keyProvider.replaceChildren(...providers.map((p) => new Option(p.label, p.id)));
    els.keyProvider.value = model.provider;
  }
  els.modelStatus.textContent = {
    yours: `Using your ${model.provider_label} key ${model.key_hint} with ${model.model}.`,
    server: `Using the server's ${model.provider_label} key with ${model.model}. Enter your own key to use it instead.`,
    none: "No key yet, so replies come from a stand-in that doesn't read messages. Choose a provider and enter its API key.",
  }[model.source];
  hasOwnKey = model.source === "yours";
  els.keyRemove.hidden = !hasOwnKey;
  labelKeyForm();
  els.modelBadge.textContent = model.real_model ? `Model: ${model.model}` : "Model: stand-in (no key)";
  els.modelBadge.classList.toggle("badge--warn", !model.real_model);
  els.banner.hidden = model.real_model;
  els.modelCard.classList.toggle("model-card--warn", !model.real_model);
}

function labelKeyForm() {
  const provider = providers.find((p) => p.id === els.keyProvider.value);
  els.keySave.textContent = hasOwnKey ? "Replace key" : "Use this key";
  els.apiKey.placeholder = hasOwnKey
    ? "Paste a different key to replace yours"
    : `Paste your ${provider ? provider.label : ""} API key`;
  // Empty means the provider's default model, which the placeholder names.
  els.keyModel.placeholder = provider ? `${provider.default_model} (default)` : "";
  // Suggestions only: the field still takes any model name the provider offers.
  els.keyModels.replaceChildren(
    ...(provider ? provider.models : []).map(
      (name) => new Option(name === provider.default_model ? "default" : "", name),
    ),
  );
}

function showKeyError(message) {
  els.keyError.textContent = message;
  els.keyError.hidden = !message;
}

async function changeKey(request) {
  showKeyError("");
  els.keySave.disabled = true;
  els.keyRemove.disabled = true;
  try {
    renderModel(await request());
    channel.post({ type: "model_changed" });
  } catch (error) {
    showKeyError(error.message);
  } finally {
    els.keySave.disabled = false;
    els.keyRemove.disabled = false;
    labelKeyForm();
  }
}

function renderOutbox(emails) {
  els.outbox.replaceChildren(
    ...emails.map((email) => {
      const item = node("li", undefined, "email");
      item.append(
        node("strong", email.subject),
        node("span", ` to ${email.to}, ${email.status.replaceAll("_", " ")}`, "email__to"),
      );
      const details = node("details");
      details.append(node("summary", "Show email"), node("pre", email.body, "code"));
      item.append(details);
      return item;
    }),
  );
  els.outboxEmpty.hidden = emails.length > 0;
  setCount(els.countOutbox, emails.length);
}

function setCount(element, count) {
  element.textContent = count > 0 ? String(count) : "";
  element.hidden = count <= 0;
}

// --- tabs ------------------------------------------------------------------------------

const tabs = [...document.querySelectorAll('[role="tab"]')];

function visibleTabs() {
  return tabs.filter((tab) => !(WIDE.matches && tab.classList.contains("tab--aside")));
}

function selectTab(name, { focus = false } = {}) {
  const shown = visibleTabs();
  const chosen = shown.find((tab) => tab.dataset.tab === name) || shown[0];
  tabs.forEach((tab) => {
    const selected = tab === chosen;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !selected;
  });
  if (focus) chosen.focus();
  writeStored(TAB_KEY, chosen.dataset.tab);
}

tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
  tab.addEventListener("keydown", (event) => {
    const shown = visibleTabs();
    const index = shown.indexOf(tab);
    const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: shown.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = shown[(moves[event.key] + shown.length) % shown.length];
    selectTab(next.dataset.tab, { focus: true });
  });
});

WIDE.addEventListener("change", () => selectTab(readStored(TAB_KEY, "turns")));

function showError(message) {
  els.error.textContent = message;
  els.error.hidden = false;
}

function clearError() {
  els.error.hidden = true;
  els.error.textContent = "";
}

async function refresh() {
  try {
    const data = await api("GET", "/api/session/console");
    els.noSession.hidden = true;
    clearError();
    const snapshot = JSON.stringify(data);
    if (snapshot === lastSnapshot) return;
    lastSnapshot = snapshot;
    els.turnPending.hidden = true;
    // A new conversation started elsewhere begins the scenario again.
    if (!player.busy && data.turns.length === 1 && player.sent > 0) {
      player.sent = 0;
      renderScenario();
    }
    renderSession(data.session);
    renderTurns(data.turns);
    renderOutbox(data.outbox.emails);
  } catch (error) {
    if (error.status === 401) {
      els.noSession.hidden = false;
      lastSnapshot = "";
    } else {
      showError(error.message);
    }
  }
}

async function startConversation() {
  const settings = { date_mode: els.dateMode.value, consent_scenario: els.consent.value };
  writeStored(SETTINGS_KEY, settings);
  announceNewConversation(await api("POST", "/api/sessions", settings));
  await refresh();
}

// The chat page shows the new conversation from this message, without fetching it first.
function announceNewConversation(created) {
  channel.post({ type: "session_started", welcome: created.reply.text, session: created.session });
}

function renderScenarios() {
  els.scenarios.replaceChildren(
    ...player.scenarios.map((scenario) => {
      const button = node("button", scenario.title, "chip chip--ghost");
      button.type = "button";
      button.setAttribute("aria-pressed", String(player.current === scenario));
      button.addEventListener("click", () => selectScenario(scenario));
      return button;
    }),
  );
}

function selectScenario(scenario) {
  if (player.busy) return;
  // Choosing the open scenario again closes it.
  player.current = player.current === scenario ? null : scenario;
  player.sent = 0;
  player.filled = null;
  setScenarioStatus("");
  renderScenarios();
  renderScenario();
}

function renderScenario() {
  const scenario = player.current;
  els.scenario.hidden = !scenario;
  if (!scenario) return;
  const consent = CONSENT_LABELS[scenario.consent] || scenario.consent;
  els.scenarioDescription.textContent = `${scenario.description} Starts a new conversation (policyholder consent: ${consent}).`;
  els.scenarioSteps.replaceChildren(
    ...scenario.steps.map((step, index) => {
      const item = node("li", undefined, "step");
      item.classList.toggle("step--sent", index < player.sent);
      item.classList.toggle("step--next", index === player.sent);
      item.append(node("p", step.say, "step__say"), node("p", step.expect, "step__expect"));
      const edit = node("button", "Edit in chat", "link-button step__edit");
      edit.type = "button";
      edit.disabled = player.busy;
      edit.addEventListener("click", () => {
        player.filled = index;
        channel.post({ type: "fill", text: step.say });
        setScenarioStatus("The step is in the chat's message box; edit it and send it there.");
      });
      item.append(edit);
      return item;
    }),
  );
  // Keep the next step in view inside the list, without moving the page.
  const next = els.scenarioSteps.querySelector(".step--next");
  if (next && player.busy) els.scenarioSteps.scrollTop = next.offsetTop - 8;
  const done = player.sent >= scenario.steps.length;
  els.scenarioPlay.textContent = player.playing ? "Stop" : "Play the conversation";
  els.scenarioPlay.disabled = player.busy && !player.playing;
  els.scenarioNext.disabled = player.busy || done;
  els.scenarioRestart.disabled = player.busy;
}

function setScenarioStatus(text) {
  els.scenarioStatus.textContent = text;
}

async function startScenario() {
  const settings = { date_mode: els.dateMode.value, consent_scenario: player.current.consent };
  const created = await api("POST", "/api/sessions", settings);
  player.sent = 0;
  player.filled = null;
  announceNewConversation(created);
  await refresh();
}

// Sends the next step through the API; the chat page shows it as if it had been typed there.
async function sendNextStep() {
  const step = player.current.steps[player.sent];
  channel.post({ type: "remote_turn_started", text: step.say });
  els.turnPending.hidden = false;
  try {
    const data = await api("POST", "/api/session/messages", { message: step.say });
    player.sent += 1;
    return data.session;
  } finally {
    channel.post({ type: "remote_turn_done" });
    await refresh();
  }
}

async function runSteps(count) {
  const scenario = player.current;
  player.busy = true;
  renderScenario();
  try {
    for (let n = 0; n < count && player.sent < scenario.steps.length; n += 1) {
      if (n > 0) await new Promise((resolve) => setTimeout(resolve, STEP_PAUSE_MS));
      if (count > 1 && !player.playing) break;
      setScenarioStatus(`Step ${player.sent + 1} of ${scenario.steps.length}: waiting for the reply…`);
      const session = await sendNextStep();
      renderScenario();
      const left = scenario.steps.length - player.sent;
      if (left && (session.status === "ended" || session.status === "handed_off")) {
        setScenarioStatus(`The conversation ${session.status === "ended" ? "ended" : "was handed off"} with ${left} step(s) left.`);
        return;
      }
    }
    setScenarioStatus(
      player.sent >= scenario.steps.length
        ? "All steps sent. Compare each reply with what to look for."
        : `${player.sent} of ${scenario.steps.length} steps sent.`,
    );
  } catch (error) {
    setScenarioStatus(`Stopped: ${error.message}`);
  } finally {
    player.busy = false;
    player.playing = false;
    renderScenario();
  }
}

async function playScenario() {
  if (player.playing) {
    // Stops after the reply that is on its way.
    player.playing = false;
    setScenarioStatus("Stopping after this reply…");
    return;
  }
  player.playing = true;
  try {
    await startScenario();
  } catch (error) {
    player.playing = false;
    setScenarioStatus(`Couldn't start: ${error.message}`);
    return;
  }
  await runSteps(player.current.steps.length);
}

async function sendOneStep() {
  if (player.sent === 0) {
    try {
      await startScenario();
    } catch (error) {
      setScenarioStatus(`Couldn't start: ${error.message}`);
      return;
    }
  }
  await runSteps(1);
}

async function restartScenario() {
  try {
    await startScenario();
    setScenarioStatus("A new conversation is ready for step 1.");
  } catch (error) {
    setScenarioStatus(`Couldn't start: ${error.message}`);
  }
  renderScenario();
}

async function loadScenarios() {
  try {
    player.scenarios = await api("GET", "/static/scenarios.json");
  } catch {
    player.scenarios = [];
  }
  renderScenarios();
}

function rememberSettings() {
  settingsChosen = true;
  writeStored(SETTINGS_KEY, { date_mode: els.dateMode.value, consent_scenario: els.consent.value });
  summarizeSettings();
}

els.keyForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const key = els.apiKey.value.trim();
  if (!key) return;
  // The key leaves the page once it is sent; only its last characters come back.
  els.apiKey.value = "";
  els.keySave.textContent = "Checking…";
  const body = { provider: els.keyProvider.value, api_key: key };
  const model = els.keyModel.value.trim();
  if (model) body.model = model;
  changeKey(() => api("PUT", "/api/model/key", body));
});

els.keyProvider.addEventListener("change", () => {
  els.keyModel.value = "";
  labelKeyForm();
});

els.keyRemove.addEventListener("click", () => changeKey(() => api("DELETE", "/api/model/key")));

els.scenarioPlay.addEventListener("click", playScenario);
els.scenarioNext.addEventListener("click", sendOneStep);
els.scenarioRestart.addEventListener("click", restartScenario);

els.dateMode.addEventListener("change", rememberSettings);
els.consent.addEventListener("change", rememberSettings);

function newConversation() {
  clearError();
  startConversation().catch((error) => showError(error.message));
}

els.settings.addEventListener("submit", (event) => {
  event.preventDefault();
  newConversation();
});
els.newConversation.addEventListener("click", newConversation);

async function init() {
  document.body.classList.toggle("console-page--embedded", window.self !== window.top);
  selectTab(readStored(TAB_KEY, "turns"));
  const saved = readStored(SETTINGS_KEY, null);
  if (saved) {
    els.dateMode.value = saved.date_mode || els.dateMode.value;
    els.consent.value = saved.consent_scenario || els.consent.value;
    settingsChosen = true;
  }
  summarizeSettings();
  loadScenarios();
  checkModel(els.banner).then(renderModel);
  await refresh();
  setInterval(() => {
    if (!document.hidden) refresh();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
}

init();
