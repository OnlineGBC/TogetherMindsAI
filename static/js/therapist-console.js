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

    // Stored card history, replayed once on (re)connect so the panel survives a
    // reload or server restart instead of starting blank. Oldest first → newest
    // ends on top, matching the live feed.
    sock.on("card_history", function (data) {
        var cards = (data && data.cards) || [];
        cards.forEach(_tcRenderCard);
        _tcTrim();
    });

    // Co-pilot's private reply to a therapist note. Shown even when the panel is
    // muted — it's a direct answer the therapist explicitly asked for.
    sock.on("copilot_reply", function (data) {
        var card = data && data.card;
        if (card) { _tcRenderCard(card); _tcTrim(); }
        _tcSetStatus("");
    });

    // Free tier: answers are a Pro feature. The note still steered suggestions.
    sock.on("copilot_reply_locked", function (data) {
        _tcSetStatus((data && data.message) || "Co-pilot answers are a Pro feature.");
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
        _tcSetStatus("Note sent — the co-pilot is replying…");
    }

    noteBtn.addEventListener("click", sendNote);
    noteInput.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            sendNote();
        }
    });

    // Co-pilot chattiness — More / Less / Stop (live display only; alerts are
    // always saved to the therapist's record regardless).
    panel.querySelectorAll(".tc-cad-btn").forEach(function (b) {
        b.addEventListener("click", function () {
            if (window.socket) {
                window.socket.emit("copilot_cadence", {
                    session_id: sessionId, user_id: userId, mode: b.getAttribute("data-cadence"),
                });
            }
        });
    });
    sock.on("copilot_cadence_set", function (data) {
        var m = (data && data.mode) || "more";
        panel.querySelectorAll(".tc-cad-btn").forEach(function (b) {
            b.classList.toggle("active", b.getAttribute("data-cadence") === m);
        });
        _tcSetStatus(m === "stop" ? "Live comments paused (still saved to your record)."
                   : m === "less" ? "Showing fewer comments live."
                   : "Showing all comments.");
    });

    // Session summary (private to the therapist) — generates on demand.
    document.getElementById("tcSummaryBtn").addEventListener("click", function () {
        _tcLoadSummary(sessionId);
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

    // Resize handle — drag the panel's left edge to grow/shrink it; the chat
    // reclaims the freed space (linked, no overlap). Desktop only.
    var rh = document.getElementById("tcResize");
    if (rh) {
        var dragging = false;
        rh.addEventListener("pointerdown", function (e) {
            if (window.innerWidth < 992 || panel.classList.contains("collapsed")) { return; }
            dragging = true;
            document.body.classList.add("tc-resizing");
            try { rh.setPointerCapture(e.pointerId); } catch (err) {}
            e.preventDefault();
        });
        rh.addEventListener("pointermove", function (e) {
            if (!dragging) { return; }
            // Full travel: drag from a 48px sliver up to 92vw (matches the panel's
            // max-width); the chevron still does a full collapse. 48px kept for chat.
            var maxW = Math.min(window.innerWidth * 0.92, window.innerWidth - 48);
            var w = Math.max(48, Math.min(maxW, window.innerWidth - e.clientX));
            document.documentElement.style.setProperty("--tc-width", w + "px");
        });
        function _tcEndResize(e) {
            if (!dragging) { return; }
            dragging = false;
            document.body.classList.remove("tc-resizing");
            try { rh.releasePointerCapture(e.pointerId); } catch (err) {}
        }
        rh.addEventListener("pointerup", _tcEndResize);
        rh.addEventListener("pointercancel", _tcEndResize);
    }
}

// ---------------------------------------------------------------------------
// DOM building
// ---------------------------------------------------------------------------

function _tcBuildPanel() {
    var panel = document.createElement("aside");
    panel.id = "therapistConsole";
    panel.className = "therapist-console";
    panel.innerHTML =
        '<div class="tc-resize-handle" id="tcResize" title="Drag to resize"></div>' +
        '<button type="button" id="tcExpandBtn" class="tc-expand-handle" title="Open Co-Pilot">' +
        '  <i class="bi bi-chevron-bar-left"></i>Co-Pilot</button>' +
        '<div class="tc-header">' +
        '  <span class="tc-title"><i class="bi bi-clipboard2-pulse-fill me-1"></i>Co-Pilot</span>' +
        '  <span class="tc-badge">private</span>' +
        '  <div class="tc-actions">' +
        '    <button type="button" id="tcSummaryBtn" class="tc-icon-btn" title="Session summary (private to you)"><i class="bi bi-file-earmark-text-fill"></i></button>' +
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
        '  <div class="tc-cadence" id="tcCadence">' +
        '    <span class="tc-cadence-label">Chattiness:</span>' +
        '    <button type="button" class="tc-cad-btn active" data-cadence="more">More</button>' +
        '    <button type="button" class="tc-cad-btn" data-cadence="less">Less</button>' +
        '    <button type="button" class="tc-cad-btn" data-cadence="stop">Stop</button>' +
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
        reference:   { label: "Reference", icon: "journal-medical" },
        reply:       { label: "Co-pilot reply", icon: "chat-left-heart-fill" },
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
    // Reply cards echo the therapist's question above the co-pilot's answer.
    if (type === "reply" && card.question) {
        var q = document.createElement("div");
        q.className = "tc-reply-q";
        q.textContent = card.question;
        el.appendChild(q);
    }
    el.appendChild(body);

    // Reference cards carry a grounded ICD code + source citation (read from the
    // curated corpus, never written by the model) — show them under the text.
    // Prefer clickable per-code links; fall back to the plain code string.
    if (card.code_links && card.code_links.length) {
        var codeWrap = document.createElement("div");
        codeWrap.className = "tc-card-code";
        card.code_links.forEach(function (link) {
            var a = document.createElement("a");
            a.className = "tc-code-link";
            a.href = link.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            a.textContent = link.label;
            codeWrap.appendChild(a);
            // ICD-10 links go to a third-party lookup — say so explicitly.
            if (link.third_party) {
                var tp = document.createElement("span");
                tp.className = "tc-code-tp";
                tp.textContent = "third-party";
                codeWrap.appendChild(tp);
            }
        });
        el.appendChild(codeWrap);
    } else if (card.code) {
        var code = document.createElement("div");
        code.className = "tc-card-code";
        code.textContent = card.code;
        el.appendChild(code);
    }
    if (card.source) {
        var src = document.createElement("div");
        src.className = "tc-card-source";
        src.textContent = card.source;
        el.appendChild(src);
    }

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

// ---------------------------------------------------------------------------
// Session summary — private to the therapist (clinical recap + grounded ICD
// codes for billing reference + a draft the therapist MAY share). The endpoint
// is therapist-gated server-side; this UI is only loaded for the therapist.
// ---------------------------------------------------------------------------

function _tcLoadSummary(sessionId) {
    var overlay = _tcSummaryOverlay();
    var body = overlay.querySelector(".tcs-body");
    body.innerHTML = "";
    body.appendChild(_tcEl("div", "tcs-note", "Generating session summary… this can take a few seconds."));

    fetch("/session/" + encodeURIComponent(sessionId) + "/summary", {
        headers: { "Accept": "application/json" },
    })
        .then(function (r) {
            if (r.status === 403) { throw new Error("forbidden"); }
            // 402 — feature locked to a paid plan; render the returned upsell.
            if (r.status === 402) { return r.json().then(function (d) { throw { locked: true, data: d }; }); }
            if (!r.ok) { throw new Error("http " + r.status); }
            return r.json();
        })
        .then(function (data) { _tcRenderSummary(body, data); })
        .catch(function (err) {
            body.innerHTML = "";
            if (err && err.locked) {
                var d = err.data || {};
                body.appendChild(_tcEl("div", "tcs-note", d.message || "The AI session summary is a Plus feature."));
                var a = _tcEl("a", "tcs-upgrade", "Upgrade in Plans & billing");
                a.href = d.upgrade_url || "/billing";
                a.target = "_blank"; a.rel = "noopener noreferrer";
                body.appendChild(a);
                return;
            }
            var msg = err && err.message === "forbidden"
                ? "This summary is available only to the session's therapist."
                : "Could not generate the summary right now. Please try again.";
            body.appendChild(_tcEl("div", "tcs-note", msg));
        });
}

function _tcEl(tag, className, text) {
    var el = document.createElement(tag);
    if (className) { el.className = className; }
    if (text != null) { el.textContent = text; }
    return el;
}

function _tcRenderSummary(body, data) {
    body.innerHTML = "";

    if (data.disclaimer) {
        body.appendChild(_tcEl("div", "tcs-disclaimer", data.disclaimer));
    }

    // Clinical summary
    body.appendChild(_tcEl("h4", "tcs-h", "Clinical summary"));
    body.appendChild(_tcEl("p", "tcs-p",
        data.clinical || "AI narrative unavailable — the grounded codes below are still accurate."));

    // ICD codes (billing reference)
    body.appendChild(_tcEl("h4", "tcs-h", "ICD codes — billing reference"));
    var codes = data.codes || [];
    if (codes.length) {
        var ul = _tcEl("ul", "tcs-codes");
        codes.forEach(function (c) {
            var label = c.label ? c.label + " — " : "";
            var li = _tcEl("li", null, label + (c.code || "") + (c.source ? "  (" + c.source + ")" : ""));
            ul.appendChild(li);
        });
        body.appendChild(ul);
    } else {
        body.appendChild(_tcEl("p", "tcs-p", "No ICD reference codes surfaced during this session."));
    }
    if (data.codes_rationale) {
        body.appendChild(_tcEl("p", "tcs-p", data.codes_rationale));
    }

    // Client-facing draft
    if (data.client_recap) {
        body.appendChild(_tcEl("h4", "tcs-h",
            "Client-facing draft — share only at your discretion"));
        body.appendChild(_tcEl("div", "tcs-note tcs-warn",
            "The client cannot see this unless you choose to give it to them."));
        body.appendChild(_tcEl("p", "tcs-p tcs-client", data.client_recap));
    }
}

function _tcSummaryOverlay() {
    var existing = document.getElementById("tcSummaryOverlay");
    if (existing) { existing.style.display = "flex"; return existing; }

    var overlay = document.createElement("div");
    overlay.id = "tcSummaryOverlay";
    overlay.style.cssText =
        "position:fixed;inset:0;z-index:3000;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(0,0,0,.45);padding:16px;";

    var card = document.createElement("div");
    card.style.cssText =
        "background:var(--surface);color:var(--text-dark);max-width:680px;width:100%;max-height:88vh;overflow:auto;" +
        "border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.5);padding:20px 22px;";

    var head = document.createElement("div");
    head.style.cssText = "display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;";
    var title = _tcEl("strong", null, "Session Summary — Private");
    title.style.cssText = "font-size:1.1rem;color:#5fd0c2;";
    var close = _tcEl("button", null, "✕");
    close.type = "button";
    close.title = "Close";
    close.style.cssText = "border:none;background:none;font-size:1.2rem;cursor:pointer;color:#555;";
    close.addEventListener("click", function () { overlay.style.display = "none"; });
    head.appendChild(title);
    head.appendChild(close);

    var bodyEl = document.createElement("div");
    bodyEl.className = "tcs-body";
    bodyEl.style.cssText = "font-size:.92rem;line-height:1.5;color:var(--text-dark);";

    // Lightweight scoped styling for the summary sections.
    var style = document.createElement("style");
    style.textContent =
        "#tcSummaryOverlay .tcs-h{margin:14px 0 4px;font-size:.8rem;text-transform:uppercase;" +
        "letter-spacing:.03em;color:#6b7280;}" +
        "#tcSummaryOverlay .tcs-p{margin:0 0 8px;white-space:pre-wrap;}" +
        "#tcSummaryOverlay .tcs-codes{margin:0 0 8px;padding-left:18px;}" +
        "#tcSummaryOverlay .tcs-codes li{margin-bottom:3px;}" +
        "#tcSummaryOverlay .tcs-disclaimer{font-size:.78rem;font-style:italic;color:#92400e;" +
        "background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:8px 10px;margin-bottom:6px;}" +
        "#tcSummaryOverlay .tcs-note{color:#555;font-size:.85rem;margin:4px 0;}" +
        "#tcSummaryOverlay .tcs-upgrade{display:inline-block;margin-top:8px;font-weight:600;" +
        "color:#00796b;text-decoration:underline;}" +
        "#tcSummaryOverlay .tcs-warn{color:#92400e;font-weight:600;}" +
        "#tcSummaryOverlay .tcs-client{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:10px 12px;}";

    card.appendChild(head);
    card.appendChild(style);
    card.appendChild(bodyEl);
    overlay.appendChild(card);
    overlay.addEventListener("click", function (e) {
        if (e.target === overlay) { overlay.style.display = "none"; }
    });
    document.body.appendChild(overlay);
    return overlay;
}
