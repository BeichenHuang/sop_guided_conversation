"use strict";

// The chat page: what a caller sees. The SOP console (console.js) watches the same
// conversation, either beside the chat or in its own tab.

const CONSOLE_KEY = "sop.console";

// Quick replies send an ordinary message, so they go through the same consent logic.
const QUICK_REPLIES = {
  email_consent: ["Yes, please send the summary.", "No thanks, skip the email."],
};

// Conversations that take no more messages, and what the caller is told.
const CLOSED = {
  ended: "This conversation has ended.",
  handed_off: "This conversation was transferred to a human representative (simulated).",
};

const $ = (id) => document.getElementById(id);
const els = {
  layout: $("layout"),
  progress: $("progress"),
  messages: $("messages"),
  composer: $("composer"),
  input: $("input"),
  send: $("send"),
  error: $("error"),
  quick: $("quick-replies"),
  ended: $("ended"),
  endedText: $("ended-text"),
  restart: $("restart"),
  hint: $("composer-hint"),
  newConversation: $("new-conversation"),
  consoleToggle: $("console-toggle"),
  banner: $("dev-banner"),
  openConsole: $("open-console"),
};

let pending = false;
// A message the console sent that is still waiting for its reply. It is shown again whenever the
// conversation is re-rendered, so a reload that finishes late can't wipe it out.
let remoteText = null;

const channel = openChannel((message) => {
  if (message.type === "fill") {
    els.input.value = message.text;
    fitInput();
    els.input.focus();
  } else if (message.type === "remote_turn_started") {
    // The console's scenario player sent a message: show it as if it were typed here.
    clearError();
    remoteText = message.text;
    appendMessage("user", message.text);
    setPending(true);
  } else if (message.type === "remote_turn_done") {
    remoteText = null;
    setPending(false);
    loadSession().catch((error) => showError(error.message));
  } else if (message.type === "model_changed") {
    checkModel(els.banner);
  } else if (message.type === "session_started") {
    clearError();
    remoteText = null;
    if (message.welcome && message.session) {
      // The console sends the new conversation along, so nothing has to be fetched first.
      renderMessages([{ role: "assistant", text: message.welcome }]);
      renderSession(message.session);
    } else {
      loadSession().catch((error) => showError(error.message));
    }
  }
});

const SVG_NS = "http://www.w3.org/2000/svg";

// The assistant's avatar: a claim form with a check mark, drawn in the text color of the accent.
function avatar() {
  const badge = node("span", undefined, "avatar");
  badge.setAttribute("aria-hidden", "true");
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  ["M7 3h7l4 4v13a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z", "M14 3v4h4", "M9.5 14l2 2 3.5-4"].forEach((d) => {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  });
  badge.append(svg);
  return badge;
}

// Agent replies are plain text: blank lines separate paragraphs, and lines starting with "- " form a list.
function formattedText(text) {
  const blocks = [];
  text
    .trim()
    .split(/\n\s*\n/)
    .forEach((paragraph) => {
      let lines = [];
      let list = null;
      const endLines = () => {
        if (lines.length) blocks.push(node("p", lines.join("\n")));
        lines = [];
      };
      const endList = () => {
        if (list) blocks.push(list);
        list = null;
      };
      paragraph.split("\n").forEach((line) => {
        const item = line.match(/^\s*[-•*]\s+(.*)$/);
        if (item) {
          endLines();
          list = list || node("ul");
          list.append(node("li", item[1]));
        } else {
          endList();
          lines.push(line);
        }
      });
      endLines();
      endList();
    });
  return blocks;
}

function messageRow(role) {
  const item = node("li", undefined, `message message--${role}`);
  if (role !== "user") item.append(avatar());
  const body = node("div", undefined, "message__body");
  body.append(node("span", role === "user" ? "You" : "Claims assistant", "message__role"));
  item.append(body);
  return [item, body];
}

function appendMessage(role, text) {
  const [item, body] = messageRow(role);
  const bubble = node("div", undefined, "message__text");
  if (role === "user") bubble.append(node("p", text));
  else bubble.append(...formattedText(text));
  body.append(bubble);
  els.messages.append(item);
  els.messages.scrollTop = els.messages.scrollHeight;
  return item;
}

function showTyping(visible) {
  document.getElementById("typing")?.remove();
  if (!visible) return;
  const [item, body] = messageRow("assistant");
  item.id = "typing";
  const bubble = node("div", undefined, "message__text typing");
  bubble.append(node("span", "Claims assistant is typing", "visually-hidden"));
  for (let i = 0; i < 3; i += 1) bubble.append(node("span", undefined, "typing__dot"));
  body.append(bubble);
  els.messages.append(item);
  els.messages.scrollTop = els.messages.scrollHeight;
}

function renderMessages(messages) {
  els.messages.replaceChildren();
  messages.forEach((message) => appendMessage(message.role, message.text));
}

function renderProgress(phase, finished) {
  const steps = [...els.progress.children];
  const current = finished ? steps.length : steps.findIndex((step) => step.dataset.phase === phase);
  steps.forEach((step, index) => {
    step.querySelector(".step-marker").textContent = index < current ? "✓" : String(index + 1);
    step.classList.toggle("is-done", index < current);
    step.classList.toggle("is-current", index === current);
    if (index === current) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });
}

function renderSession(view) {
  renderProgress(view.phase, view.status === "ended");
  const closed = CLOSED[view.status];
  els.ended.hidden = !closed;
  els.endedText.textContent = closed || "";
  els.composer.hidden = Boolean(closed);
  els.hint.hidden = Boolean(closed);
  const replies = (view.status === "active" && QUICK_REPLIES[view.awaiting]) || [];
  els.quick.replaceChildren(
    ...replies.map((text) => {
      const button = node("button", text, "chip");
      button.type = "button";
      button.addEventListener("click", () => {
        if (!pending) sendMessage(text);
      });
      return button;
    }),
  );
  els.quick.hidden = replies.length === 0;
}

function showError(message) {
  els.error.textContent = message;
  els.error.hidden = false;
}

function clearError() {
  els.error.hidden = true;
  els.error.textContent = "";
}

function setPending(value) {
  pending = value;
  els.send.disabled = value;
  els.newConversation.disabled = value;
  showTyping(value);
  els.quick.querySelectorAll("button").forEach((button) => {
    button.disabled = value;
  });
}

async function loadSession() {
  const data = await api("GET", "/api/session");
  renderMessages(data.messages);
  renderSession(data.session);
  if (remoteText !== null) {
    appendMessage("user", remoteText);
    showTyping(true);
  }
}

async function startConversation() {
  const data = await api("POST", "/api/sessions", readStored(SETTINGS_KEY, {}));
  renderMessages([{ role: "assistant", text: data.reply.text }]);
  renderSession(data.session);
  channel.post({ type: "changed" });
}

async function sendMessage(text) {
  clearError();
  const bubble = appendMessage("user", text);
  setPending(true);
  channel.post({ type: "turn_started" });
  try {
    const data = await api("POST", "/api/session/messages", { message: text });
    showTyping(false);
    appendMessage("assistant", data.reply.text);
    renderSession(data.session);
  } catch (error) {
    bubble.remove();
    els.input.value = text;
    fitInput();
    if (error.status === 401) {
      await startConversation();
      showError("Your previous conversation ended, so a new one was started. Please send your message again.");
    } else {
      showError(error.message);
    }
  } finally {
    setPending(false);
    channel.post({ type: "changed" });
    (els.composer.hidden ? els.restart : els.input).focus();
  }
}

function setConsole(open) {
  els.consoleToggle.checked = open;
  els.layout.classList.toggle("layout--chat", !open);
  els.layout.classList.toggle("layout--split", open);
  let frame = document.getElementById("console-frame");
  if (open && !frame) {
    frame = node("iframe", undefined, "console-frame");
    frame.id = "console-frame";
    frame.title = "SOP console";
    frame.src = "/console";
    els.layout.append(frame);
  } else if (!open && frame) {
    // Removed rather than hidden, so a closed console stops polling.
    frame.remove();
  }
}

els.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = els.input.value.trim();
  if (!text || pending) return;
  els.input.value = "";
  fitInput();
  sendMessage(text);
});

// The message box grows with its text, up to the height the stylesheet allows.
function fitInput() {
  els.input.style.height = "auto";
  els.input.style.height = `${els.input.scrollHeight}px`;
}

els.input.addEventListener("input", fitInput);

els.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    els.composer.requestSubmit();
  }
});

function restart() {
  clearError();
  startConversation()
    .then(() => els.input.focus())
    .catch((error) => showError(error.message));
}

els.newConversation.addEventListener("click", restart);
els.restart.addEventListener("click", restart);

els.consoleToggle.addEventListener("change", () => {
  writeStored(CONSOLE_KEY, els.consoleToggle.checked);
  setConsole(els.consoleToggle.checked);
});

els.openConsole.addEventListener("click", () => {
  writeStored(CONSOLE_KEY, true);
  setConsole(true);
});

async function init() {
  setConsole(readStored(CONSOLE_KEY, true));
  checkModel(els.banner);
  try {
    await loadSession();
  } catch (error) {
    if (error.status === 401) {
      await startConversation().catch((startError) => showError(startError.message));
    } else {
      showError(error.message);
    }
  }
  els.input.focus();
}

init();
