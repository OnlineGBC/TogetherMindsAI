"""
tests/socket_utils.py
---------------------
Shared helpers for SocketIO tests.

Realtime identity is bound to the authenticated Flask session (never the event
payload), so every socket in a test must carry its OWN signed session. Two
sockets sharing one Flask test client would share one identity — these helpers
build a per-user socket and the consent/licensure attestations a client needs
to be admitted to a clinician-led room.
"""
from datetime import datetime, timezone


def authed_socket(app, socketio, user_id, *, session_id=None, consented=True,
                  state=None, clinician=False):
    """A SocketIO test client whose handshake carries an authenticated session.

    user_id                → session['user_id'] (the trusted identity)
    clinician=True         → also set session['clinician_id'] (therapist)
    session_id + consented → mark this session consented (client admission)
    session_id + state     → record the client's attested US state
    """
    fc = app.test_client()
    with fc.session_transaction() as s:
        s["user_id"] = user_id
        if clinician:
            s["clinician_id"] = user_id
        if session_id and consented:
            s["consented_sessions"] = [session_id]
        if session_id and state:
            s["session_states"] = {session_id: state}
    return socketio.test_client(app, flask_test_client=fc)


def certify_state(db, SessionStateCert, session_id, therapist_id, state="CA"):
    """Record that the clinician certified `state` for this session, so a client
    attesting `state` clears the licensure gate (mirrors a real certify POST).
    Idempotent — safe to call once per client in a shared session."""
    exists = SessionStateCert.query.filter_by(session_id=session_id, state=state).first()
    if exists:
        return
    db.session.add(SessionStateCert(
        session_id=session_id, state=state, therapist_id=therapist_id,
        decision="certified", attested_at=datetime.now(timezone.utc),
    ))
    db.session.commit()
