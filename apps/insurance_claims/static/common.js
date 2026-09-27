"use strict";

// Shared by the chat page (app.js) and the SOP console (console.js).

// The two pages talk over this channel: the chat page says when a turn starts or the
// conversation changes, and the console asks it to fill the message box or reload.
const CHANNEL_NAME = "sop-demo";
// Settings for the next conversation, chosen in the console and used by both pages.
const SETTINGS_KEY = "sop.settings";

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(method, path, body) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = (data && data.error) || {};
    throw new ApiError(
      response.status,
      error.code || "http_error",
      error.message || `Request failed (${response.status}).`,
    );
  }
  return data;
}

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function openChannel(onMessage) {
  if (!("BroadcastChannel" in window)) return { post() {} };
  const channel = new BroadcastChannel(CHANNEL_NAME);
  channel.addEventListener("message", (event) => onMessage(event.data || {}));
  return { post: (message) => channel.postMessage(message) };
}

// Storage can be unavailable (private windows, blocked site data); the pages work without it.
function readStored(key, fallback) {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : JSON.parse(value);
  } catch {
    return fallback;
  }
}

function writeStored(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Not remembered; nothing else depends on it.
  }
}

// Which model replies in this browser, and whose key it uses; the banner shows while no real one does.
async function checkModel(banner) {
  try {
    const model = await api("GET", "/api/model");
    banner.hidden = model.real_model;
    return model;
  } catch {
    return null;
  }
}
