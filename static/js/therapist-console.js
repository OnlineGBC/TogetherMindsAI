/**
 * therapist-console.js
 * Private co-pilot console for the therapist leading a session.
 *
 * Renders suggestion / risk cards pushed from the server over the therapist-only
 * SocketIO room, plus a notes box that steers the co-pilot. Loaded ONLY when the
 * current user is the session therapist (gated server-side via IS_THERAPIST), so
 * clients never receive this script or its socket events.
 *
 * Relies on the global `socket` created by joinRoom() in therapy.js — call
 * initTherapistConsole() after joinRoom() so the connection already exists.
 */

var _tcMuted = false;
var _TC_MAX_CARDS = 15;

function initTherapistConsole(sessionId, userId) {
    var panel = _tcBuildPanel();
    document.body.appendChild(panel);
    // Shift the page so the fixed panel doesn't cover the chat (desktop CSS).
    document.body.classList.add("tcp-console-open");
    // On phones/tablets the open panel would cover the chat — start collapsed.
    if (window.innerWidth < 992) {
        panel.classList.add("collapsed");
        document.body.classList.add("tcp-collapsed");
    }

    var sock = window.socket;
    if (!sock) {
        console.warn("Therapist console: socket not ready.");
        return;
    }

    sock.on("console_init", function () {
        _tcSetStatus("Listening for suggestions…");
    });

    sock.on("suggestion_cards", function (data) {
        var cards = (data && data.cards) || [];
        if (_tcMuted) { return; }
        cards.forEach(_tcRenderCard);
        _tcTrim();
    });

    // Notes box → therapist_note
    var noteInput = document.getElementById("tcNoteInput");
    var noteBtn   = document.getElementById("tcNoteBtn");

    function sendNote() {
        var text = noteInput.value.trim();
        if (!text || !window.socket) { return; }
        window.socket.emit("therapist_note", {
            session_id: sessionId,
            user_id:    userId,
            text:       text,
        });
        noteInput.value = "";
        _tcSetStatus("Note sent — refreshing suggestions…");
    }

    noteBtn.addEventListener("click", sendNote);
    noteInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendNote();
        }
    });

    // Mute toggle
    document.getElementById("tcMuteBtn").addEventListener("click", function () {
        _tcMuted = !_tcMuted;
        this.classList.toggle("active", _tcMuted);
        panel.classList.toggle("tc-muted", _tcMuted);
        this.innerHTML = _tcMuted
            ? '<i class="bi bi-bell-slash-fill"></i>'
            : '<i class="bi bi-bell-fill"></i>';
        this.title = _tcMuted ? "Suggestions muted — click to resume" : "Mute suggestions";
        _tcSetStatus(_tcMuted ? "Muted — incoming suggestions are hidden." : "Listening for suggestions…");
    });

    // Collapse / expand — keep the body padding in sync so the chat reclaims space.
    function _tcSetCollapsed(collapsed) {
        panel.classList.toggle("collapsed", collapsed);
        document.body.classList.toggle("tcp-collapsed", collapsed);
    }
    document.getElementById("tcCollapseBtn").addEventListener("click", function () {
        _tcSetCollapsed(!panel.classList.contains("collapsed"));
    });
    document.getElementById("tcExpandBtn").addEventListener("click", function () {
        _tcSetCollapsed(false);
    });
}

// ---------------------------------------------------------------------------
// DOM building
// ---------------------------------------------------------------------------

function _tcBuildPanel() {
    var panel = document.createElement("aside");
    panel.id = "therapistConsole";
    panel.className = "therapist-console";
    panel.innerHTML =
        '<button type="button" id="tcExpandBtn" class="tc-expand-handle" title="Open Co-Pilot">' +
        '  <i class="bi bi-chevron-bar-left"></i>Co-Pilot</button>' +
        '<div class="tc-header">' +
        '  <span class="tc-title"><i class="bi bi-clipboard2-pulse-fill me-1"></i>Co-Pilot</span>' +
        '  <span class="tc-badge">private</span>' +
        '  <div class="tc-actions">' +
        '    <button type="button" id="tcMuteBtn" class="tc-icon-btn" title="Mute suggestions"><i class="bi bi-bell-fill"></i></button>' +
        '    <button type="button" id="tcCollapseBtn" class="tc-icon-btn" title="Collapse"><i class="bi bi-chevron-bar-right"></i></button>' +
        '  </div>' +
        '</div>' +
        '<div class="tc-body">' +
        '  <div class="tc-status" id="tcStatus">Connecting…</div>' +
        '  <div class="tc-cards" id="tcCards">' +
        '    <div class="tc-empty" id="tcEmpty"><i class="bi bi-stars"></i>' +
        'Suggestions and risk alerts will appear here as the conversation unfolds.</div>' +
        '  </div>' +
        '  <div class="tc-notes">' +
        '    <textarea id="tcNoteInput" rows="1" class="form-control form-control-sm" ' +
        '              placeholder="Private note to the co-pilot…"></textarea>' +
        '    <button type="button" id="tcNoteBtn" class="btn btn-sm btn-primary-green" title="Send note">' +
        '      <i class="bi bi-send-fill"></i></button>' +
        '  </div>' +
        '</div>';
    return panel;
}

function _tcRenderCard(card) {
    var container = document.getElementById("tcCards");
    if (!container) { return; }

    var empty = document.getElementById("tcEmpty");
    if (empty) { empty.remove(); }

    var type = (card.type || "observation").toLowerCase();
    var el = document.createElement("div");
    el.className = "tc-card tc-card-" + type + (card.priority === "high" ? " tc-card-urgent" : "");

    var meta = {
        risk:        { label: "Risk",      icon: "exclamation-triangle-fill" },
        question:    { label: "Ask",       icon: "chat-quote-fill" },
        technique:   { label: "Technique", icon: "life-preserver" },
        observation: { label: "Notice",    icon: "eye-fill" },
    }[type] || { label: "Note", icon: "sticky-fill" };

    var dismiss = document.createElement("button");
    dismiss.className = "tc-dismiss";
    dismiss.title = "Dismiss";
    dismiss.innerHTML = '<i class="bi bi-x"></i>';
    dismiss.addEventListener("click", function () { el.remove(); });

    var head = document.createElement("div");
    head.className = "tc-card-label";
    head.innerHTML = '<i class="bi bi-' + meta.icon + '"></i><span></span>';
    head.querySelector("span").textContent = meta.label;

    var body = document.createElement("div");
    body.className = "tc-card-text";
    body.textContent = card.text || "";

    el.appendChild(dismiss);
    el.appendChild(head);
    el.appendChild(body);

    // Newest on top; risk cards always pinned above suggestions.
    if (card.priority === "high") {
        container.insertBefore(el, container.firstChild);
    } else {
        var firstNonUrgent = container.querySelector(".tc-card:not(.tc-card-urgent)");
        container.insertBefore(el, firstNonUrgent);
    }
}

function _tcTrim() {
    var container = document.getElementById("tcCards");
    if (!container) { return; }
    var cards = container.querySelectorAll(".tc-card");
    for (var i = _TC_MAX_CARDS; i < cards.length; i++) {
        cards[i].remove();
    }
}

function _tcSetStatus(text) {
    var s = document.getElementById("tcStatus");
    if (s) { s.textContent = text; }
}
