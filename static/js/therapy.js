/**
 * therapy.js
 * SocketIO client logic shared by couple.html and group.html
 */

var socket = null;
var _currentUserId = null;

// ---------------------------------------------------------------------------
// Translation gate — call before any chat send. If text is English the helper
// invokes onProceed(text) immediately. If non-English, it pops a confirmation
// modal showing the original and the English translation; on Confirm runs
// onProceed(translation). Cancel/X = no-op (user can edit and re-send).
//
// Fail-open: any network/server error proceeds with original text rather than
// blocking the user from sending.
// ---------------------------------------------------------------------------
function checkAndConfirmEnglish(text, onProceed, onCancel) {
    if (!text || !text.trim()) return;

    fetch("/api/translate-check", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text }),
    }).then(function (res) {
        return res.json().catch(function () { return {}; });
    }).then(function (data) {
        if (!data || data.is_english !== false || !data.translation) {
            // English (or fail-open fallback): send original text.
            onProceed(text);
            return;
        }
        var modalEl = document.getElementById("translateConfirmModal");
        if (!modalEl || typeof bootstrap === "undefined") {
            // Modal not in DOM (shouldn't happen on chat pages) — proceed without confirmation.
            onProceed(data.translation);
            return;
        }
        document.getElementById("translateOriginal").textContent = text;
        document.getElementById("translateEnglish").textContent = data.translation;

        var modal = bootstrap.Modal.getOrCreateInstance(modalEl);
        var confirmBtn = document.getElementById("translateConfirmBtn");
        var cancelBtn = document.getElementById("translateCancelBtn");

        function cleanup() {
            confirmBtn.removeEventListener("click", onConfirmClick);
            cancelBtn.removeEventListener("click", onCancelClick);
        }
        function onConfirmClick() {
            cleanup();
            modal.hide();
            onProceed(data.translation);
        }
        function onCancelClick() {
            cleanup();
            modal.hide();
            if (onCancel) onCancel();
        }
        confirmBtn.addEventListener("click", onConfirmClick);
        cancelBtn.addEventListener("click", onCancelClick);
        modal.show();
    }).catch(function () {
        // Network error → fail-open with original text.
        onProceed(text);
    });
}

// ---------------------------------------------------------------------------
// Chat input auto-grow — wraps text to next line up to a CSS max-height,
// then scrolls inside. Call once per page on any chat-input textarea.
// Voice input dispatches an "input" event after pasting transcripts, which
// triggers re-measurement automatically.
// ---------------------------------------------------------------------------
function initChatTextareaAutoGrow(elementId) {
    var el = document.getElementById(elementId);
    if (!el || el.tagName !== "TEXTAREA") return;

    function recalc() {
        // Reset to auto so scrollHeight reflects content, not previous height,
        // then grow to fit. CSS max-height caps it; overflow-y: auto handles
        // anything beyond that.
        el.style.height = "auto";
        el.style.height = el.scrollHeight + "px";
    }
    el.addEventListener("input", recalc);
    // Initial measurement (covers server-rendered draft text in solo mode).
    setTimeout(recalc, 0);

    // Expose a reset hook so send-handlers can collapse back to one line
    // after clearing the value.
    el._resetAutoGrow = function () {
        el.value = "";
        el.style.height = "auto";
    };
}

// ---------------------------------------------------------------------------
// Wellness check — fires after 20 minutes of inactivity
// ---------------------------------------------------------------------------
var _inactivityTimer = null;
var _INACTIVITY_MS   = 20 * 60 * 1000; // 20 minutes

/**
 * Start the inactivity wellness check timer.
 * Call once per page load on any therapy page.
 */
function startWellnessCheck() {
    _resetInactivityTimer();
    document.addEventListener("keydown", _resetInactivityTimer);
    document.addEventListener("click",   _resetInactivityTimer);
}

function _resetInactivityTimer() {
    clearTimeout(_inactivityTimer);
    _inactivityTimer = setTimeout(_showWellnessModal, _INACTIVITY_MS);
}

function _showWellnessModal() {
    var modalEl = document.getElementById("wellnessModal");
    if (modalEl && typeof bootstrap !== "undefined") {
        bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
    // Reset so it fires again after the next period of inactivity
    _resetInactivityTimer();
}

// Current user's display name (set after name prompt is confirmed)
var _currentDisplayName = null;

/**
 * Establish a SocketIO connection and join a therapy room.
 * @param {string} sessionId  - The room / session identifier
 * @param {string} userId     - The current user's UUID
 * @param {string} mode       - Therapy mode: "couple" or "group"
 * @param {boolean} soloMode  - True for solo (server-rendered messages; skip history render)
 */
function joinRoom(sessionId, userId, mode, soloMode) {
    _currentUserId = userId;

    // Use polling only. Cloud Run is capped at max-instances=1 so all
    // Socket.IO sessions share the same instance — no cross-instance room
    // problem. WebSocket upgrade is skipped: werkzeug (dev) crashes on it
    // and Cloud Run's load balancer does not reliably pass WS upgrades through.
    socket = io({
        transports: ["polling"],
        upgrade: false,
    });

    socket.on("connect", function () {
        socket.emit("join", { session_id: sessionId, user_id: userId, mode: mode || "solo" });
    });

    socket.on("history", function (data) {
        var messages           = data.messages             || [];
        var defaultName        = data.default_name         || "";
        var currentDisplayName = data.current_display_name || null;

        if (soloMode) {
            // Solo messages are already server-rendered
            if (currentDisplayName) {
                _currentDisplayName = currentDisplayName;
                _updateDisplayNameBanner(sessionId, currentDisplayName);
            } else {
                _acceptDefaultName(sessionId, userId, defaultName, true);
            }
            return;
        }

        var chatBox = document.getElementById("chatBox");
        if (messages.length > 0) {
            chatBox.innerHTML = "";
            messages.forEach(function (msg) {
                appendMessage(chatBox, msg.user_id, msg.text, msg.timestamp,
                              userId, msg.display_name, sessionId);
            });
            // Opening message has arrived — allow participants to send messages.
            // The send button starts disabled in the HTML so no one can speak
            // before the AI co-pilot has opened the session.
            _hideSendSpinner();
        }
        scrollToBottom(chatBox);

        if (currentDisplayName) {
            _currentDisplayName = currentDisplayName;
            _updateDisplayNameBanner(sessionId, currentDisplayName);
        } else {
            _acceptDefaultName(sessionId, userId, defaultName, false);
        }
    });

    socket.on("new_message", function (data) {
        var chatBox = document.getElementById("chatBox");

        // Remove empty state if present
        var emptyState = document.getElementById("emptyState");
        if (emptyState) {
            emptyState.remove();
        }

        appendMessage(chatBox, data.user_id, data.text, data.timestamp,
                      _currentUserId, data.display_name, sessionId);
        scrollToBottom(chatBox);

        // Restore send button:
        // - Solo: wait for the AI reply (page reload handles the rest anyway).
        // - Couple/group: re-enable as soon as any message arrives. The AI may
        //   not respond at all (20s cooldown), so we cannot wait for an AI message
        //   or the button stays disabled permanently.
        if (data.user_id === "AI" || !soloMode) {
            _hideSendSpinner();
        }
    });

    socket.on("name_set", function (data) {
        if (data.user_id === _currentUserId) {
            _currentDisplayName = data.display_name;
            _updateDisplayNameBanner(sessionId, data.display_name);
        }
    });

    socket.on("name_error", function (data) {
        // Show error in whichever name modal is open
        var promptErr  = document.getElementById("displayNameError");
        var renameErr  = document.getElementById("renameError");
        if (promptErr && !promptErr.closest(".modal").classList.contains("d-none")) {
            promptErr.textContent = data.message;
            promptErr.classList.remove("d-none");
        }
        if (renameErr) {
            renameErr.textContent = data.message;
            renameErr.classList.remove("d-none");
        }
    });

    socket.on("name_changed", function (data) {
        // Re-label all existing bubbles for this user
        var wrappers = document.querySelectorAll('[data-user-id="' + data.user_id + '"]');
        wrappers.forEach(function (wrapper) {
            var label = wrapper.querySelector(".bubble-sender");
            if (label) {
                label.textContent = sessionId + "-" + data.new_name;
            }
        });

        // Update our own banner if this is us
        if (data.user_id === _currentUserId) {
            _currentDisplayName = data.new_name;
            _updateDisplayNameBanner(sessionId, data.new_name);
        }

        // System notice
        var chatBox = document.getElementById("chatBox");
        if (chatBox) {
            var notice = document.createElement("div");
            notice.className = "text-center text-muted small py-1 fst-italic";
            notice.textContent = sessionId + "-" + data.old_name +
                                 " is now known as " + sessionId + "-" + data.new_name;
            chatBox.appendChild(notice);
            scrollToBottom(chatBox);
        }
    });

    socket.on("rate_limited", function (data) {
        var chatBox = document.getElementById("chatBox");
        if (chatBox) {
            var notice = document.createElement("div");
            notice.className = "text-center text-muted small py-2";
            notice.textContent = data.message || "You're sending messages too quickly. Please slow down.";
            chatBox.appendChild(notice);
            scrollToBottom(chatBox);
        }
    });

    socket.on("error", function (data) {
        var chatBox = document.getElementById("chatBox");
        if (chatBox) {
            var notice = document.createElement("div");
            notice.className = "text-center text-danger small py-2";
            notice.textContent = data.message || "An error occurred.";
            chatBox.appendChild(notice);
            scrollToBottom(chatBox);
        }
    });

    socket.on("participant_list", function (data) {
        _updateParticipantPanel(data.participants || []);
    });

    socket.on("participant_joined", function (data) {
        _updateParticipantCount(1);
    });

    socket.on("participant_left", function (data) {
        _updateParticipantCount(-1);
    });

    socket.on("disconnect", function () {
        console.log("SocketIO disconnected");
    });

    socket.on("connect_error", function (err) {
        console.error("SocketIO connection error:", err.message);
    });
}

/**
 * Phase 4 — recording consent controls.
 *
 * Recording runs ONLY while every current participant consents. It stops the
 * instant anyone declines or withdraws, or an un-consented participant joins,
 * and resumes once everyone consents again. The clinician initiates; the server
 * is the source of truth and broadcasts "recording_state" on every change.
 *
 * @param {string}  sessionId
 * @param {string}  userId
 * @param {boolean} isTherapist
 */
function initRecordingControls(sessionId, userId, isTherapist) {
    if (!socket) return;
    var bar         = document.getElementById("recBar");
    if (!bar) return;

    var badge       = document.getElementById("recBadge");
    var statusText  = document.getElementById("recStatusText");
    var requestBtn  = document.getElementById("recRequestBtn");    // therapist
    var stopBtn     = document.getElementById("recStopBtn");       // therapist
    var allowBtn    = document.getElementById("recAllowBtn");      // client
    var withdrawBtn = document.getElementById("recWithdrawBtn");   // client
    var consentBtn  = document.getElementById("recConsentBtn");    // modal
    var declineBtn  = document.getElementById("recDeclineBtn");    // modal
    var modalEl     = document.getElementById("recConsentModal");

    bar.classList.remove("d-none");

    // Local hint only — the server's "awaiting" list is authoritative for button
    // state; this just stops the consent modal from re-popping once answered.
    var _answered = isTherapist;

    function show(el) { if (el) el.classList.remove("d-none"); }
    function hide(el) { if (el) el.classList.add("d-none"); }

    function sendConsent(value) {
        _answered = true;
        socket.emit("recording_consent", { session_id: sessionId, user_id: userId, consent: value });
    }
    function hideModal() {
        if (modalEl && typeof bootstrap !== "undefined") {
            bootstrap.Modal.getOrCreateInstance(modalEl).hide();
        }
    }

    // ---- Clinician controls ----
    if (requestBtn) requestBtn.addEventListener("click", function () {
        socket.emit("recording_request", { session_id: sessionId, user_id: userId });
    });
    if (stopBtn) stopBtn.addEventListener("click", function () {
        socket.emit("recording_cancel", { session_id: sessionId, user_id: userId });
    });

    // ---- Client controls ----
    if (allowBtn)    allowBtn.addEventListener("click", function () { sendConsent(true); });
    if (withdrawBtn) withdrawBtn.addEventListener("click", function () { sendConsent(false); });
    if (consentBtn)  consentBtn.addEventListener("click", function () { sendConsent(true); hideModal(); });
    if (declineBtn)  declineBtn.addEventListener("click", function () { sendConsent(false); });

    // ---- Server → client events ----
    socket.on("recording_unavailable", function (data) {
        if (statusText) statusText.textContent = (data && data.message) || "Recording is not available.";
    });

    socket.on("recording_consent_prompt", function () {
        // Prompt this client to consent — once — unless they've already answered.
        if (isTherapist || _answered) return;
        if (modalEl && typeof bootstrap !== "undefined") {
            bootstrap.Modal.getOrCreateInstance(modalEl).show();
        }
    });

    socket.on("recording_state", function (st) {
        st = st || {};
        var requested  = !!st.requested;
        var active     = !!st.active;
        var awaiting   = st.awaiting || [];
        var iAmAwaited = awaiting.indexOf(userId) !== -1;
        if (!requested) _answered = isTherapist;   // reset for the next request round
        if (statusText) statusText.textContent = "";   // live status now lives on the button

        if (isTherapist) {
            if (!requested) {
                show(requestBtn); hide(stopBtn);
            } else {
                hide(requestBtn); show(stopBtn);
                if (active) {
                    // Recording in progress — click to stop.
                    stopBtn.innerHTML = '<span class="rec-blink" aria-hidden="true">●</span> Recording';
                    stopBtn.className = "btn btn-sm btn-danger rounded-pill";
                    stopBtn.title = "Recording — click to stop";
                } else {
                    // Requested but waiting for everyone to consent — click to cancel.
                    stopBtn.innerHTML = "Awaiting consent (" + awaiting.length + ")";
                    stopBtn.className = "btn btn-sm btn-warning rounded-pill";
                    stopBtn.title = "Waiting for everyone to consent — click to cancel";
                }
            }
        } else if (requested) {
            if (iAmAwaited) {
                show(allowBtn); hide(withdrawBtn);
            } else {
                hide(allowBtn); show(withdrawBtn);
                // Once consenting, the Withdraw button doubles as the live indicator.
                withdrawBtn.innerHTML = active
                    ? '<span class="rec-blink" aria-hidden="true">●</span> Recording — withdraw'
                    : '<i class="bi bi-shield-x"></i> Withdraw';
            }
        } else {
            hide(allowBtn); hide(withdrawBtn);
            hideModal();
        }
    });
}

/**
 * Send a message through SocketIO.
 * @param {string} sessionId - Room identifier
 * @param {string} userId    - Sender's UUID
 * @param {string} text      - Message content
 * @param {string} mode      - Therapy mode: "couple" or "group"
 */
function sendMessage(sessionId, userId, text, mode) {
    if (!socket || !socket.connected) {
        console.warn("Socket not connected — cannot send message.");
        return;
    }
    _showSendSpinner();
    socket.emit("send_message", {
        session_id: sessionId,
        user_id: userId,
        text: text,
        mode: mode || "solo",
    });
}

// ---------------------------------------------------------------------------
// Send button spinner helpers
// ---------------------------------------------------------------------------

var _SEND_ICON_HTML = '<i class="bi bi-send-fill"></i>';
var _SEND_SPINNER_HTML = '<span class="spinner-multi" role="status" aria-hidden="true"></span>';

function _getSendBtn() {
    // All three modes now use id="sendBtn" (solo's submit button also has this id)
    return document.getElementById("sendBtn");
}

function _showSendSpinner() {
    var btn = _getSendBtn();
    if (btn) {
        btn.innerHTML = _SEND_SPINNER_HTML;
        btn.disabled = true;
    }
}

function _hideSendSpinner() {
    var btn = _getSendBtn();
    if (btn) {
        btn.innerHTML = _SEND_ICON_HTML;
        btn.disabled = false;
    }
}

/**
 * Build and append a chat bubble to the chat box.
 * @param {HTMLElement} chatBox      - The scrollable chat container
 * @param {string}      senderId     - User ID of the sender ("AI" or UUID)
 * @param {string}      text         - Message text
 * @param {string}      timestamp    - Formatted timestamp string
 * @param {string}      myUserId     - The current viewer's user ID (to decide alignment)
 * @param {string|null} displayName  - Participant display name (e.g. "Michael"); null for AI
 * @param {string}      sessionId    - Session ID prefix for the label
 */
function appendMessage(chatBox, senderId, text, timestamp, myUserId, displayName, sessionId) {
    var isAI = senderId === "AI";
    var isMe = !isAI && senderId === myUserId;

    var wrapper = document.createElement("div");
    wrapper.className = "d-flex mb-3 align-items-end" + (isMe ? " justify-content-end" : "");
    if (!isAI && senderId) {
        wrapper.setAttribute("data-user-id", senderId);
    }

    var senderLabel;
    if (isAI) {
        senderLabel = "AI Co-Pilot";
    } else if (displayName) {
        senderLabel = (sessionId || "") + "-" + displayName;
    } else {
        // Fallback for legacy messages with no display_name stored
        senderLabel = isMe ? "You" : "Partner";
    }

    var timeStr = timestamp || "";
    // Extract HH:MM if a full datetime string was provided
    if (timeStr.length > 5) {
        var parts = timeStr.split(" ");
        if (parts.length > 1) {
            timeStr = parts[1].substring(0, 5);
        }
    }

    if (isAI || !isMe) {
        // Left-aligned bubble (AI or other participant)
        var avatarIcon  = isAI ? "bi-robot" : "bi-person-fill";
        var bubbleClass = "bubble " + (isAI ? "bubble-ai" : "bubble-partner");

        wrapper.innerHTML =
            '<div class="' + (isAI ? "avatar-ai" : "avatar-user") + ' me-2">' +
                '<i class="bi ' + avatarIcon + '"></i>' +
            '</div>' +
            '<div class="' + bubbleClass + '">' +
                '<span class="bubble-sender">' + escapeHtml(senderLabel) + '</span>' +
                '<p class="mb-0">' + escapeHtml(text) + '</p>' +
                '<span class="bubble-time">' + escapeHtml(timeStr) + '</span>' +
            '</div>';
    } else {
        // Right-aligned bubble (current user)
        wrapper.innerHTML =
            '<div class="bubble bubble-user">' +
                '<span class="bubble-sender text-end d-block">' + escapeHtml(senderLabel) + '</span>' +
                '<p class="mb-0">' + escapeHtml(text) + '</p>' +
                '<span class="bubble-time">' + escapeHtml(timeStr) + '</span>' +
            '</div>' +
            '<div class="avatar-user ms-2">' +
                '<i class="bi bi-person-fill"></i>' +
            '</div>';
    }

    chatBox.appendChild(wrapper);
}

/**
 * Scroll a container to its bottom.
 * @param {HTMLElement} el
 */
function scrollToBottom(el) {
    if (el) {
        el.scrollTop = el.scrollHeight;
    }
}

/**
 * Escape HTML special characters to prevent XSS.
 * @param {string} str
 * @returns {string}
 */
function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
}

// ---------------------------------------------------------------------------
// End Session guard — modal + beforeunload warning
// ---------------------------------------------------------------------------

var _sessionEnded = false;

/**
 * Wire up the End Session button, modal, and beforeunload warning.
 * @param {string} sessionId   - Session ID to display in the modal
 * @param {string} redirectUrl - URL to navigate to after confirming end
 */
function initEndSessionGuard(sessionId, userId, redirectUrl) {
    // Plain confirm: the modal just asks "end for everyone?". The server ends any
    // recording, emails the clinician the session record, and notifies clients.
    var confirmBtn = document.getElementById("endSessionConfirmBtn");

    function _showEndError(msg) {
        var n = document.getElementById("endSessionError");
        if (n) { n.textContent = msg; n.classList.remove("d-none"); }
    }

    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            var errEl = document.getElementById("endSessionError");
            if (errEl) { errEl.classList.add("d-none"); }
            var original = confirmBtn.innerHTML;
            confirmBtn.disabled = true;
            confirmBtn.innerHTML = "Ending…";
            var done = false;
            var fail = function (msg) {
                if (done) { return; }
                done = true;
                confirmBtn.disabled = false;
                confirmBtn.innerHTML = original;
                _showEndError(msg);
            };
            var timer = setTimeout(function () {
                fail("Couldn't end the session — please try again.");
            }, 8000);
            // End over plain HTTP — reliable, signed in via the session cookie, and
            // NOT dependent on the live socket. Confirm-before-navigate: only leave
            // on a confirmed end; on failure or timeout, STAY and show the error.
            fetch("/session/" + encodeURIComponent(sessionId) + "/end", { method: "POST" })
                .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
                .then(function (resp) {
                    if (done) { return; }
                    clearTimeout(timer);
                    if (resp && resp.ended) {
                        done = true;
                        _sessionEnded = true;        // success → suppress the leave warning
                        window.location.href = redirectUrl;
                    } else {
                        fail("Couldn't end the session. You may not be its clinician.");
                    }
                })
                .catch(function () {
                    clearTimeout(timer);
                    fail("Couldn't end the session — please try again.");
                });
        });
    }

    // beforeunload warning — fires on tab close / navigation away
    window.addEventListener("beforeunload", function (e) {
        if (!_sessionEnded) {
            e.preventDefault();
            // Modern browsers show their own generic message; returnValue is required
            e.returnValue = "You have an active session. Save your Session ID (" + sessionId + ") before leaving.";
            return e.returnValue;
        }
    });
}

// ---------------------------------------------------------------------------
// Session controls — client Leave, therapist-set friendly name, end-session
// notification. (End-session emit itself lives in initEndSessionGuard above.)
// ---------------------------------------------------------------------------

function _showSessionToast(msg) {
    var t = document.createElement("div");
    t.style.cssText = "position:fixed;top:16px;left:50%;transform:translateX(-50%);z-index:5000;" +
        "background:#15403A;color:#eaf5f2;border:1px solid rgba(0,150,136,.55);border-radius:10px;" +
        "padding:10px 16px;font-size:.95rem;box-shadow:0 8px 24px rgba(0,0,0,.5);max-width:90vw;text-align:center;";
    var icon = document.createElement("i"); icon.className = "bi bi-tag-fill me-2";
    t.appendChild(icon);
    t.appendChild(document.createTextNode(msg));
    document.body.appendChild(t);
    setTimeout(function () { t.remove(); }, 5000);
}

function _showSessionEndedOverlay() {
    if (document.getElementById("_sessEndOverlay")) return;
    var o = document.createElement("div");
    o.id = "_sessEndOverlay";
    o.style.cssText = "position:fixed;inset:0;z-index:6000;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(0,0,0,.6);";
    o.innerHTML = '<div style="background:#1E2A28;color:#E8EEEC;border-radius:14px;padding:24px 28px;' +
        'max-width:360px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.5);">' +
        '<i class="bi bi-door-closed-fill" style="font-size:2rem;color:#4CAF50"></i>' +
        '<h5 class="mt-2 mb-1">Session ended</h5>' +
        '<p class="small mb-3" style="color:#9DB0AA">The therapist has ended this session.</p>' +
        '<button id="_sessEndOk" class="btn btn-primary-green rounded-pill">Leave</button></div>';
    document.body.appendChild(o);
    var go = function () { _sessionEnded = true; window.location.href = "/"; };
    document.getElementById("_sessEndOk").addEventListener("click", go);
    setTimeout(go, 5000);   // auto-leave after 5s
}

// Waiting room — a client joined (or tried to talk) while no clinician is present.
// Cover the session and disable the composer until the therapist arrives. No
// client-only conversation is allowed without a qualified clinician.
function _showWaitingRoomOverlay(msg) {
    var input = document.getElementById("messageInput");
    var send  = document.getElementById("sendBtn");
    if (input) { input.disabled = true; }
    if (send)  { send.disabled = true; }
    if (document.getElementById("_waitingOverlay")) return;
    var o = document.createElement("div");
    o.id = "_waitingOverlay";
    o.style.cssText = "position:fixed;inset:0;z-index:6000;display:flex;align-items:center;" +
        "justify-content:center;background:rgba(0,0,0,.6);";
    o.innerHTML = '<div style="background:#1E2A28;color:#E8EEEC;border-radius:14px;padding:24px 28px;' +
        'max-width:380px;text-align:center;box-shadow:0 12px 40px rgba(0,0,0,.5);">' +
        '<i class="bi bi-hourglass-split" style="font-size:2rem;color:#4CAF50"></i>' +
        '<h5 class="mt-2 mb-1">Waiting room</h5>' +
        '<p class="small mb-3" style="color:#9DB0AA">' + (msg || "Please wait — your clinician will start the session.") + '</p>' +
        '<button id="_waitLeave" class="btn btn-outline-light rounded-pill btn-sm">Leave</button></div>';
    document.body.appendChild(o);
    document.getElementById("_waitLeave").addEventListener("click", function () {
        _sessionEnded = true; window.location.href = "/";
    });
}

/**
 * Wire client Leave, the therapist-set shared session friendly name, and the
 * end-session notification. `socket` is already connected (joinRoom ran first).
 */
function initSessionControls(sessionId, userId, isTherapist) {
    var fnLabel = document.getElementById("friendlyNameLabel");
    function applyName(name) {
        if (!fnLabel) return;
        if (name) { fnLabel.textContent = name; fnLabel.classList.remove("d-none"); }
        else { fnLabel.textContent = ""; fnLabel.classList.add("d-none"); }
    }

    // Client Leave — just disconnect and go home (no End, no notification).
    var leaveBtn = document.getElementById("leaveSessionBtn");
    if (leaveBtn) {
        leaveBtn.addEventListener("click", function () {
            _sessionEnded = true;          // suppress the unsaved-session warning
            window.location.href = "/";
        });
    }

    // Therapist: name the session (shared with everyone).
    if (isTherapist) {
        var setBtn  = document.getElementById("setFriendlyNameBtn");
        var modalEl = document.getElementById("friendlyNameModal");
        var input   = document.getElementById("friendlyNameInput");
        var saveBtn = document.getElementById("friendlyNameSaveBtn");
        if (setBtn && modalEl && typeof bootstrap !== "undefined") {
            setBtn.addEventListener("click", function () {
                if (input && fnLabel) input.value = fnLabel.textContent || "";
                bootstrap.Modal.getOrCreateInstance(modalEl).show();
            });
        }
        if (saveBtn) {
            saveBtn.addEventListener("click", function () {
                var name = (input ? input.value : "").trim();
                var note = document.getElementById("friendlyNameNote");
                if (note) note.classList.add("d-none");
                // No optimistic update: wait for friendly_name_set (applied) or
                // friendly_name_taken (suggest a unique alternative).
                if (socket) socket.emit("set_friendly_name", { session_id: sessionId, user_id: userId, name: name });
            });
        }
    }

    if (!socket) return;

    var fnModalEl = document.getElementById("friendlyNameModal");

    socket.on("friendly_name_set", function (data) {
        data = data || {};
        applyName(data.name || "");
        if (isTherapist && fnModalEl && typeof bootstrap !== "undefined") {
            bootstrap.Modal.getOrCreateInstance(fnModalEl).hide();   // applied → close
        }
        // Clients get a popup when the therapist (re)names the session — but not on
        // the silent sync sent to a newcomer when they join.
        if (!isTherapist && !data.silent && data.name) {
            _showSessionToast('This session is now called: ' + data.name);
        }
    });

    // Name already taken — suggest a unique one; the therapist accepts or edits.
    socket.on("friendly_name_taken", function (data) {
        data = data || {};
        var note = document.getElementById("friendlyNameNote");
        var inp  = document.getElementById("friendlyNameInput");
        if (note) {
            note.textContent = '"' + (data.name || "") + '" is already taken. Try "'
                + (data.suggestion || "") + '", or pick another — then Save.';
            note.classList.remove("d-none");
        }
        if (inp && data.suggestion) { inp.value = data.suggestion; inp.focus(); inp.select(); }
    });

    socket.on("session_ended", function () {
        _showSessionEndedOverlay();
    });

    // Waiting room: held out until the clinician is present; admitted on arrival.
    socket.on("waiting_room", function (data) {
        data = data || {};
        _showWaitingRoomOverlay(data.message);
    });
    socket.on("session_open", function () {
        window.location.reload();   // re-join now that the clinician is present
    });
}

// ---------------------------------------------------------------------------
// Display name — prompt, banner update, rename
// ---------------------------------------------------------------------------

/**
 * Silently accept the server-assigned default name without showing a modal.
 * For solo: AJAX POST to /api/display-name with the default name.
 * For couple/group: emits set_display_name via socket.
 */
function _acceptDefaultName(sessionId, userId, defaultName, isSolo) {
    if (!defaultName) { return; }
    if (isSolo) {
        fetch("/api/display-name", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: sessionId, display_name: defaultName }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.display_name) {
                _currentDisplayName = data.display_name;
                _updateDisplayNameBanner(sessionId, data.display_name);
            }
        })
        .catch(function () { /* non-critical — banner stays as default */ });
    } else {
        if (socket && socket.connected) {
            socket.emit("set_display_name", {
                session_id: sessionId,
                user_id: userId,
                display_name: defaultName,
            });
        }
    }
}

/**
 * Show the display name prompt modal.
 * For couple/group: emits set_display_name via socket.
 * For solo: AJAX POST to /api/display-name.
 *
 * @param {string}  sessionId   - Session ID
 * @param {string}  userId      - Current user's UUID
 * @param {string}  defaultName - Pre-filled default (e.g. "Partner1")
 * @param {boolean} isSolo      - True for solo (uses AJAX), false for socket
 */
function _showDisplayNamePrompt(sessionId, userId, defaultName, isSolo) {
    var modalEl  = document.getElementById("displayNameModal");
    var input    = document.getElementById("displayNameInput");
    var errorEl  = document.getElementById("displayNameError");
    var confirmBtn = document.getElementById("displayNameConfirmBtn");
    if (!modalEl) { return; }

    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    input.value = defaultName;
    errorEl.classList.add("d-none");

    function _doConfirm() {
        var name = input.value.trim() || defaultName;
        errorEl.classList.add("d-none");

        if (isSolo) {
            fetch("/api/display-name", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sessionId, display_name: name }),
            })
            .then(function (r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function (result) {
                if (result.ok) {
                    _currentDisplayName = result.data.display_name;
                    _updateDisplayNameBanner(sessionId, result.data.display_name);
                    modal.hide();
                    // Re-label any existing server-rendered bubbles for this user
                    var wrappers = document.querySelectorAll('[data-user-id="' + userId + '"]');
                    wrappers.forEach(function (w) {
                        var lbl = w.querySelector(".bubble-sender");
                        if (lbl) { lbl.textContent = sessionId + "-" + result.data.display_name; }
                    });
                } else {
                    errorEl.textContent = result.data.error || "Could not set name. Please try again.";
                    errorEl.classList.remove("d-none");
                }
            })
            .catch(function () {
                errorEl.textContent = "Network error. Please try again.";
                errorEl.classList.remove("d-none");
            });
        } else {
            // couple / group — use socket
            if (socket && socket.connected) {
                socket.emit("set_display_name", {
                    session_id: sessionId,
                    user_id: userId,
                    display_name: name,
                });
                // name_set / name_error handlers in joinRoom() will close modal or show error
                socket.once("name_set", function (data) {
                    if (data.user_id === userId) { modal.hide(); }
                });
            }
        }
    }

    // Remove previous listener before adding new one (prevent duplicate fires)
    var newBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
    newBtn.addEventListener("click", _doConfirm);

    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); _doConfirm(); }
    });

    modal.show();
    setTimeout(function () { input.focus(); input.select(); }, 300);
}

/**
 * Update the display name label in the session banner.
 * @param {string} sessionId
 * @param {string} displayName
 */
function _updateDisplayNameBanner(sessionId, displayName) {
    var el = document.getElementById("displayNameLabel");
    if (el) {
        el.textContent = sessionId + "-" + displayName;
    }
}

/**
 * Wire up the rename display name button in the session banner.
 * For couple/group: emits rename via socket.
 * For solo: AJAX POST to /api/display-name.
 *
 * @param {string}  sessionId
 * @param {string}  userId
 * @param {boolean} isSolo
 */
function initDisplayNameRename(sessionId, userId, isSolo) {
    var btn      = document.getElementById("renameDisplayNameBtn");
    var modalEl  = document.getElementById("renameDisplayNameModal");
    var input    = document.getElementById("renameInput");
    var errorEl  = document.getElementById("renameError");
    var confirmBtn = document.getElementById("renameConfirmBtn");
    if (!btn || !modalEl) { return; }

    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    btn.addEventListener("click", function () {
        input.value = _currentDisplayName || "";
        errorEl.classList.add("d-none");
        modal.show();
        setTimeout(function () { input.focus(); input.select(); }, 300);
    });

    function _doRename() {
        var newName = input.value.trim();
        if (!newName) { return; }
        errorEl.classList.add("d-none");

        if (isSolo) {
            fetch("/api/display-name", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ session_id: sessionId, display_name: newName }),
            })
            .then(function (r) { return r.json().then(function(d) { return { ok: r.ok, data: d }; }); })
            .then(function (result) {
                if (result.ok) {
                    var oldName = _currentDisplayName;
                    _currentDisplayName = result.data.display_name;
                    _updateDisplayNameBanner(sessionId, result.data.display_name);
                    modal.hide();
                    // Re-label solo bubbles
                    var wrappers = document.querySelectorAll('[data-user-id="' + userId + '"]');
                    wrappers.forEach(function (w) {
                        var lbl = w.querySelector(".bubble-sender");
                        if (lbl) { lbl.textContent = sessionId + "-" + result.data.display_name; }
                    });
                    // System notice
                    var chatBox = document.getElementById("chatBox");
                    if (chatBox && oldName) {
                        var notice = document.createElement("div");
                        notice.className = "text-center text-muted small py-1 fst-italic";
                        notice.textContent = sessionId + "-" + oldName + " is now known as " +
                                             sessionId + "-" + result.data.display_name;
                        chatBox.appendChild(notice);
                        scrollToBottom(chatBox);
                    }
                } else {
                    errorEl.textContent = result.data.error || "Could not rename. Please try again.";
                    errorEl.classList.remove("d-none");
                }
            })
            .catch(function () {
                errorEl.textContent = "Network error. Please try again.";
                errorEl.classList.remove("d-none");
            });
        } else {
            if (socket && socket.connected) {
                socket.emit("rename", {
                    session_id: sessionId,
                    user_id: userId,
                    new_name: newName,
                });
                socket.once("name_changed", function () { modal.hide(); });
                socket.once("name_error", function (data) {
                    errorEl.textContent = data.message;
                    errorEl.classList.remove("d-none");
                });
            }
        }
    }

    confirmBtn.addEventListener("click", _doRename);
    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); _doRename(); }
    });
}

// ---------------------------------------------------------------------------
// Inline session nickname — pencil button in the session ID banner
// ---------------------------------------------------------------------------

/**
 * Wire up the inline rename button in the session ID banner.
 * Saves the friendly name to localStorage under session_nickname_{sessionId}.
 * @param {string} sessionId
 */
function initSessionNickname(sessionId) {
    var storageKey = "session_nickname_" + sessionId;
    var renameBtn  = document.getElementById("renameSessionBtn");
    var input      = document.getElementById("sessionNicknameInline");
    var display    = document.getElementById("sessionNicknameDisplay");

    if (!renameBtn || !input || !display) { return; }

    // Restore saved name on page load — shown inline as  CODE · "Nickname"
    var saved = localStorage.getItem(storageKey);
    if (saved) {
        display.textContent = " \u00B7 \u201C" + saved + "\u201D";
        input.value = saved;
    }

    // Pencil button: show input for editing
    renameBtn.addEventListener("click", function () {
        if (input.classList.contains("d-none")) {
            input.classList.remove("d-none");
            display.textContent = "";   // clear while typing; code span stays visible
            input.focus();
            input.select();
        } else {
            _saveNickname();
        }
    });

    // Save on Enter key; cancel on Escape
    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter")  { e.preventDefault(); _saveNickname(); }
        if (e.key === "Escape") {
            input.classList.add("d-none");
            var prev = localStorage.getItem(storageKey);
            display.textContent = prev ? " \u00B7 \u201C" + prev + "\u201D" : "";
        }
    });

    // Save on blur (clicking away)
    input.addEventListener("blur", function () { _saveNickname(); });

    function _saveNickname() {
        var val = input.value.trim();
        if (val) {
            localStorage.setItem(storageKey, val);
            display.textContent = " \u00B7 \u201C" + val + "\u201D";
        } else {
            localStorage.removeItem(storageKey);
            display.textContent = "";
        }
        input.classList.add("d-none");
    }
}


// ---------------------------------------------------------------------------
// Participant presence panel helpers
// ---------------------------------------------------------------------------

/**
 * Update the participant count badge if present on the page.
 * @param {Array<string>} participants - List of user IDs currently in the room
 */
function _updateParticipantPanel(participants) {
    var badge = document.getElementById("participantCount");
    if (badge) {
        badge.textContent = participants.length;
    }
}

/**
 * Adjust participant count badge by a delta (+1 join, -1 leave).
 * @param {number} delta
 */
function _updateParticipantCount(delta) {
    var badge = document.getElementById("participantCount");
    if (badge) {
        var current = parseInt(badge.textContent, 10) || 0;
        badge.textContent = Math.max(0, current + delta);
    }
}

