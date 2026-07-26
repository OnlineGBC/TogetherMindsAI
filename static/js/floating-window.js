/* floating-window.js
 * A non-modal floating panel (no backdrop, so the live session stays usable
 * behind it) that is draggable by its header and resizable from all 8 edges /
 * corners. Used for the Progress and content (My sessions / Privacy / Terms …)
 * windows in a live session. Desktop only — on small screens the window is
 * shown near-fullscreen and drag/resize are disabled.
 */
window.FloatingWindow = (function () {
    var Z = 1090;
    var MIN_W = 280, MIN_H = 180;

    function isMobile() { return window.matchMedia("(max-width: 768px)").matches; }
    function bringToFront(win) { Z += 1; win.style.zIndex = Z; }

    function iframeInteract(win, on) {
        var f = win.querySelector("iframe");
        if (f) f.style.pointerEvents = on ? "" : "none";   // let drag events pass over the iframe
    }

    function positionDefault(win) {
        if (isMobile()) {
            win.style.left = "2vw"; win.style.top = "2vh";
            win.style.width = "96vw"; win.style.height = "92vh";
            return;
        }
        var w = Math.min(760, Math.round(window.innerWidth * 0.86));
        var h = Math.min(Math.round(window.innerHeight * 0.82), window.innerHeight - 60);
        win.style.width = w + "px";
        win.style.height = h + "px";
        win.style.left = Math.round((window.innerWidth - w) / 2) + "px";
        win.style.top = Math.round((window.innerHeight - h) / 2) + "px";
    }

    function makeDraggable(win, handle) {
        var sx, sy, sl, st, dragging = false;
        handle.addEventListener("mousedown", function (e) {
            if (isMobile() || e.target.closest("[data-fw-close]")) return;
            dragging = true; bringToFront(win); iframeInteract(win, false);
            sx = e.clientX; sy = e.clientY; sl = win.offsetLeft; st = win.offsetTop;
            document.body.style.userSelect = "none";
            e.preventDefault();
        });
        document.addEventListener("mousemove", function (e) {
            if (!dragging) return;
            win.style.left = (sl + e.clientX - sx) + "px";
            win.style.top = Math.max(0, st + e.clientY - sy) + "px";
        });
        document.addEventListener("mouseup", function () {
            if (!dragging) return;
            dragging = false; document.body.style.userSelect = ""; iframeInteract(win, true);
        });
    }

    function makeResizable(win) {
        win.querySelectorAll(".fw-rs").forEach(function (h) {
            var dir = h.getAttribute("data-dir") || "";
            h.addEventListener("mousedown", function (e) {
                if (isMobile()) return;
                bringToFront(win); iframeInteract(win, false);
                var sx = e.clientX, sy = e.clientY;
                var sw = win.offsetWidth, sh = win.offsetHeight;
                var sl = win.offsetLeft, st = win.offsetTop;
                document.body.style.userSelect = "none";
                function move(ev) {
                    var dx = ev.clientX - sx, dy = ev.clientY - sy;
                    var w = sw, hgt = sh, l = sl, t = st;
                    if (dir.indexOf("e") !== -1) w = Math.max(MIN_W, sw + dx);
                    if (dir.indexOf("s") !== -1) hgt = Math.max(MIN_H, sh + dy);
                    if (dir.indexOf("w") !== -1) { w = Math.max(MIN_W, sw - dx); l = sl + (sw - w); }
                    if (dir.indexOf("n") !== -1) { hgt = Math.max(MIN_H, sh - dy); t = st + (sh - hgt); }
                    win.style.width = w + "px"; win.style.height = hgt + "px";
                    win.style.left = l + "px"; win.style.top = t + "px";
                }
                function up() {
                    document.removeEventListener("mousemove", move);
                    document.removeEventListener("mouseup", up);
                    document.body.style.userSelect = ""; iframeInteract(win, true);
                }
                document.addEventListener("mousemove", move);
                document.addEventListener("mouseup", up);
                e.preventDefault(); e.stopPropagation();
            });
        });
    }

    function init(win) {
        if (win._fwInit) return;
        win._fwInit = true;
        var header = win.querySelector(".fw-header");
        if (header) makeDraggable(win, header);
        makeResizable(win);
        win.addEventListener("mousedown", function () { bringToFront(win); });
        win.querySelectorAll("[data-fw-close]").forEach(function (b) {
            b.addEventListener("click", function () { close(win); });
        });
    }

    function open(win) {
        if (!win) return;
        init(win);
        if (!win.style.width || isMobile()) positionDefault(win);
        win.hidden = false;
        bringToFront(win);
    }

    function close(win) {
        if (!win) return;
        win.hidden = true;
        var f = win.querySelector("iframe");
        if (f) f.removeAttribute("src");   // stop the embedded page when closed
    }

    return { open: open, close: close, init: init };
})();
