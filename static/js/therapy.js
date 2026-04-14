/**
 * therapy.js
 * SocketIO client logic shared by couple.html and group.html
 */

var socket = null;
var _currentUserId = null;

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

/**
 * Establish a SocketIO connection and join a therapy room.
 * @param {string} sessionId - The room / session identifier
 * @param {string} userId    - The current user's UUID
 * @param {string} mode      - Therapy mode: "couple" or "group"
 */
function joinRoom(sessionId, userId, mode) {
    _currentUserId = userId;

    socket = io({
        // Use polling only. werkzeug (dev server) crashes with
        // "write() before start_response" when a WebSocket upgrade is
        // attempted. Long-polling is functionally identical for therapy
        // chat and works correctly under both werkzeug and eventlet.
        transports: ["polling"],
        upgrade: false,
    });

    socket.on("connect", function () {
        socket.emit("join", { session_id: sessionId, user_id: userId, mode: mode || "solo" });
    });

    socket.on("history", function (data) {
        var messages = data.messages || [];
        var chatBox = document.getElementById("chatBox");

        if (messages.length > 0) {
            // Clear the "connecting…" empty state
            chatBox.innerHTML = "";
            messages.forEach(function (msg) {
                appendMessage(chatBox, msg.user_id, msg.text, msg.timestamp, userId);
            });
        }
        scrollToBottom(chatBox);
    });

    socket.on("new_message", function (data) {
        var chatBox = document.getElementById("chatBox");

        // Remove empty state if present
        var emptyState = document.getElementById("emptyState");
        if (emptyState) {
            emptyState.remove();
        }

        appendMessage(chatBox, data.user_id, data.text, data.timestamp, _currentUserId);
        scrollToBottom(chatBox);

        // Restore send button once the AI reply arrives
        if (data.user_id === "AI") {
            _hideSendSpinner();
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
 * @param {HTMLElement} chatBox   - The scrollable chat container
 * @param {string}      senderId  - User ID of the sender ("AI" or UUID)
 * @param {string}      text      - Message text
 * @param {string}      timestamp - Formatted timestamp string
 * @param {string}      myUserId  - The current viewer's user ID (to decide alignment)
 */
function appendMessage(chatBox, senderId, text, timestamp, myUserId) {
    var isAI   = senderId === "AI";
    var isMe   = !isAI && senderId === myUserId;

    var wrapper = document.createElement("div");
    wrapper.className = "d-flex mb-3 align-items-end" + (isMe ? " justify-content-end" : "");

    var senderLabel = isAI ? "AI Guide" : (isMe ? "You" : "Partner");

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
        var avatarClass = isAI ? "avatar-ai" : "avatar-other";
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
function initEndSessionGuard(sessionId, redirectUrl) {
    var _storageKey = "session_nickname_" + sessionId;

    // Populate modal display
    var display = document.getElementById("endSessionIdDisplay");
    if (display) {
        display.textContent = sessionId;
    }

    // Restore any previously saved nickname
    var nicknameInput = document.getElementById("endSessionNickname");
    if (nicknameInput) {
        var saved = localStorage.getItem(_storageKey);
        if (saved) { nicknameInput.value = saved; }
        nicknameInput.addEventListener("input", function () {
            var val = nicknameInput.value.trim();
            if (val) {
                localStorage.setItem(_storageKey, val);
            } else {
                localStorage.removeItem(_storageKey);
            }
        });
    }

    // Copy button inside modal
    var copyBtn = document.getElementById("endSessionCopyBtn");
    if (copyBtn) {
        copyBtn.addEventListener("click", function () {
            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(sessionId).then(function () {
                    copyBtn.innerHTML = '<i class="bi bi-clipboard-check"></i>';
                    setTimeout(function () {
                        copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>';
                    }, 1500);
                });
            } else {
                var el = document.createElement("textarea");
                el.value = sessionId;
                document.body.appendChild(el);
                el.select();
                document.execCommand("copy");
                document.body.removeChild(el);
            }
        });
    }

    // Confirm button — save nickname then redirect (blocks on 409)
    var confirmBtn = document.getElementById("endSessionConfirmBtn");
    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            var nicknameVal = nicknameInput ? nicknameInput.value.trim() : "";

            // Clear any previous modal nickname error
            var existingErr = document.getElementById("endSessionNicknameError");
            if (existingErr) { existingErr.remove(); }

            if (!nicknameVal) {
                // No nickname — proceed straight to redirect
                _sessionEnded = true;
                window.location.href = redirectUrl;
                return;
            }

            // Save nickname first; only redirect if the server accepts it
            _persistNicknameToServer(sessionId, nicknameVal).then(function () {
                localStorage.setItem(_storageKey, nicknameVal);
                _sessionEnded = true;
                window.location.href = redirectUrl;
            }).catch(function (err) {
                // Show error inside the modal; keep modal open for retry
                var errEl = document.createElement("div");
                errEl.id = "endSessionNicknameError";
                errEl.className = "text-danger small mt-2";
                errEl.textContent = err.message || "Could not save name — please try another.";
                nicknameInput.insertAdjacentElement("afterend", errEl);
                nicknameInput.focus();
                nicknameInput.select();
            });
        });
    }

    // Delete session button — removes all server-side data then goes home
    var deleteBtn = document.getElementById("endSessionDeleteBtn");
    if (deleteBtn) {
        deleteBtn.addEventListener("click", function () {
            if (!confirm("This will permanently delete all messages in this session. Are you sure?")) {
                return;
            }
            deleteBtn.disabled = true;
            deleteBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Deleting…';
            fetch("/session/" + sessionId + "/delete", { method: "POST" })
                .then(function () {
                    localStorage.removeItem(_storageKey);
                    _sessionEnded = true;
                    window.location.href = "/";
                })
                .catch(function () {
                    deleteBtn.disabled = false;
                    deleteBtn.innerHTML = '<i class="bi bi-trash me-1"></i>Delete session and exit';
                    alert("Could not delete the session. Please try again.");
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

        // Clear any previous inline error
        var existingErr = document.getElementById("nicknameInlineError");
        if (existingErr) { existingErr.remove(); }

        if (!val) {
            localStorage.removeItem(storageKey);
            display.textContent = "";
            input.classList.add("d-none");
            _persistNicknameToServer(sessionId, "");
            return;
        }

        // Persist server-side; wait for response before committing locally
        _persistNicknameToServer(sessionId, val).then(function () {
            localStorage.setItem(storageKey, val);
            display.textContent = " \u00B7 \u201C" + val + "\u201D";
            input.classList.add("d-none");
        }).catch(function (err) {
            // Show inline error beneath the input; keep input open for retry
            var errEl = document.createElement("div");
            errEl.id = "nicknameInlineError";
            errEl.className = "text-danger small mt-1";
            errEl.textContent = err.message || "Could not save — please try another name.";
            input.insertAdjacentElement("afterend", errEl);
            input.focus();
            input.select();
        });
    }
}

/**
 * POST the nickname to the server.
 * Returns a Promise that resolves with {ok: true} on success or
 * rejects with an Error whose message is the server's error string on failure.
 */
function _persistNicknameToServer(sessionId, nickname) {
    return fetch("/session/" + sessionId + "/nickname", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname: nickname }),
    }).then(function (res) {
        return res.json().then(function (body) {
            if (!res.ok) {
                throw new Error(body.error || "Could not save name (" + res.status + ")");
            }
            return body;
        });
    });
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
