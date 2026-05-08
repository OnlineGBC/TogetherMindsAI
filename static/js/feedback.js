// Feedback form — standalone page + end-of-session modal.
// Sends to POST /api/feedback. No PII captured client-side.
//
// Public API:
//   window.initFeedbackForm({ formId, mode, redirectOnSuccess })
//   window.initFeedbackModal({ mode, redirectUrl })  — wraps endSession confirm

(function () {
    "use strict";

    var ENDPOINT = "/api/feedback";

    function detectPlatform() {
        try {
            if (document.referrer && document.referrer.indexOf("android-app://") === 0) {
                return "android_twa";
            }
        } catch (e) {}
        try {
            if (window.navigator && window.navigator.standalone === true) {
                return "ios_pwa";
            }
        } catch (e) {}
        var ua = navigator.userAgent || "";
        if (/Mobi|Android|iPhone|iPad/.test(ua)) {
            return "mobile_browser";
        }
        return "web";
    }

    function detectOS() {
        var ua = navigator.userAgent || "";
        if (/Android/i.test(ua)) return "android";
        if (/iPhone|iPad|iPod/i.test(ua)) return "ios";
        // Modern iPads report as "Macintosh" — disambiguate via touch points.
        if (/Macintosh/i.test(ua) && (navigator.maxTouchPoints || 0) > 1) return "ios";
        if (/Mac OS X|Macintosh/i.test(ua)) return "macos";
        if (/Windows/i.test(ua)) return "windows";
        if (/Linux|X11|CrOS/i.test(ua)) return "linux";
        return "unknown";
    }

    function bindRatingButtons(container, state) {
        if (!container) return;
        var btns = container.querySelectorAll(".rating-btn");
        var naBtn = container.querySelector(".rating-na-btn");
        btns.forEach(function (b) {
            b.addEventListener("click", function () {
                state.rating = parseInt(b.getAttribute("data-rating"), 10);
                btns.forEach(function (other) {
                    other.classList.remove("btn-primary-green", "active");
                    other.classList.add("btn-outline-secondary");
                });
                b.classList.remove("btn-outline-secondary");
                b.classList.add("btn-primary-green", "active");
                if (state.naCheckbox) state.naCheckbox.checked = false;
                if (naBtn) {
                    naBtn.classList.remove("btn-primary-green", "active");
                    naBtn.classList.add("btn-outline-secondary");
                }
            });
        });
        if (naBtn) {
            naBtn.addEventListener("click", function () {
                if (!state.naCheckbox) state.naCheckbox = { checked: false };
                state.naCheckbox.checked = !state.naCheckbox.checked;
                if (state.naCheckbox.checked) {
                    state.rating = null;
                    btns.forEach(function (other) {
                        other.classList.remove("btn-primary-green", "active");
                        other.classList.add("btn-outline-secondary");
                    });
                    naBtn.classList.remove("btn-outline-secondary");
                    naBtn.classList.add("btn-primary-green", "active");
                } else {
                    naBtn.classList.remove("btn-primary-green", "active");
                    naBtn.classList.add("btn-outline-secondary");
                }
            });
        }
    }

    function bindPayButtons(container, state) {
        if (!container) return;
        var btns = container.querySelectorAll(".pay-btn");
        btns.forEach(function (b) {
            b.addEventListener("click", function () {
                var current = b.getAttribute("data-pay");
                if (state.wouldPay === current) {
                    state.wouldPay = null;
                    b.classList.remove("btn-primary-green", "active");
                    b.classList.add("btn-outline-secondary");
                    return;
                }
                state.wouldPay = current;
                btns.forEach(function (other) {
                    other.classList.remove("btn-primary-green", "active");
                    other.classList.add("btn-outline-secondary");
                });
                b.classList.remove("btn-outline-secondary");
                b.classList.add("btn-primary-green", "active");
            });
        });
    }

    function bindCharCounters(root) {
        var spans = root.querySelectorAll(".char-count");
        spans.forEach(function (s) {
            var id = s.getAttribute("data-for");
            var ta = root.querySelector("#" + id);
            if (!ta) return;
            ta.addEventListener("input", function () {
                s.textContent = String(ta.value.length);
            });
        });
    }

    function setStatus(statusEl, kind, message) {
        if (!statusEl) return;
        statusEl.classList.remove("d-none", "alert-success", "alert-danger", "alert-warning");
        statusEl.classList.add("alert-" + kind);
        statusEl.textContent = message;
    }

    function clearStatus(statusEl) {
        if (!statusEl) return;
        statusEl.classList.add("d-none");
        statusEl.textContent = "";
    }

    function buildPayload(opts, state) {
        return {
            rating: (state.naCheckbox && state.naCheckbox.checked) ? null : (state.rating || null),
            what_worked: (opts.fields.whatWorked.value || "").slice(0, 1000),
            what_to_improve: (opts.fields.whatToImprove.value || "").slice(0, 1000),
            desired_features: (opts.fields.desiredFeatures.value || "").slice(0, 1000),
            would_pay: state.wouldPay || null,
            other: (opts.fields.other.value || "").slice(0, 1000),
            platform: detectPlatform(),
            os: detectOS(),
            mode: opts.mode || null,
        };
    }

    function hasAnyContent(p) {
        return (
            p.rating !== null ||
            p.would_pay !== null ||
            (p.what_worked && p.what_worked.trim()) ||
            (p.what_to_improve && p.what_to_improve.trim()) ||
            (p.desired_features && p.desired_features.trim()) ||
            (p.other && p.other.trim())
        );
    }

    function submitFeedback(payload) {
        return fetch(ENDPOINT, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        }).then(function (res) {
            return res.json().catch(function () { return {}; }).then(function (body) {
                return { ok: res.ok, status: res.status, body: body };
            });
        });
    }

    // -------- Standalone page form -----------------------------------------

    window.initFeedbackForm = function (opts) {
        var form = document.getElementById(opts.formId);
        if (!form) return;
        var statusEl = document.getElementById("feedbackStatus");
        var submitBtn = document.getElementById("feedbackSubmitBtn");

        var state = {
            rating: null,
            wouldPay: null,
            naCheckbox: document.getElementById("ratingNA"),
        };

        var fields = {
            whatWorked: document.getElementById("whatWorked"),
            whatToImprove: document.getElementById("whatToImprove"),
            desiredFeatures: document.getElementById("desiredFeatures"),
            other: document.getElementById("otherFeedback"),
        };

        bindRatingButtons(document.getElementById("ratingButtons"), state);
        bindPayButtons(document.getElementById("payButtons"), state);
        bindCharCounters(form);

        if (state.naCheckbox) {
            state.naCheckbox.addEventListener("change", function () {
                if (state.naCheckbox.checked) {
                    state.rating = null;
                    var btns = document.querySelectorAll("#ratingButtons .rating-btn");
                    btns.forEach(function (b) {
                        b.classList.remove("btn-primary-green", "active");
                        b.classList.add("btn-outline-secondary");
                    });
                }
            });
        }

        form.addEventListener("submit", function (e) {
            e.preventDefault();
            clearStatus(statusEl);

            var payload = buildPayload({ fields: fields, mode: opts.mode }, state);
            if (!hasAnyContent(payload)) {
                setStatus(statusEl, "warning", "Please fill in at least one field.");
                return;
            }

            submitBtn.disabled = true;
            submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Sending…';

            submitFeedback(payload).then(function (r) {
                if (r.ok) {
                    setStatus(statusEl, "success", "Thank you — your feedback has been sent.");
                    form.reset();
                    if (opts.redirectOnSuccess) {
                        setTimeout(function () {
                            window.location.href = opts.redirectOnSuccess;
                        }, 1500);
                    }
                } else {
                    setStatus(statusEl, "danger", (r.body && r.body.error) || "Could not send feedback. Please try again.");
                    submitBtn.disabled = false;
                    submitBtn.innerHTML = '<i class="bi bi-send-fill me-1"></i>Send feedback';
                }
            }).catch(function () {
                setStatus(statusEl, "danger", "Network error. Please try again.");
                submitBtn.disabled = false;
                submitBtn.innerHTML = '<i class="bi bi-send-fill me-1"></i>Send feedback';
            });
        });
    };

    // -------- Draggable FAB ------------------------------------------------
    //
    // Tap = open modal (existing click behavior). Drag = move the FAB; the
    // last position is persisted in localStorage and re-applied on page load,
    // clamped into the visible viewport (below navbar/disclaimer bar, above
    // the bottom edge). Touch is supported via Pointer Events.

    var FAB_POS_KEY = "feedback_fab_pos_v1";
    var FAB_DRAG_THRESHOLD_PX = 5;

    function _fabTopBoundary() {
        // Don't let the user drag the button behind the navbar / disclaimer bar.
        var bottom = 0;
        var navbar = document.querySelector(".navbar");
        var disclaimer = document.querySelector(".disclaimer-bar");
        if (navbar) bottom = Math.max(bottom, navbar.getBoundingClientRect().bottom);
        if (disclaimer) bottom = Math.max(bottom, disclaimer.getBoundingClientRect().bottom);
        return bottom + 4;
    }

    function _applyFabPos(fab, left, top) {
        var rect = fab.getBoundingClientRect();
        var w = rect.width, h = rect.height;
        var minLeft = 4;
        var maxLeft = window.innerWidth - w - 4;
        var minTop = _fabTopBoundary();
        var maxTop = window.innerHeight - h - 4;
        if (maxLeft < minLeft) maxLeft = minLeft;
        if (maxTop < minTop) maxTop = minTop;
        left = Math.max(minLeft, Math.min(left, maxLeft));
        top = Math.max(minTop, Math.min(top, maxTop));
        fab.style.left = left + "px";
        fab.style.top = top + "px";
        fab.style.right = "auto";
        fab.style.bottom = "auto";
    }

    window.makeFabDraggable = function (fab) {
        if (!fab) return;

        // Restore saved position (if any) once layout has settled.
        function restore() {
            try {
                var saved = JSON.parse(localStorage.getItem(FAB_POS_KEY) || "null");
                if (saved && typeof saved.left === "number" && typeof saved.top === "number") {
                    _applyFabPos(fab, saved.left, saved.top);
                }
            } catch (e) { /* ignore */ }
        }
        // Defer one tick so getBoundingClientRect reflects the rendered FAB size.
        setTimeout(restore, 0);

        var dragState = null;
        var didDrag = false;

        fab.addEventListener("pointerdown", function (e) {
            // Ignore non-primary buttons (right-click, etc.)
            if (e.button !== undefined && e.button !== 0) return;
            var rect = fab.getBoundingClientRect();
            dragState = {
                startX: e.clientX,
                startY: e.clientY,
                offsetX: e.clientX - rect.left,
                offsetY: e.clientY - rect.top,
                moving: false,
            };
            try { fab.setPointerCapture(e.pointerId); } catch (err) {}
        });

        fab.addEventListener("pointermove", function (e) {
            if (!dragState) return;
            var dx = e.clientX - dragState.startX;
            var dy = e.clientY - dragState.startY;
            if (!dragState.moving) {
                if (Math.abs(dx) + Math.abs(dy) < FAB_DRAG_THRESHOLD_PX) return;
                dragState.moving = true;
                didDrag = true;
                fab.classList.add("is-dragging");
            }
            _applyFabPos(fab, e.clientX - dragState.offsetX, e.clientY - dragState.offsetY);
        });

        function endDrag(e) {
            if (dragState && dragState.moving) {
                var rect = fab.getBoundingClientRect();
                try {
                    localStorage.setItem(FAB_POS_KEY, JSON.stringify({
                        left: rect.left, top: rect.top,
                    }));
                } catch (err) {}
            }
            dragState = null;
            fab.classList.remove("is-dragging");
            if (e && e.pointerId !== undefined) {
                try { fab.releasePointerCapture(e.pointerId); } catch (err) {}
            }
        }
        fab.addEventListener("pointerup", endDrag);
        fab.addEventListener("pointercancel", endDrag);

        // Capture-phase click suppressor: when the user just dragged, swallow
        // the click so it doesn't open the modal.
        fab.addEventListener("click", function (e) {
            if (didDrag) {
                didDrag = false;
                e.stopImmediatePropagation();
                e.preventDefault();
            }
        }, true);

        // Re-clamp on viewport changes so the FAB never ends up off-screen
        // after rotation, window resize, or browser-chrome show/hide.
        window.addEventListener("resize", function () {
            var rect = fab.getBoundingClientRect();
            _applyFabPos(fab, rect.left, rect.top);
        });
    };

    // -------- Unified popup modal (FAB + end-of-session) -------------------
    //
    // Three trigger paths share the SAME modal element:
    //   (a) FAB click on any page         →  no redirect on submit/close
    //   (b) End Session confirm           →  redirect to /progress/... on close
    //   (c) "Delete session and exit"     →  call delete API, then redirect to /
    //
    // state.redirectUrl tells the close path where (or whether) to navigate.
    // state.preRedirectAction is an optional async function run BEFORE the
    // redirect — used by the delete flow to call /session/<id>/delete.

    window.initFeedbackOnAllPages = function (opts) {
        var modalEl = document.getElementById("feedbackModal");
        if (!modalEl) return;

        var statusEl = document.getElementById("feedbackModalStatus");
        var skipBtn = document.getElementById("feedbackSkipBtn");
        var submitBtn = document.getElementById("feedbackModalSubmitBtn");
        var fabBtn = document.getElementById("feedbackFab");
        var confirmEndBtn = document.getElementById("endSessionConfirmBtn");

        var state = {
            rating: null,
            wouldPay: null,
            naCheckbox: { checked: false },   // fake checkbox-like for the modal's N/A button
            redirectUrl: null,                 // null = stay on page; string = redirect on close/submit
            preRedirectAction: null,           // optional Promise-returning fn run before redirect
        };
        var fields = {
            whatWorked: document.getElementById("whatWorkedModal"),
            whatToImprove: document.getElementById("whatToImproveModal"),
            desiredFeatures: document.getElementById("desiredFeaturesModal"),
            other: document.getElementById("otherFeedbackModal"),
        };

        bindRatingButtons(document.getElementById("ratingButtonsModal"), state);
        bindPayButtons(document.getElementById("payButtonsModal"), state);

        // Tooltip on the N/A rating button
        if (typeof bootstrap !== "undefined" && bootstrap.Tooltip) {
            var naTipEl = modalEl.querySelector(".rating-na-btn");
            if (naTipEl) new bootstrap.Tooltip(naTipEl);
        }

        var modal = new bootstrap.Modal(modalEl);

        function resetForm() {
            state.rating = null;
            state.wouldPay = null;
            if (state.naCheckbox) state.naCheckbox.checked = false;
            ["whatWorked", "whatToImprove", "desiredFeatures", "other"].forEach(function (k) {
                if (fields[k]) fields[k].value = "";
            });
            var ratingBtns = modalEl.querySelectorAll(".rating-btn");
            ratingBtns.forEach(function (b) {
                b.classList.remove("btn-primary-green", "active");
                b.classList.add("btn-outline-secondary");
            });
            var naBtn = modalEl.querySelector(".rating-na-btn");
            if (naBtn) {
                naBtn.classList.remove("btn-primary-green", "active");
                naBtn.classList.add("btn-outline-secondary");
            }
            var payBtns = modalEl.querySelectorAll(".pay-btn");
            payBtns.forEach(function (b) {
                b.classList.remove("btn-primary-green", "active");
                b.classList.add("btn-outline-secondary");
            });
            clearStatus(statusEl);
            submitBtn.disabled = false;
            skipBtn.disabled = false;
            submitBtn.innerHTML = '<i class="bi bi-send-fill me-1"></i>Send feedback';
        }

        function closeOrRedirect() {
            if (!state.redirectUrl) {
                modal.hide();
                return;
            }
            window._sessionEnded = true;
            var action = state.preRedirectAction;
            var url = state.redirectUrl;
            state.preRedirectAction = null;
            if (action) {
                // Run the pre-redirect work (e.g., POST /session/<id>/delete)
                // and navigate regardless of whether it succeeds — matches the
                // existing delete-button handler's lenient failure mode.
                Promise.resolve()
                    .then(action)
                    .catch(function () {})
                    .then(function () { window.location.href = url; });
            } else {
                window.location.href = url;
            }
        }

        // ---- Trigger 1: FAB click (no redirect) ---------------------------
        if (fabBtn) {
            fabBtn.addEventListener("click", function (e) {
                e.preventDefault();
                state.redirectUrl = null;
                skipBtn.textContent = "Cancel";
                resetForm();
                modal.show();
            });
        }

        // ---- Trigger 2: End Session confirm (with redirect) ---------------
        // Capture phase so this fires BEFORE initEndSessionGuard's listener.
        if (confirmEndBtn && opts.endSessionRedirectUrl) {
            confirmEndBtn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopImmediatePropagation();

                // Persist nickname (mirroring the original handler)
                var nicknameInput = document.getElementById("endSessionNickname");
                if (nicknameInput && nicknameInput.value.trim()) {
                    try {
                        localStorage.setItem("session_nickname_" + (window.SESSION_ID || ""), nicknameInput.value.trim());
                    } catch (err) {}
                }

                var endModal = bootstrap.Modal.getInstance(document.getElementById("endSessionModal"));
                if (endModal) endModal.hide();

                state.redirectUrl = opts.endSessionRedirectUrl;
                state.preRedirectAction = null;
                skipBtn.textContent = "Skip";
                resetForm();
                modal.show();
            }, true);
        }

        // ---- Trigger 3: Delete session and exit (with delete API + redirect to /) ----
        // Capture phase, same pattern as the End Session interceptor. The
        // browser confirm() prompt that the original handler shows is moved
        // here so we can branch on it before any modal work.
        var deleteEndBtn = document.getElementById("endSessionDeleteBtn");
        if (deleteEndBtn && window.SESSION_ID) {
            deleteEndBtn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopImmediatePropagation();

                if (!confirm("This will permanently delete all messages in this session. Are you sure?")) {
                    return;
                }

                var endModal = bootstrap.Modal.getInstance(document.getElementById("endSessionModal"));
                if (endModal) endModal.hide();

                var sid = window.SESSION_ID;
                state.redirectUrl = "/";
                state.preRedirectAction = function () {
                    return fetch("/session/" + sid + "/delete", { method: "POST" })
                        .then(function () {
                            try { localStorage.removeItem("session_nickname_" + sid); } catch (err) {}
                        });
                };
                skipBtn.textContent = "Skip";
                resetForm();
                modal.show();
            }, true);
        }

        // ---- Skip / Cancel button -----------------------------------------
        skipBtn.addEventListener("click", function () {
            closeOrRedirect();
        });

        // ---- Submit button ------------------------------------------------
        submitBtn.addEventListener("click", function () {
            clearStatus(statusEl);
            var payload = buildPayload({ fields: fields, mode: opts.mode }, state);
            if (!hasAnyContent(payload)) {
                closeOrRedirect();
                return;
            }

            submitBtn.disabled = true;
            skipBtn.disabled = true;
            submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Sending…';

            submitFeedback(payload).then(function (r) {
                if (r.ok) {
                    var msg = state.redirectUrl
                        ? "Thank you — sending you on your way."
                        : "Thank you — your feedback has been sent.";
                    setStatus(statusEl, "success", msg);
                    setTimeout(closeOrRedirect, 1100);
                } else {
                    setStatus(statusEl, "danger", (r.body && r.body.error) || "Could not send feedback.");
                    submitBtn.disabled = false;
                    skipBtn.disabled = false;
                    submitBtn.innerHTML = '<i class="bi bi-send-fill me-1"></i>Send feedback';
                }
            }).catch(function () {
                setStatus(statusEl, "danger", "Network error.");
                submitBtn.disabled = false;
                skipBtn.disabled = false;
                submitBtn.innerHTML = '<i class="bi bi-send-fill me-1"></i>Send feedback';
            });
        });
    };
})();
