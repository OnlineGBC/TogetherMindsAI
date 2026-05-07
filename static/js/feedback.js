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
            });
        });
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

    // -------- End-of-session modal -----------------------------------------

    window.initFeedbackModal = function (opts) {
        var modalEl = document.getElementById("feedbackModal");
        if (!modalEl) return;

        var statusEl = document.getElementById("feedbackModalStatus");
        var skipBtn = document.getElementById("feedbackSkipBtn");
        var submitBtn = document.getElementById("feedbackModalSubmitBtn");
        var confirmEndBtn = document.getElementById("endSessionConfirmBtn");
        if (!confirmEndBtn) return;

        var state = { rating: null, wouldPay: null, naCheckbox: null };
        var fields = {
            whatWorked: document.getElementById("whatWorkedModal"),
            whatToImprove: document.getElementById("whatToImproveModal"),
            desiredFeatures: document.getElementById("desiredFeaturesModal"),
            other: document.getElementById("otherFeedbackModal"),
        };

        bindRatingButtons(document.getElementById("ratingButtonsModal"), state);
        bindPayButtons(document.getElementById("payButtonsModal"), state);

        var modal = new bootstrap.Modal(modalEl);

        function redirect() {
            window._sessionEnded = true;
            window.location.href = opts.redirectUrl;
        }

        // Intercept End Session confirm — capture phase so we run before
        // initEndSessionGuard's listener does its redirect.
        confirmEndBtn.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopImmediatePropagation();

            // Persist nickname (mirroring the original confirm handler)
            var nicknameInput = document.getElementById("endSessionNickname");
            if (nicknameInput && nicknameInput.value.trim()) {
                try {
                    localStorage.setItem("session_nickname_" + (window.SESSION_ID || ""), nicknameInput.value.trim());
                } catch (err) {}
            }

            // Hide the End Session modal, show the feedback modal
            var endModal = bootstrap.Modal.getInstance(document.getElementById("endSessionModal"));
            if (endModal) endModal.hide();
            clearStatus(statusEl);
            modal.show();
        }, true);

        skipBtn.addEventListener("click", function () {
            redirect();
        });

        submitBtn.addEventListener("click", function () {
            clearStatus(statusEl);
            var payload = buildPayload({ fields: fields, mode: opts.mode }, state);
            if (!hasAnyContent(payload)) {
                redirect();
                return;
            }

            submitBtn.disabled = true;
            skipBtn.disabled = true;
            submitBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Sending…';

            submitFeedback(payload).then(function (r) {
                if (r.ok) {
                    setStatus(statusEl, "success", "Thank you — sending you on your way.");
                    setTimeout(redirect, 900);
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
