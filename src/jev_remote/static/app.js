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
    if (result.state !== "working" && polling) { clearInterval(polling); polling = null; }
  } catch (error) {
    showStatus("error", error.message);
  }
}

async function sendCommand(text) {
  if (!token) { tokenDialog.showModal(); return; }
  showStatus("working", `Sending “${text}”…`);
  try {
    const result = await api("/api/command", { method: "POST", body: JSON.stringify({ text }) });
    showStatus(result.state, result.message);
    if (result.state === "working" && !polling) polling = setInterval(refreshStatus, 1000);
  } catch (error) {
    showStatus("error", error.message);
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
$("#token-form").addEventListener("submit", (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  token = $("#token").value.trim();
  localStorage.setItem("jevRemoteToken", token);
  tokenDialog.close();
  refreshStatus();
});

const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
if (Recognition) {
  const recognition = new Recognition();
  let listening = false;
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.lang = "en-US";
  const start = (event) => {
    event.preventDefault();
    if (listening) return;
    listening = true;
    speakButton.classList.add("listening");
    recognition.start();
  };
  const stop = (event) => {
    event.preventDefault();
    if (listening) recognition.stop();
  };
  speakButton.addEventListener("pointerdown", start);
  speakButton.addEventListener("pointerup", stop);
  speakButton.addEventListener("pointercancel", stop);
  recognition.addEventListener("result", (event) => sendCommand(event.results[0][0].transcript));
  recognition.addEventListener("end", () => { listening = false; speakButton.classList.remove("listening"); });
  recognition.addEventListener("error", (event) => {
    listening = false;
    speakButton.classList.remove("listening");
    showStatus("error", `Voice input: ${event.error}`);
  });
} else {
  speakButton.disabled = true;
  $("#voice-help").textContent = "Voice recognition is unavailable here; type a command below.";
}

if (!token) tokenDialog.showModal(); else refreshStatus();
