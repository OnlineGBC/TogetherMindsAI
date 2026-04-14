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
        var messages    = data.messages    || [];
        var defaultName = data.default_name || "";

        if (soloMode) {
            // Solo messages are already server-rendered — just trigger the name prompt
            _showDisplayNamePrompt(sessionId, userId, defaultName, true);
            return;
        }

        var chatBox = document.getElementById("chatBox");
        if (messages.length > 0) {
            chatBox.innerHTML = "";
            messages.forEach(function (msg) {
                appendMessage(chatBox, msg.user_id, msg.text, msg.timestamp,
                              userId, msg.display_name, sessionId);
            });
        }
        scrollToBottom(chatBox);

        // Show name prompt for couple/group after history is rendered
        _showDisplayNamePrompt(sessionId, userId, defaultName, false);
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

        // Restore send button once the AI reply arrives
        if (data.user_id === "AI") {
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
        senderLabel = "AI Guide";
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

    // Confirm button — save nickname to localStorage then redirect
    var confirmBtn = document.getElementById("endSessionConfirmBtn");
    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            if (nicknameInput && nicknameInput.value.trim()) {
                localStorage.setItem(_storageKey, nicknameInput.value.trim());
            }
            _sessionEnded = true;
            window.location.href = redirectUrl;
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
// Display name — prompt, banner update, rename
// ---------------------------------------------------------------------------

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
    var preview  = document.getElementById("displayNamePreview");
    var errorEl  = document.getElementById("displayNameError");
    var confirmBtn = document.getElementById("displayNameConfirmBtn");
    if (!modalEl) { return; }

    var modal = bootstrap.Modal.getOrCreateInstance(modalEl);

    input.value = defaultName;
    preview.textContent = sessionId + "-" + defaultName;
    errorEl.classList.add("d-none");

    input.addEventListener("input", function () {
        var val = input.value.trim() || defaultName;
        preview.textContent = sessionId + "-" + val;
    });

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
