// Voice input via MediaRecorder + OpenAI Whisper translate.
// Audio recorded in any language -> English text appended to #messageInput.
// Crisis detection runs on the English transcript via the existing pipeline.
//
// Public API:
//   window.initVoiceInput({ inputId })
//
// Page must contain:
//   <button id="voiceBtn">      — toggle mic
//   <div    id="voiceIndicator">— recording / transcribing / cap label

(function () {
    "use strict";

    var MAX_RECORDING_MS = 90 * 1000;
    var DISCLOSURE_KEY = "voiceDisclosureSeen_v2";
    var TRANSLATE_ENDPOINT = "/api/voice/translate";

    var hasMediaRecorder =
        typeof MediaRecorder !== "undefined" &&
        navigator.mediaDevices &&
        typeof navigator.mediaDevices.getUserMedia === "function";

    if (!document.getElementById("voice-styles")) {
        var style = document.createElement("style");
        style.id = "voice-styles";
        style.textContent =
            ".voice-recording{background-color:#dc3545!important;color:#fff!important;border-color:#dc3545!important;animation:voice-pulse 1.2s ease-in-out infinite}" +
            ".voice-uploading{background-color:#6c757d!important;color:#fff!important;border-color:#6c757d!important}" +
            "@keyframes voice-pulse{0%,100%{opacity:1}50%{opacity:.55}}" +
            "#voiceIndicator{font-size:.85rem}";
        document.head.appendChild(style);
    }

    window.initVoiceInput = function (opts) {
        var inputEl = document.getElementById(opts.inputId);
        var btn = document.getElementById("voiceBtn");
        var indicator = document.getElementById("voiceIndicator");
        if (!inputEl || !btn) return;

        if (!hasMediaRecorder) {
            btn.remove();
            if (indicator) indicator.remove();
            return;
        }

        var recorder = null;
        var chunks = [];
        var mediaStream = null;
        var isRecording = false;
        var isUploading = false;
        var stopTimer = null;
        var capExceeded = false;

        function setIndicator(html, mutedNotDanger) {
            if (!indicator) return;
            indicator.innerHTML = html;
            if (mutedNotDanger) {
                indicator.classList.remove("text-danger");
                indicator.classList.add("text-muted");
            } else {
                indicator.classList.remove("text-muted");
                indicator.classList.add("text-danger");
            }
            indicator.classList.remove("d-none");
        }

        function hideIndicator() {
            if (indicator) indicator.classList.add("d-none");
        }

        function setIdleUi() {
            btn.classList.remove("voice-recording", "voice-uploading");
            btn.disabled = false;
            btn.title = "Tap to record";
            hideIndicator();
        }

        function setRecordingUi() {
            btn.classList.remove("voice-uploading");
            btn.classList.add("voice-recording");
            btn.disabled = false;
            btn.title = "Tap to stop";
            setIndicator(
                '<i class="bi bi-record-circle me-1"></i>Listening… (tap mic again to stop)',
                false
            );
        }

        function setUploadingUi() {
            btn.classList.remove("voice-recording");
            btn.classList.add("voice-uploading");
            btn.disabled = true;
            btn.title = "Transcribing…";
            setIndicator(
                '<i class="bi bi-arrow-up-circle me-1"></i>Transcribing audio…',
                true
            );
        }

        function setCapExceededUi(message) {
            capExceeded = true;
            btn.classList.remove("voice-recording", "voice-uploading");
            btn.disabled = true;
            btn.title = message;
            setIndicator(
                '<i class="bi bi-exclamation-circle me-1"></i>' + message,
                true
            );
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

        function uploadBlob(blob) {
            isUploading = true;
            setUploadingUi();
            var fd = new FormData();
            fd.append("audio", blob, "recording");

            return fetch(TRANSLATE_ENDPOINT, {
                method: "POST",
                body: fd,
                credentials: "same-origin",
            }).then(function (res) {
                if (res.status === 429) {
                    return res.json().then(function (j) {
                        setCapExceededUi(
                            j && j.message
                                ? j.message
                                : "Voice limit reached for today. You can keep typing."
                        );
                        return null;
                    });
                }
                if (!res.ok) {
                    return res.text().then(function (t) {
                        console.error("Voice translate failed:", res.status, t);
                        return null;
                    });
                }
                return res.json();
            }).then(function (data) {
                isUploading = false;
                if (data && data.text) {
                    appendTranscript(data.text);
                }
                if (!capExceeded) setIdleUi();
            }).catch(function (err) {
                isUploading = false;
                console.error("Voice upload error:", err);
                if (!capExceeded) setIdleUi();
            });
        }

        function stopRecording() {
            if (stopTimer) {
                clearTimeout(stopTimer);
                stopTimer = null;
            }
            if (recorder && recorder.state !== "inactive") {
                try { recorder.stop(); } catch (e) { /* already stopped */ }
            }
            // recorder.onstop fires the upload.
        }

        function startRecording() {
            if (capExceeded || isUploading || isRecording) return;
            navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
                mediaStream = stream;
                chunks = [];
                try {
                    recorder = new MediaRecorder(stream);
                } catch (e) {
                    console.error("MediaRecorder init failed:", e);
                    stream.getTracks().forEach(function (t) { t.stop(); });
                    setIdleUi();
                    return;
                }

                recorder.ondataavailable = function (e) {
                    if (e.data && e.data.size > 0) chunks.push(e.data);
                };

                recorder.onstop = function () {
                    isRecording = false;
                    if (mediaStream) {
                        mediaStream.getTracks().forEach(function (t) { t.stop(); });
                        mediaStream = null;
                    }
                    if (chunks.length === 0) {
                        setIdleUi();
                        return;
                    }
                    var blob = new Blob(chunks, {
                        type: (recorder && recorder.mimeType) || "audio/webm",
                    });
                    chunks = [];
                    uploadBlob(blob);
                };

                recorder.onerror = function (e) {
                    console.warn("MediaRecorder error:", e);
                };

                recorder.start();
                isRecording = true;
                setRecordingUi();
                stopTimer = setTimeout(stopRecording, MAX_RECORDING_MS);
            }).catch(function (err) {
                console.error("Microphone permission denied or unavailable:", err);
                setIdleUi();
            });
        }

        function showDisclosureModal(onAccept) {
            var existing = document.getElementById("voiceDisclosureModal");
            if (existing) existing.remove();

            var html =
                '<div class="modal fade" id="voiceDisclosureModal" tabindex="-1" aria-hidden="true">' +
                '  <div class="modal-dialog modal-dialog-centered">' +
                '    <div class="modal-content rounded-4">' +
                '      <div class="modal-header">' +
                '        <h5 class="modal-title"><i class="bi bi-mic-fill me-2"></i>Voice input — privacy &amp; usage</h5>' +
                '        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
                '      </div>' +
                '      <div class="modal-body small">' +
                '        <p class="mb-2"><strong>How it works.</strong> Your microphone audio is uploaded to OpenAI for transcription and translation to English. The English transcript appears in the message box. <strong>Review it before sending.</strong></p>' +
                '        <p class="mb-2"><strong>Languages.</strong> Speak in any language &mdash; the transcript is always English, and the AI replies in English. Quality is highest for major world languages.</p>' +
                '        <p class="mb-2"><strong>Daily limit.</strong> Up to 4 hours of voice per user per day. Typing always works without limit.</p>' +
                '        <p class="mb-0 text-muted">Audio is sent to OpenAI for processing and is not stored on our servers. By continuing you accept that audio leaves your device for transcription.</p>' +
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
            if (capExceeded || isUploading) return;
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
