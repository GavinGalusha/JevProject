const $ = (selector) => document.querySelector(selector);
const tokenDialog = $("#token-dialog");
const speakButton = $("#speak");
let token = localStorage.getItem("jevRemoteToken") || "";
let polling = null;

function showStatus(state, message) {
  $("#status-dot").className = `status-dot ${state}`;
  $("#status-title").textContent = state === "working" ? "Working" : state === "error" ? "Needs attention" : state === "stopped" ? "Stopped & locked" : "Ready";
  $("#status-message").textContent = message;
  $("#kill").hidden = state === "stopped";
  $("#arm").hidden = state !== "stopped";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

async function refreshStatus() {
  try {
    const result = await api("/api/status");
    showStatus(result.state, result.message || "Ready");
    $("#save-command").hidden = !(result.state === "done" && lastJevCommand && !result.media);
    if (result.state !== "working" && polling) { clearInterval(polling); polling = null; }
  } catch (error) {
    showStatus("error", error.message);
  }
}

let lastJevCommand = null;

async function sendCommand(text, extra = {}) {
  if (!token) { tokenDialog.showModal(); return { ok: false, message: "Enter your access token first" }; }
  showStatus("working", `Sending “${text}”…`);
  try {
    const result = await api("/api/command", { method: "POST", body: JSON.stringify({ text, ...extra }) });
    lastJevCommand = { text, start_url: extra.start_url || "", fullscreen: Boolean(extra.fullscreen) };
    $("#save-command").hidden = true;
    showStatus(result.state, result.message);
    if (result.state === "working" && !polling) polling = setInterval(refreshStatus, 1000);
    document.querySelector(".status-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
    return { ok: true };
  } catch (error) {
    showStatus("error", error.message);
    return { ok: false, message: error.message };
  }
}

$("#command-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const input = $("#command");
  const text = input.value.trim();
  if (text) { sendCommand(text); input.value = ""; }
});

document.querySelectorAll("[data-command]").forEach((button) =>
  button.addEventListener("click", () => sendCommand(button.dataset.command))
);

$("#kill").addEventListener("click", async () => {
  if (!token || !confirm("Stop Jev, close its browser tabs, and lock the remote?")) return;
  try {
    const result = await api("/api/kill", { method: "POST" });
    if (polling) { clearInterval(polling); polling = null; }
    showStatus(result.state, result.message);
  } catch (error) { showStatus("error", error.message); }
});

$("#arm").addEventListener("click", async () => {
  try {
    const result = await api("/api/arm", { method: "POST" });
    showStatus(result.state, result.message);
  } catch (error) { showStatus("error", error.message); }
});

$("#settings").addEventListener("click", () => { $("#token").value = token; tokenDialog.showModal(); });
async function saveToken(value) {
  value = value.trim();
  if (value.length < 32) {
    // A short value is a pairing code; trade it for the real token.
    const response = await fetch("/api/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code: value }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `Pairing failed (${response.status})`);
    value = body.token;
  }
  token = value;
  localStorage.setItem("jevRemoteToken", token);
}

$("#token-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  try {
    await saveToken($("#token").value);
    tokenDialog.close();
    refreshStatus();
    loadSaved();
  } catch (error) {
    showStatus("error", error.message);
  }
});

const pairFromLink = new URLSearchParams(location.hash.slice(1)).get("pair");
if (pairFromLink) {
  history.replaceState(null, "", location.pathname);
  saveToken(pairFromLink).then(() => { tokenDialog.close(); refreshStatus(); loadSaved(); })
    .catch((error) => { tokenDialog.showModal(); showStatus("error", error.message); });
}

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  const recognition = new Recognition();
  const speakLabel = $("#speak-label");
  const help = $("#voice-help");
  const SILENCE_MS = 1200; // phone browsers can wait a long time to finalize, so stop after a short pause
  let listening = false;
  let heard = "";
  let sent = false;
  let silenceTimer = null;
  let resetTimer = null;
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = "en-US";
  const setButton = (state) => {
    speakButton.classList.toggle("listening", state === "listening");
    speakButton.setAttribute("aria-pressed", String(state === "listening"));
    speakLabel.textContent = {
      idle: "Tap to speak",
      listening: "Listening… tap to send",
      sending: "Sending…",
      sent: "Sent ✓ — tap to speak again",
    }[state];
  };
  const submitHeard = async () => {
    clearTimeout(silenceTimer);
    const text = heard.trim();
    if (sent || !text) return;
    sent = true;
    setButton("sending");
    help.textContent = `Sending: “${text}”`;
    const { ok, message } = await sendCommand(text);
    help.textContent = ok ? `Sent: “${text}”` : `“${text}” didn't run: ${message}`;
    setButton(ok ? "sent" : "idle");
    clearTimeout(resetTimer);
    if (ok) resetTimer = setTimeout(() => { if (!listening) setButton("idle"); }, 4000);
  };
  // Tap once to start; it sends after you stop talking, or tap again to send early.
  speakButton.addEventListener("click", () => {
    if (listening) { recognition.stop(); return; }
    clearTimeout(resetTimer);
    heard = "";
    sent = false;
    help.textContent = "Listening… start talking";
    try { recognition.start(); listening = true; setButton("listening"); } catch (error) { showStatus("error", `Voice input: ${error.message}`); }
  });
  recognition.addEventListener("result", (event) => {
    heard = Array.from(event.results).map((r) => r[0].transcript).join(" ");
    help.textContent = `Hearing: “${heard}”`;
    clearTimeout(silenceTimer);
    if (Array.from(event.results).every((r) => r.isFinal)) submitHeard();
    else silenceTimer = setTimeout(() => recognition.stop(), SILENCE_MS);
  });
  recognition.addEventListener("end", () => {
    listening = false;
    clearTimeout(silenceTimer);
    if (heard.trim() && !sent) { submitHeard(); return; }
    if (!sent) { setButton("idle"); help.textContent = "Didn't catch anything. Tap and try again, or type below."; }
  });
  recognition.addEventListener("error", (event) => {
    listening = false;
    clearTimeout(silenceTimer);
    if (event.error === "no-speech" || event.error === "aborted") return;
    setButton("idle");
    const blocked = "Microphone blocked. Allow it for this site in your browser settings, then reload.";
    const reasons = {
      "not-allowed": blocked,
      "service-not-allowed": blocked,
      "network": "Speech service unreachable. Check the phone's internet connection.",
      "audio-capture": "No microphone found.",
    };
    const message = reasons[event.error] || `Voice input: ${event.error}`;
    help.textContent = message;
    showStatus("error", message);
  });
} else {
  const unavailable = "Voice input isn't available in this browser. Type a command below.";
  $("#voice-help").textContent = unavailable;
  speakButton.addEventListener("click", () => { showStatus("error", unavailable); $("#command").focus(); });
}

if (pairFromLink) { /* pairing in progress */ } else if (!token) tokenDialog.showModal(); else refreshStatus();


// ----- Saved commands -----
async function loadSaved() {
  if (!token) return;
  try {
    const items = await api("/api/saved");
    const list = $("#saved-list");
    list.replaceChildren();
    $("#saved-section").hidden = items.length === 0;
    for (const item of items) {
      const row = document.createElement("div");
      row.className = "saved-item";
      const run = document.createElement("button");
      run.className = "saved-run";
      const title = document.createElement("strong");
      title.textContent = item.name;
      const detail = document.createElement("small");
      detail.textContent = item.text;
      run.append(title, detail);
      run.addEventListener("click", () => runSaved(item));
      const del = document.createElement("button");
      del.className = "saved-delete";
      del.setAttribute("aria-label", `Delete ${item.name}`);
      del.textContent = "×";
      del.addEventListener("click", async () => {
        if (!confirm(`Delete saved command “${item.name}”?`)) return;
        try { await api(`/api/saved/${item.id}`, { method: "DELETE" }); loadSaved(); }
        catch (error) { showStatus("error", error.message); }
      });
      row.append(run, del);
      list.append(row);
    }
  } catch (error) { showStatus("error", error.message); }
}

function runSaved(item) {
  let text = item.text;
  for (const name of new Set([...text.matchAll(/\{([^{}]{1,40})\}/g)].map((m) => m[1]))) {
    const answer = prompt(`${item.name}: enter ${name}`);
    if (answer === null || !answer.trim()) return;
    text = text.split(`{${name}}`).join(answer.trim());
  }
  sendCommand(text, { start_url: item.start_url || undefined, fullscreen: item.fullscreen });
}

$("#save-command").addEventListener("click", () => {
  if (!lastJevCommand) return;
  $("#save-name").value = "";
  $("#save-text").value = lastJevCommand.text;
  $("#save-url").value = lastJevCommand.start_url;
  $("#save-fullscreen").checked = lastJevCommand.fullscreen;
  $("#save-dialog").showModal();
});

$("#save-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  try {
    await api("/api/saved", {
      method: "POST",
      body: JSON.stringify({
        name: $("#save-name").value,
        text: $("#save-text").value,
        start_url: $("#save-url").value.trim() || null,
        fullscreen: $("#save-fullscreen").checked,
      }),
    });
    $("#save-dialog").close();
    $("#save-command").hidden = true;
    loadSaved();
  } catch (error) { showStatus("error", error.message); }
});

loadSaved();
