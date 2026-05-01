// Browser-native Speech-to-Text via the Web Speech API.
// English-only by design — see ai_therapist.py system prompt for rationale.
//
// Public API:
//   window.initVoiceInput({ inputId })
//
// The page must contain:
//   <button id="voiceBtn">…</button>     — toggle mic
//   <div    id="voiceIndicator">…</div>  — "Listening…" label, hidden by default

(function () {
    "use strict";

    var MAX_RECORDING_MS = 90 * 1000;
    var DISCLOSURE_KEY = "voiceDisclosureSeen_v1";
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;

    // Inject visual styles once per document.
    if (!document.getElementById("voice-styles")) {
        var style = document.createElement("style");
        style.id = "voice-styles";
        style.textContent =
            ".voice-recording{background-color:#dc3545!important;color:#fff!important;border-color:#dc3545!important;animation:voice-pulse 1.2s ease-in-out infinite}" +
            "@keyframes voice-pulse{0%,100%{opacity:1}50%{opacity:.55}}" +
            "#voiceIndicator{font-size:.85rem}";
        document.head.appendChild(style);
    }

    window.initVoiceInput = function (opts) {
        var inputEl = document.getElementById(opts.inputId);
        var btn = document.getElementById("voiceBtn");
        var indicator = document.getElementById("voiceIndicator");
        if (!inputEl || !btn) return;

        // No browser support — hide the mic UI silently and bail.
        if (!SR) {
            btn.remove();
            if (indicator) indicator.remove();
            return;
        }

        var recognition = null;
        var isRecording = false;
        var stopTimer = null;

        function setIdleUi() {
            btn.classList.remove("voice-recording");
            btn.title = "Tap to record";
            if (indicator) indicator.classList.add("d-none");
        }

        function setRecordingUi() {
            btn.classList.add("voice-recording");
            btn.title = "Tap to stop";
            if (indicator) indicator.classList.remove("d-none");
        }

        function appendTranscript(text) {
            text = (text || "").trim();
            if (!text) return;
            var current = inputEl.value;
            if (current && !/\s$/.test(current)) {
                inputEl.value = current + " " + text;
            } else {
                inputEl.value = current + text;
            }
            inputEl.dispatchEvent(new Event("input", { bubbles: true }));
        }

        function stopRecording() {
            if (stopTimer) {
                clearTimeout(stopTimer);
                stopTimer = null;
            }
            if (recognition) {
                try { recognition.stop(); } catch (e) { /* already stopped */ }
            }
            // setIdleUi runs from recognition.onend.
        }

        function startRecording() {
            try {
                recognition = new SR();
            } catch (e) {
                console.error("Speech recognition init failed:", e);
                return;
            }
            recognition.lang = "en-US";
            recognition.continuous = true;
            recognition.interimResults = true;

            recognition.onresult = function (event) {
                var finalText = "";
                for (var i = event.resultIndex; i < event.results.length; i++) {
                    if (event.results[i].isFinal) {
                        finalText += event.results[i][0].transcript;
                    }
                }
                if (finalText) appendTranscript(finalText);
            };

            recognition.onerror = function (event) {
                console.warn("Speech recognition error:", event.error);
            };

            recognition.onend = function () {
                isRecording = false;
                recognition = null;
                if (stopTimer) {
                    clearTimeout(stopTimer);
                    stopTimer = null;
                }
                setIdleUi();
            };

            try {
                recognition.start();
                isRecording = true;
                setRecordingUi();
                stopTimer = setTimeout(stopRecording, MAX_RECORDING_MS);
            } catch (e) {
                console.error("Speech recognition start failed:", e);
                isRecording = false;
                setIdleUi();
            }
        }

        function showDisclosureModal(onAccept) {
            var existing = document.getElementById("voiceDisclosureModal");
            if (existing) existing.remove();

            var html =
                '<div class="modal fade" id="voiceDisclosureModal" tabindex="-1" aria-hidden="true">' +
                '  <div class="modal-dialog modal-dialog-centered">' +
                '    <div class="modal-content rounded-4">' +
                '      <div class="modal-header">' +
                '        <h5 class="modal-title"><i class="bi bi-mic-fill me-2"></i>Speech recognition note</h5>' +
                '        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
                '      </div>' +
                '      <div class="modal-body small">' +
                '        <p class="mb-2">Your microphone audio is processed by your browser&rsquo;s built-in speech recognition:</p>' +
                '        <ul class="mb-2">' +
                '          <li><strong>Chrome / Edge / Android:</strong> audio is sent to Google or Microsoft servers for transcription.</li>' +
                '          <li><strong>Safari (Mac / iPhone):</strong> audio stays on your device.</li>' +
                '        </ul>' +
                '        <p class="mb-2">The transcript appears in the message box. <strong>Review it before sending.</strong></p>' +
                '        <p class="mb-0 text-muted">English only. Speech in other languages will not transcribe correctly and the AI will reply in English asking you to switch.</p>' +
                '      </div>' +
                '      <div class="modal-footer">' +
                '        <button type="button" class="btn btn-outline-secondary rounded-pill" data-bs-dismiss="modal">Cancel</button>' +
                '        <button type="button" class="btn btn-primary-green rounded-pill" id="voiceDisclosureAccept">Got it &mdash; start recording</button>' +
                '      </div>' +
                '    </div>' +
                '  </div>' +
                '</div>';

            var wrapper = document.createElement("div");
            wrapper.innerHTML = html;
            document.body.appendChild(wrapper.firstChild);

            var modalEl = document.getElementById("voiceDisclosureModal");
            var modal = new bootstrap.Modal(modalEl);

            document.getElementById("voiceDisclosureAccept").addEventListener("click", function () {
                try { localStorage.setItem(DISCLOSURE_KEY, "1"); } catch (e) {}
                modal.hide();
                onAccept();
            });

            modalEl.addEventListener("hidden.bs.modal", function () {
                modalEl.remove();
            });

            modal.show();
        }

        btn.addEventListener("click", function (e) {
            e.preventDefault();
            if (isRecording) {
                stopRecording();
                return;
            }
            var seen = false;
            try { seen = !!localStorage.getItem(DISCLOSURE_KEY); } catch (e) {}
            if (seen) {
                startRecording();
            } else {
                showDisclosureModal(startRecording);
            }
        });

        setIdleUi();
    };
})();
