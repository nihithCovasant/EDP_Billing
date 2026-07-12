// EDP Billing Assistant — minimal chat client for POST /agent/run.
// Served same-origin (mounted by src/agent/__main__.py), so no CORS setup needed.

const API_URL = "/agent/run";
const STORAGE_KEY = "edpb_chat_conversation_id";

const SUGGESTED_QUESTIONS = [
  "How is today's EDP processing going?",
  "Download the script with script name VN_09072026.txt",
  "Calculate 15% GST on 24500",
  "What can you help me with?",
];

const messagesEl = document.getElementById("messages");
const suggestionsEl = document.getElementById("suggestions");
const formEl = document.getElementById("chatForm");
const inputEl = document.getElementById("chatInput");
const sendBtn = document.getElementById("sendBtn");
const newChatBtn = document.getElementById("newChatBtn");

let conversationId = sessionStorage.getItem(STORAGE_KEY) || null;
let isSending = false;

const WELCOME_HTML = `
  <div class="welcome">
    <div class="welcome-icon">
      <svg width="30" height="30" viewBox="0 0 24 24" fill="none">
        <path d="M12 3C7.03 3 3 6.58 3 11c0 2.39 1.19 4.53 3.08 6.02L5 21l4.29-1.53C10.14 19.82 11.05 20 12 20c4.97 0 9-3.58 9-9s-4.03-8-9-8Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>
        <circle cx="8.5" cy="11" r="1.1" fill="currentColor"/>
        <circle cx="12" cy="11" r="1.1" fill="currentColor"/>
        <circle cx="15.5" cy="11" r="1.1" fill="currentColor"/>
      </svg>
    </div>
    <h2>How can I help you today?</h2>
    <p>Ask me to download a file, check EDP segment status, upload a workflow config, run a calculation, or answer a question — I'll pick the right tool automatically.</p>
  </div>`;

function renderSuggestions() {
  suggestionsEl.innerHTML = "";
  SUGGESTED_QUESTIONS.forEach((question) => {
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
  avatar.textContent = role === "user" ? "You" : "AI";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (markdown) {
    bubble.innerHTML = renderMarkdown(text);
  } else {
    bubble.textContent = text;
  }

  if (role === "user") {
    row.appendChild(bubble);
    row.appendChild(avatar);
  } else {
    row.appendChild(avatar);
    row.appendChild(bubble);
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
  avatar.textContent = "AI";

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
    const response = await fetch(API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

    appendMessage("bot", data.response || "No response generated.", { markdown: true });
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
  messagesEl.innerHTML = WELCOME_HTML;
});

renderSuggestions();
