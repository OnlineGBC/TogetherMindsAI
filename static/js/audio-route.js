/* audio-route.js
 * Which speaker should the other person's voice play through?
 *
 * Reported from a live test (Oct 2026): a patient used a Bluetooth speaker that
 * has its own mic. Windows used that mic, which flips the speaker into "phone
 * call" (hands-free) mode. In that mode the speaker plays its own mic back to
 * itself, and its normal "music" output goes silent. We played the therapist
 * to the normal default output — so she heard herself and not the therapist.
 * Zoom, on the same hardware, worked: it plays to the call channel that goes
 * with the mic.
 *
 * So: when the mic is a Bluetooth hands-free mic, play through the output of
 * that same hands-free device. Any other mic (laptop, USB, Mac) → leave the
 * browser's default alone ("" = default).
 *
 * Pure function, no DOM: tests run it under node.
 */
(function (root) {
    // Windows names Bluetooth call-mode endpoints "... Hands-Free ..." or
    // "... Hands-Free AG Audio". Only these switch the device into call mode.
    var HANDS_FREE = /hands[\s-]?free/i;

    // "Communications - Headset (JBL Flip 5 Hands-Free)" -> "JBL Flip 5 Hands-Free".
    // The part in brackets is the device's own name; Windows uses the same name
    // for the device's input and its output.
    function deviceName(label) {
        var m = /\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$/.exec(label || "");
        return m ? m[1].trim().toLowerCase() : "";
    }

    // The "default" and "communications" entries are aliases for a real device,
    // not devices of their own.
    function isAlias(d) { return d.deviceId === "default" || d.deviceId === "communications"; }

    function pickOutput(mic, devices) {
        mic = mic || {};
        devices = devices || [];
        if (!HANDS_FREE.test(mic.label || "")) { return ""; }

        var outs = devices.filter(function (d) { return d.kind === "audiooutput"; });
        var real = outs.filter(function (d) { return !isAlias(d) && HANDS_FREE.test(d.label || ""); });

        // 1) Same device name as the mic: the output half of the same hands-free device.
        var name = deviceName(mic.label);
        if (name) {
            for (var i = 0; i < real.length; i++) {
                if (deviceName(real[i].label) === name) { return real[i].deviceId; }
            }
        }
        // 2) Same group as the mic (the browser's "same physical device" hint).
        if (mic.groupId) {
            for (var j = 0; j < real.length; j++) {
                if (real[j].groupId === mic.groupId) { return real[j].deviceId; }
            }
        }
        // 3) Windows' communications output, if it is a hands-free one.
        for (var k = 0; k < outs.length; k++) {
            if (outs[k].deviceId === "communications" && HANDS_FREE.test(outs[k].label || "")) {
                return "communications";
            }
        }
        return "";
    }

    var api = { pickOutput: pickOutput };
    if (typeof module !== "undefined" && module.exports) { module.exports = api; }
    else { root.AudioRoute = api; }
})(this);
