/**
 * auth.js
 * Self-authenticating identity using Web Crypto API (ECDSA P-256).
 *
 * Identity  = public key
 * Auth      = proof of possession of the corresponding private key
 *
 * The private key is stored in IndexedDB with extractable:false — it never
 * leaves the browser, not even to other JavaScript on the page.
 *
 * WARNING: Clearing browser data (IndexedDB) permanently removes the private
 * key. There is no recovery mechanism — this is intentional for privacy.
 */

var _DB_NAME    = "togetherminds_auth";
var _STORE_NAME = "keys";
var _DB_VERSION = 1;

// ---------------------------------------------------------------------------
// IndexedDB helpers
// ---------------------------------------------------------------------------

function _openDb() {
    return new Promise(function (resolve, reject) {
        var req = indexedDB.open(_DB_NAME, _DB_VERSION);
        req.onupgradeneeded = function (e) {
            e.target.result.createObjectStore(_STORE_NAME);
        };
        req.onsuccess = function (e) { resolve(e.target.result); };
        req.onerror   = function (e) { reject(e.target.error); };
    });
}

function _dbGet(key) {
    return _openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
            var tx  = db.transaction(_STORE_NAME, "readonly");
            var req = tx.objectStore(_STORE_NAME).get(key);
            req.onsuccess = function () { resolve(req.result); };
            req.onerror   = function () { reject(req.error); };
        });
    });
}

function _dbPut(key, value) {
    return _openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
            var tx  = db.transaction(_STORE_NAME, "readwrite");
            var req = tx.objectStore(_STORE_NAME).put(value, key);
            req.onsuccess = function () { resolve(); };
            req.onerror   = function () { reject(req.error); };
        });
    });
}

// ---------------------------------------------------------------------------
// Keypair management
// ---------------------------------------------------------------------------

/**
 * Check whether this browser already has a stored keypair.
 * @returns {Promise<boolean>}
 */
function hasKeypair() {
    return _dbGet("privateKey").then(function (key) {
        return key !== undefined && key !== null;
    });
}

/**
 * Generate a new P-256 ECDSA keypair, store it in IndexedDB, and return the
 * public key as a base64-encoded SPKI DER string ready to send to the server.
 * @returns {Promise<string>} base64 SPKI public key
 */
function generateAndStoreKeypair() {
    return crypto.subtle.generateKey(
        { name: "ECDSA", namedCurve: "P-256" },
        false,              // private key NOT extractable — never leaves browser
        ["sign", "verify"]
    ).then(function (keypair) {
        return Promise.all([
            _dbPut("privateKey", keypair.privateKey),
            _dbPut("publicKey",  keypair.publicKey),
            crypto.subtle.exportKey("spki", keypair.publicKey),
        ]);
    }).then(function (results) {
        var spkiBuffer = results[2];
        return _arrayBufferToBase64(spkiBuffer);
    });
}

// ---------------------------------------------------------------------------
// Auth flows
// ---------------------------------------------------------------------------

/**
 * Register a new user: generate keypair, POST public key to server.
 * @param {string} therapyMode - "solo", "couple", or "group"
 * @returns {Promise<object>} server response data
 */
function registerUser(therapyMode) {
    return generateAndStoreKeypair().then(function (publicKeyB64) {
        return fetch("/api/auth/register", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({ public_key: publicKeyB64, therapy_mode: therapyMode }),
        });
    }).then(function (res) {
        if (!res.ok) {
            return res.json().then(function (err) {
                throw new Error(err.error || "Registration failed (" + res.status + ")");
            });
        }
        return res.json();
    }).then(function (data) {
        sessionStorage.setItem("user_id",      data.user_id);
        sessionStorage.setItem("therapy_mode", data.therapy_mode);
        if (data.session_id) {
            sessionStorage.setItem("session_id", data.session_id);
        }
        return data;
    });
}

/**
 * Authenticate a returning user: get challenge, sign with private key, verify.
 * @param {string} userId
 * @returns {Promise<object>} server response data
 */
function authenticateUser(userId) {
    var challengeNonce;

    return fetch("/api/auth/challenge", {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ user_id: userId }),
    }).then(function (res) {
        if (!res.ok) {
            return res.json().then(function (err) {
                throw new Error(err.error || "Challenge failed (" + res.status + ")");
            });
        }
        return res.json();
    }).then(function (data) {
        challengeNonce = data.challenge;
        return _dbGet("privateKey");
    }).then(function (privateKey) {
        if (!privateKey) throw new Error("No private key found in this browser.");
        var encoder = new TextEncoder();
        return crypto.subtle.sign(
            { name: "ECDSA", hash: "SHA-256" },
            privateKey,
            encoder.encode(challengeNonce)
        );
    }).then(function (sigBuffer) {
        return fetch("/api/auth/verify", {
            method:  "POST",
            headers: { "Content-Type": "application/json" },
            body:    JSON.stringify({
                user_id:   userId,
                signature: _arrayBufferToBase64(sigBuffer),
            }),
        });
    }).then(function (res) {
        if (!res.ok) {
            return res.json().then(function (err) {
                throw new Error(err.error || "Verification failed (" + res.status + ")");
            });
        }
        return res.json();
    }).then(function (data) {
        sessionStorage.setItem("user_id",      data.user_id);
        sessionStorage.setItem("therapy_mode", data.therapy_mode);
        if (data.session_id) {
            sessionStorage.setItem("session_id", data.session_id);
        }
        return data;
    });
}

/**
 * Entry point: detect new vs. returning user and run the appropriate flow.
 * @param {string}   therapyMode - "solo", "couple", or "group"
 * @param {Function} onSuccess   - called with server response data
 * @param {Function} onError     - called with Error object
 */
function startAuth(therapyMode, onSuccess, onError) {
    hasKeypair().then(function (has) {
        var storedUserId = sessionStorage.getItem("user_id");
        if (has && storedUserId) {
            return authenticateUser(storedUserId).then(onSuccess).catch(function (err) {
                // If the user no longer exists on the server (e.g. after a restart or
                // delete), clear stale state and register fresh rather than surfacing
                // a confusing error.
                if (err.message && err.message.indexOf("User not found") !== -1) {
                    sessionStorage.removeItem("user_id");
                    sessionStorage.removeItem("therapy_mode");
                    sessionStorage.removeItem("session_id");
                    return registerUser(therapyMode).then(onSuccess);
                }
                throw err;
            });
        }
        return registerUser(therapyMode).then(onSuccess);
    }).catch(onError);
}

// ---------------------------------------------------------------------------
// Utility
// ---------------------------------------------------------------------------

function _arrayBufferToBase64(buffer) {
    var bytes  = new Uint8Array(buffer);
    var binary = "";
    for (var i = 0; i < bytes.length; i++) {
        binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
}
