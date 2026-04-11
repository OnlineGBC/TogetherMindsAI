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
        transports: ["websocket", "polling"],
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
    socket.emit("send_message", {
        session_id: sessionId,
        user_id: userId,
        text: text,
        mode: mode || "solo",
    });
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

    var senderLabel = isAI ? "AI Therapist" : (isMe ? "You" : "Partner");

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
    // Populate modal display
    var display = document.getElementById("endSessionIdDisplay");
    if (display) {
        display.textContent = sessionId;
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

    // Confirm button — mark ended and redirect
    var confirmBtn = document.getElementById("endSessionConfirmBtn");
    if (confirmBtn) {
        confirmBtn.addEventListener("click", function () {
            _sessionEnded = true;
            window.location.href = redirectUrl;
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
