// EDP Billing Assistant — minimal chat client for POST /agent/run.
// Served same-origin (mounted by src/agent/__main__.py), so no CORS setup needed.

const API_URL = "/agent/run";
const STORAGE_KEY = "edpb_chat_conversation_id";
// Dev-only role simulator (see require_admin_role() in
// src/agent/edp/api/auth.py) -- a real deployment gets this from the CAMS
// gateway's JWT `role` claim instead; this <select> exists purely so this
// bundled chat UI can test admin-gated actions (upload/apply/delete a
// workflow version) without a real auth setup. Persists across reloads via
// localStorage, sent as X-User-Role on every /agent/run call.
const ROLE_STORAGE_KEY = "edpb_chat_role";

const DEFAULT_SUGGESTIONS = [
  "How is today's EDP processing going?",
  "Download the script with script name VN_09072026.txt",
  "What can you help me with?",
];

// Simple keyword-driven follow-up suggestions — re-evaluated after every
// exchange so the chips stay relevant to what the user is actually doing,
// instead of always showing the same generic starter questions.
const FOLLOWUP_RULES = [
  {
    test: /status|segment|processing|trade|workflow|upload|failed|pending|completed/i,
    suggestions: [
      "Show me the status for segment EQ",
      "Which segments failed today and why?",
      "Upload a new workflow config",
    ],
  },
  {
    test: /download|file|script/i,
    suggestions: [
      "Download another file",
      "How is today's EDP processing going?",
      "What can you help me with?",
    ],
  },
];

const BOT_AVATAR_SVG = `
  <svg viewBox="0 0 24 24" fill="none">
    <rect x="3" y="7" width="18" height="13" rx="4" stroke="currentColor" stroke-width="1.6"/>
    <path d="M12 7V4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="12" cy="3" r="1.2" fill="currentColor"/>
    <circle cx="8.5" cy="13.5" r="1.3" fill="currentColor"/>
    <circle cx="15.5" cy="13.5" r="1.3" fill="currentColor"/>
    <path d="M9 17.5h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
  </svg>`;

// Big clickable cards shown on the empty/welcome screen only. Clicking one
// sends that query immediately (see initSuggestionCards below).
const SUGGESTION_CARDS = [
  {
    icon: "icon-status",
    glyph: "●",
    title: "Today's status",
    desc: "See every segment and post-trade process at a glance",
    query: "How is today's EDP processing going?",
    featured: true,
  },
  {
    icon: "icon-download",
    glyph: "↓",
    title: "Download a script",
    desc: 'e.g. "get VN_09072026.txt"',
    query: "Download the script with script name VN_09072026.txt",
  },
  {
    icon: "icon-help",
    glyph: "?",
    title: "What can you do?",
    desc: "Tools, commands, and what to ask me",
    query: "What can you help me with?",
  },
];

const messagesEl = document.getElementById("messages");
const suggestionsEl = document.getElementById("suggestions");
const dayStatusBarEl = document.getElementById("dayStatusBar");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");
const roleSelectEl = document.getElementById("roleSelect");

let conversationId = sessionStorage.getItem(STORAGE_KEY) || null;
let isSending = false;
let currentSuggestions = DEFAULT_SUGGESTIONS;

const WELCOME_HTML = `
  <div class="welcome">
    <span class="welcome-eyebrow">Assistant</span>
    <h2>What do you need from today's run?</h2>
    <p>Ask about segment status, retry a failed step, upload a workflow config, or download a script — I'll route it to the right tool and confirm what happened.</p>
    <div class="suggestion-cards" id="suggestionCards"></div>
  </div>`;

function initSuggestionCards() {
  const container = document.getElementById("suggestionCards");
  if (!container) return;
  container.innerHTML = "";
  SUGGESTION_CARDS.forEach((card) => {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `suggestion-card${card.featured ? " featured" : ""}`;
    el.innerHTML = `
      <span class="suggestion-card-icon ${card.icon}">${card.glyph}</span>
      <span class="suggestion-card-title">${card.title}</span>
      <span class="suggestion-card-desc">${card.desc}</span>
    `;
    el.addEventListener("click", () => sendMessage(card.query));
    container.appendChild(el);
  });
}

function showWelcome() {
  messagesEl.innerHTML = WELCOME_HTML;
  initSuggestionCards();
}

// ---------- Live day-status strip (fetched directly from this agent's own
// EDP API — same-origin, see src/agent/edp/api/status.py) ----------

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

function formatDayLabel(isoDate) {
  const [, month, day] = isoDate.split("-").map(Number);
  return `${day} ${MONTHS[month - 1]}`;
}

function todayIso() {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).formatToParts(new Date());
  const get = (type) => parts.find((p) => p.type === type).value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function buildDayPill(className, count, label, suffix) {
  if (!count) return "";
  const suffixHtml = suffix ? ` · ${escapeHtml(suffix)}` : "";
  return `<span class="day-pill ${className}"><span class="dot"></span><span class="count">${count}</span> ${label}${suffixHtml}</span>`;
}

async function refreshDayStatus() {
  const dateIso = todayIso();
  try {
    const resp = await fetch(`/edp/status/${dateIso}`);
    if (!resp.ok) {
      dayStatusBarEl.classList.add("is-empty");
      return;
    }
    const data = await resp.json();
    const segments = data.segments || [];
    if (!segments.length) {
      dayStatusBarEl.classList.add("is-empty");
      return;
    }

    const failedSegs = segments.filter((s) => s.segment_status === "FAILED");
    const skippedSegs = segments.filter((s) => s.segment_status === "SKIPPED");

    const failedSuffix = failedSegs.length === 1 ? failedSegs[0].segment_code : null;
    const skippedSuffix =
      skippedSegs.length === 1 ? (skippedSegs[0].skip_category || skippedSegs[0].skip_reason || "").toLowerCase() : null;

    const html =
      `<div class="day-date"><span class="day-label">Today</span><span class="day-value">${formatDayLabel(dateIso)}</span></div>` +
      buildDayPill("completed", data.completed, "Completed") +
      buildDayPill("in-progress", data.in_progress, "In progress") +
      buildDayPill("blocked", data.pending, "Pending") +
      buildDayPill("failed", data.failed, "Failed", failedSuffix) +
      buildDayPill("skipped", data.skipped, "Skipped", skippedSuffix);

    dayStatusBarEl.innerHTML = html;
    dayStatusBarEl.classList.remove("is-empty");
  } catch (err) {
    dayStatusBarEl.classList.add("is-empty");
  }
}

function renderSuggestions() {
  suggestionsEl.innerHTML = "";
  currentSuggestions.forEach((question) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "suggestion-chip";
    chip.textContent = question;
    chip.addEventListener("click", () => {
      inputEl.value = question;
      inputEl.focus();
      autoResize();
    });
    suggestionsEl.appendChild(chip);
  });
}

function updateSuggestions(lastQuery, lastResponse) {
  const combined = `${lastQuery} ${lastResponse}`;
  const match = FOLLOWUP_RULES.find((rule) => rule.test.test(combined));
  currentSuggestions = match ? match.suggestions : DEFAULT_SUGGESTIONS;
  renderSuggestions();
}

function formatTime(date) {
  return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function clearWelcome() {
  const welcome = messagesEl.querySelector(".welcome");
  if (welcome) welcome.remove();
}

// ---------- Minimal markdown renderer (headings, bold, code, lists, tables) ----------
// Purpose-built for this agent's own tool outputs (see src/tools/edp_status.py) —
// not a general-purpose markdown engine, but covers everything the tools emit.

function escapeHtml(str) {
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function inlineFormat(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}

function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes("-");
}

function splitRow(line) {
  const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
  return trimmed.split("|").map((cell) => cell.trim());
}

function renderMarkdown(raw) {
  const lines = raw.replace(/\r\n/g, "\n").split("\n");
  let html = "";
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (line.trim() === "") {
      i++;
      continue;
    }

    const heading = line.match(/^#{1,4}\s+(.*)$/);
    if (heading) {
      html += `<h3>${inlineFormat(heading[1])}</h3>`;
      i++;
      continue;
    }

    if (line.trim().startsWith("|") && lines[i + 1] && isTableSeparator(lines[i + 1])) {
      const headerCells = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      html +=
        '<div class="bubble-table-wrap"><table><thead><tr>' +
        headerCells.map((c) => `<th>${inlineFormat(c)}</th>`).join("") +
        "</tr></thead><tbody>" +
        rows
          .map((r) => `<tr>${r.map((c) => `<td>${inlineFormat(c)}</td>`).join("")}</tr>`)
          .join("") +
        "</tbody></table></div>";
      continue;
    }

    if (/^[-*•]\s+/.test(line.trim())) {
      const items = [];
      while (i < lines.length && /^[-*•]\s+/.test(lines[i].trim())) {
        items.push(lines[i].trim().replace(/^[-*•]\s+/, ""));
        i++;
      }
      html += `<ul>${items.map((it) => `<li>${inlineFormat(it)}</li>`).join("")}</ul>`;
      continue;
    }

    const paraLines = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^#{1,4}\s+/.test(lines[i]) &&
      !lines[i].trim().startsWith("|") &&
      !/^[-*•]\s+/.test(lines[i].trim())
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    html += `<p>${paraLines.map(inlineFormat).join("<br>")}</p>`;
  }

  return html;
}

function appendMessage(role, text, { markdown = false } = {}) {
  clearWelcome();
  const row = document.createElement("div");
  row.className = `msg-row ${role}`;

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (role === "user") {
    avatar.innerHTML = '<span class="avatar-initials">You</span>';
  } else {
    avatar.innerHTML = BOT_AVATAR_SVG;
  }

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (markdown) {
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text;
  }

  const time = document.createElement("span");
  time.className = "msg-time";
  time.textContent = formatTime(new Date());

  const col = document.createElement("div");
  col.className = "bubble-col";
  col.appendChild(bubble);
  col.appendChild(time);

  if (role === "user") {
    row.appendChild(col);
    row.appendChild(avatar);
  } else {
    row.appendChild(avatar);
    row.appendChild(col);
  }

  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return bubble;
}

function appendTypingIndicator() {
  clearWelcome();
  const row = document.createElement("div");
  row.className = "msg-row bot";
  row.id = "typingRow";

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.innerHTML = BOT_AVATAR_SVG;

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';

  row.appendChild(avatar);
  row.appendChild(bubble);
  messagesEl.appendChild(row);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function removeTypingIndicator() {
  const row = document.getElementById("typingRow");
  if (row) row.remove();
}

function autoResize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
}

async function sendMessage(query) {
  if (isSending || !query.trim()) return;
  isSending = true;
  sendBtn.disabled = true;

  appendMessage("user", query);
  appendTypingIndicator();

  try {
    const headers = { "Content-Type": "application/json" };
    const role = roleSelectEl ? roleSelectEl.value : "";
    if (role) headers["X-User-Role"] = role;

    const response = await fetch(API_URL, {
      method: "POST",
      headers,
      body: JSON.stringify({
        query,
        conversation_id: conversationId,
      }),
    });

    const data = await response.json();
    removeTypingIndicator();

    if (data.error) {
      appendMessage("bot", `Something went wrong: ${data.error}`).classList.add("error");
      return;
    }

    if (data.conversation_id) {
      conversationId = data.conversation_id;
      sessionStorage.setItem(STORAGE_KEY, conversationId);
    }

    const responseText = data.response || "No response generated.";
    appendMessage("bot", responseText, { markdown: true });
    updateSuggestions(query, responseText);
    refreshDayStatus();
  } catch (err) {
    removeTypingIndicator();
    appendMessage(
      "bot",
      `Could not reach the agent (${err.message}). Is it running on this same host/port?`
    ).classList.add("error");
  } finally {
    isSending = false;
    sendBtn.disabled = false;
  }
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  const query = inputEl.value;
  inputEl.value = "";
  autoResize();
  sendMessage(query);
});

inputEl.addEventListener("input", autoResize);

inputEl.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    formEl.requestSubmit();
  }
});

newChatBtn.addEventListener("click", () => {
  conversationId = null;
  sessionStorage.removeItem(STORAGE_KEY);
  showWelcome();
  currentSuggestions = DEFAULT_SUGGESTIONS;
  renderSuggestions();
  refreshDayStatus();
});

if (roleSelectEl) {
  roleSelectEl.value = localStorage.getItem(ROLE_STORAGE_KEY) || "";
  roleSelectEl.addEventListener("change", () => {
    localStorage.setItem(ROLE_STORAGE_KEY, roleSelectEl.value);
  });
}

initSuggestionCards();
renderSuggestions();
refreshDayStatus();
