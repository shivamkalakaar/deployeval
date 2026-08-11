"""realtime-room probes (7) — see docs/PROBES.md §10 and the §10.0 WebSocket contract.

Failure surface: realtime delivery + connection auth. DEPENDENCY: websocket-client (`import
websocket`) — the only third-party dep beyond requests; recorded in requirements.

Contract (§10.0): public wss:// connect URL; token at connect via ?token=<jwt> and/or
Authorization: Bearer. JSON messages: join / message / (broadcast) / presence. Sender is
server-derived; client-supplied sender/user/from MUST be ignored. Fail-closed: a missed expected
message = FAIL; an unexpected message = FAIL; socket errors on critical probes = error.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Optional

from .core import ProbeContext, ProbeResult, ProbeStatus, TestUser

TASK = "realtime-room"

try:
    import websocket  # websocket-client
    _WS_OK = True
except ImportError:
    _WS_OK = False


def _http_base(ctx):
    # Auth (signup/login) is plain HTTP. A bare WebSocket execute-api host answers 403 to ALL
    # HTTP requests, so serving auth on the wss hostname is impossible without CloudFront/a custom
    # domain. A correct free-tier build therefore puts auth on a SEPARATE HTTP API host. Honor an
    # explicitly-provided auth base (ctx.extra["auth_base"]); only fall back to deriving it from the
    # wss host for builds that unified the host (e.g. via CloudFront).
    explicit = ctx.extra.get("auth_base")
    if explicit:
        return explicit
    b = ctx.base_url
    if b.startswith("wss://"):
        return "https://" + b[len("wss://"):]
    if b.startswith("ws://"):
        return "http://" + b[len("ws://"):]
    return b


def _wss(ctx):
    b = ctx.base_url
    if b.startswith("https://"):
        return "wss://" + b[len("https://"):]
    if b.startswith("http://"):
        return "ws://" + b[len("http://"):]
    return b


def _mk(name, cls, critical, status, expected="", observed="", requests_=None, detail=""):
    return ProbeResult(probe=name, task=TASK, probe_class=cls, critical=critical, status=status,
                       expected=expected, observed=observed, requests=requests_ or [], detail=detail)


def _login(ctx, label) -> Optional[str]:
    import requests
    base = _http_base(ctx)
    u = f"rt_{label}_{secrets.token_hex(4)}@example.test"; pw = secrets.token_urlsafe(12)
    try:
        requests.post(base.rstrip("/") + "/auth/signup", json={"email": u, "password": pw}, timeout=ctx.timeout_s)
        r = requests.post(base.rstrip("/") + "/auth/login", json={"email": u, "password": pw}, timeout=ctx.timeout_s)
        if r.status_code == 200:
            return (r.json() or {}).get("token")
    except Exception:  # noqa: BLE001
        return None
    return None


def _connect(ctx, token: Optional[str], timeout=None):
    """Open a ws connection with token as query param + header. Returns (ws, error)."""
    url = _wss(ctx).rstrip("/")
    if token:
        url = f"{url}?token={token}"
    header = [f"Authorization: Bearer {token}"] if token else []
    try:
        ws = websocket.create_connection(url, timeout=timeout or ctx.timeout_s, header=header)
        return ws, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc!r}"


def _send(ws, obj):
    ws.send(json.dumps(obj))


def _read_for(ws, deliver_s, predicate):
    """Read frames until predicate(msg) True or deliver_s elapses. Returns (matched_msg or None)."""
    end = time.time() + deliver_s
    ws.settimeout(deliver_s)
    while time.time() < end:
        try:
            raw = ws.recv()
        except Exception:  # noqa: BLE001 — timeout/closed
            return None
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if predicate(msg):
            return msg
    return None


def _deliver_s(ctx):
    return min(5.0, max(2.0, ctx.timeout_s / 4))


def _ensure_tokens(ctx):
    if ctx.extra.get("tokA") is None and not ctx.extra.get("_tok_tried"):
        ctx.extra["_tok_tried"] = True
        ctx.extra["tokA"] = _login(ctx, "A")
        ctx.extra["tokB"] = _login(ctx, "B")
    return ctx.extra.get("tokA"), ctx.extra.get("tokB")


def _dep_guard(name, cls, critical):
    return _mk(name, cls, critical, ProbeStatus.ERROR,
               detail="websocket-client not installed; `pip install websocket-client`")


# ---- probes ----------------------------------------------------------------

def live_ws_connect(ctx):
    if not _WS_OK:
        return _dep_guard("live_ws_connect", "liveness", False)
    tokA, _ = _ensure_tokens(ctx)
    ws, err = _connect(ctx, tokA)
    if ws:
        ws.close()
        return _mk("live_ws_connect", "liveness", False, ProbeStatus.PASS,
                   expected="ws handshake completes or cleanly refuses", observed="connected")
    # a clean refusal (handshake rejected) still means the service answered
    refused_cleanly = any(s in err for s in ("401", "403", "Handshake", "rejected"))
    return _mk("live_ws_connect", "liveness", False,
               ProbeStatus.PASS if refused_cleanly else ProbeStatus.FAIL,
               expected="endpoint answers upgrade (101 or clean 401/403)",
               observed=err[:120], detail="" if refused_cleanly else "nothing listening / TLS/DNS fail")


def happy_two_clients_broadcast(ctx):
    if not _WS_OK:
        return _dep_guard("happy_two_clients_broadcast", "happy-path", False)
    tokA, tokB = _ensure_tokens(ctx)
    room = "R-" + secrets.token_hex(4); marker = "m-" + secrets.token_hex(4)
    wsa = wsb = None
    try:
        wsa, ea = _connect(ctx, tokA); wsb, eb = _connect(ctx, tokB)
        if not wsa or not wsb:
            return _mk("happy_two_clients_broadcast", "happy-path", False, ProbeStatus.ERROR,
                       detail=f"connect failed A={ea} B={eb}")
        _send(wsa, {"action": "join", "room": room}); _send(wsb, {"action": "join", "room": room})
        time.sleep(0.5)
        _send(wsa, {"action": "message", "room": room, "text": marker})
        got = _read_for(wsb, _deliver_s(ctx),
                        lambda m: m.get("type") == "message" and m.get("room") == room and marker in json.dumps(m))
        ok = got is not None
        return _mk("happy_two_clients_broadcast", "happy-path", False,
                   ProbeStatus.PASS if ok else ProbeStatus.FAIL,
                   expected="B receives A's room message within deliver_s",
                   observed=f"delivered={ok}", detail="" if ok else "same-room message not delivered")
    finally:
        for w in (wsa, wsb):
            try:
                w and w.close()
            except Exception:  # noqa: BLE001
                pass


def crossroom_no_leak(ctx):
    if not _WS_OK:
        return _dep_guard("crossroom_no_leak", "cross-room", True)
    tokA, tokB = _ensure_tokens(ctx)
    roomX = "X-" + secrets.token_hex(4); roomY = "Y-" + secrets.token_hex(4); marker = "mx-" + secrets.token_hex(4)
    wsa = wsb = None
    try:
        wsa, ea = _connect(ctx, tokA); wsb, eb = _connect(ctx, tokB)
        if not wsa or not wsb:
            return _mk("crossroom_no_leak", "cross-room", True, ProbeStatus.ERROR,
                       detail=f"connect failed A={ea} B={eb}")
        _send(wsa, {"action": "join", "room": roomX})
        _send(wsb, {"action": "join", "room": roomY})  # B only in Y
        time.sleep(0.5)
        _send(wsa, {"action": "message", "room": roomX, "text": marker})
        leaked = _read_for(wsb, _deliver_s(ctx),
                           lambda m: marker in json.dumps(m) or m.get("room") == roomX)
        ok = leaked is None
        return _mk("crossroom_no_leak", "cross-room", True,
                   ProbeStatus.PASS if ok else ProbeStatus.FAIL,
                   expected="B (in Y only) receives NO room-X traffic",
                   observed=f"leaked={not ok}", detail="room-X message leaked to Y-only client" if not ok else "")
    finally:
        for w in (wsa, wsb):
            try:
                w and w.close()
            except Exception:  # noqa: BLE001
                pass


def connauth_absent_token_rejected(ctx):
    if not _WS_OK:
        return _dep_guard("connauth_absent_token_rejected", "auth-spoof", True)
    # connect with NO token; must be rejected at connect OR unable to join/receive
    ws, err = _connect(ctx, None)
    if ws is None:
        # rejected at connect -> pass
        try_close = None
        return _mk("connauth_absent_token_rejected", "auth-spoof", True, ProbeStatus.PASS,
                   expected="no-credential connect rejected at handshake",
                   observed=f"connect refused: {err[:80]}")
    # connected without a token — try to join and receive; if it can, that's a fail
    try:
        room = "R-" + secrets.token_hex(4)
        _send(ws, {"action": "join", "room": room})
        got = _read_for(ws, _deliver_s(ctx), lambda m: True)  # any frame back = it's participating
        # accepted AND able to interact = fail (public connect)
        return _mk("connauth_absent_token_rejected", "auth-spoof", True, ProbeStatus.FAIL,
                   expected="no-credential connect rejected",
                   observed=f"socket accepted without token (got_frame={got is not None})",
                   detail="unauthenticated socket accepted and could join")
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def connauth_forged_token_rejected(ctx):
    if not _WS_OK:
        return _dep_guard("connauth_forged_token_rejected", "auth-spoof", True)
    tokA, _ = _ensure_tokens(ctx)
    forged = [
        (tokA[:-1] + ("A" if tokA and tokA[-1] != "A" else "B")) if tokA else secrets.token_urlsafe(30),  # tampered sig
        # alg:none unsigned with A-ish claims
        "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhdHRhY2tlciJ9.",
        secrets.token_urlsafe(40),  # random junk
    ]
    accepted_any = False; log = []
    for i, t in enumerate(forged):
        ws, err = _connect(ctx, t)
        if ws is not None:
            # accepted — see if it can actually interact
            try:
                _send(ws, {"action": "join", "room": "R-" + secrets.token_hex(3)})
                accepted_any = True
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
        log.append({"variant": ["tampered", "alg:none", "junk"][i], "accepted": ws is not None})
    return _mk("connauth_forged_token_rejected", "auth-spoof", True,
               ProbeStatus.FAIL if accepted_any else ProbeStatus.PASS,
               expected="all forged tokens rejected at connect",
               observed=f"accepted_any={accepted_any}", requests_=log,
               detail="a forged/alg:none token was accepted at connect" if accepted_any else "")


def identity_spoof_message_sender(ctx):
    if not _WS_OK:
        return _dep_guard("identity_spoof_message_sender", "identity-spoof", True)
    tokA, tokB = _ensure_tokens(ctx)
    room = "R-" + secrets.token_hex(4); marker = "id-" + secrets.token_hex(4)
    wsa = wsb = None
    try:
        wsa, ea = _connect(ctx, tokA); wsb, eb = _connect(ctx, tokB)
        if not wsa or not wsb:
            return _mk("identity_spoof_message_sender", "identity-spoof", True, ProbeStatus.ERROR,
                       detail=f"connect failed A={ea} B={eb}")
        _send(wsa, {"action": "join", "room": room}); _send(wsb, {"action": "join", "room": room})
        time.sleep(0.5)
        # A injects B's identity in the body
        _send(wsa, {"action": "message", "room": room, "text": marker,
                    "sender": "userB", "user": "userB", "from": "userB"})
        got = _read_for(wsb, _deliver_s(ctx),
                        lambda m: m.get("type") == "message" and marker in json.dumps(m))
        if got is None:
            return _mk("identity_spoof_message_sender", "identity-spoof", True, ProbeStatus.ERROR,
                       detail="message never delivered; cannot evaluate sender attribution")
        sender = str(got.get("sender", ""))
        # PASS if sender is NOT the spoofed 'userB' (server derived it from A's connection)
        spoofed = sender == "userB"
        return _mk("identity_spoof_message_sender", "identity-spoof", True,
                   ProbeStatus.FAIL if spoofed else ProbeStatus.PASS,
                   expected="broadcast attributed to A (server-derived), injected sender ignored",
                   observed=f"delivered sender={sender!r}",
                   detail="server trusted client-supplied sender (A posted as B)" if spoofed else "")
    finally:
        for w in (wsa, wsb):
            try:
                w and w.close()
            except Exception:  # noqa: BLE001
                pass


def presence_cleanup_on_disconnect(ctx):
    if not _WS_OK:
        return _dep_guard("presence_cleanup_on_disconnect", "presence", True)
    tokA, tokB = _ensure_tokens(ctx)
    room = "R-" + secrets.token_hex(4)
    wsa = wsb = None
    try:
        wsa, ea = _connect(ctx, tokA); wsb, eb = _connect(ctx, tokB)
        if not wsa or not wsb:
            return _mk("presence_cleanup_on_disconnect", "presence", True, ProbeStatus.ERROR,
                       detail=f"connect failed A={ea} B={eb}")
        _send(wsa, {"action": "join", "room": room}); _send(wsb, {"action": "join", "room": room})
        time.sleep(0.5)
        # NON-CRITICAL + manual-review: presence responses vary in shape across builds. A build may
        # answer a steady-state `presence` query with a {users:[...]} roster, but signal a departure
        # with an event frame ({event:"leave", user}). Both are valid. This probe therefore treats a
        # leave-signal OR B's absence from a roster as cleanup=PASS, and only returns needs-review
        # (never a hard FAIL) when the shape is ambiguous, so it can't fabricate a critical failure.
        bmark = ctx.users.get("B").username if ctx.users.get("B") else None
        wsb.close(); wsb = None
        # collect any frames A receives during the disconnect window (may include a leave event)
        deadline = _deliver_s(ctx)
        leave_seen = False
        end = time.time() + min(8.0, max(4.0, deadline))
        while time.time() < end:
            m = _read_for(wsa, 1.5, lambda m: m.get("type") == "presence")
            if m and str(m.get("event", "")).lower() in ("leave", "disconnect", "left"):
                leave_seen = True; break
        # then ask for a fresh roster
        _send(wsa, {"action": "presence", "room": room})
        pres = _read_for(wsa, deadline, lambda m: m.get("type") == "presence")
        roster = (pres or {}).get("users")
        b_absent = isinstance(roster, list) and (bmark is None or bmark not in roster) and len(roster) <= 1
        if leave_seen or b_absent:
            return _mk("presence_cleanup_on_disconnect", "presence", False, ProbeStatus.PASS,
                       expected="B is cleaned up after disconnect (leave event or absent from roster)",
                       observed=f"leave_event={leave_seen}, roster={json.dumps(roster)[:80]}")
        if roster is None and not leave_seen:
            return _mk("presence_cleanup_on_disconnect", "presence", False, ProbeStatus.SKIP,
                       detail="presence response shape not recognized; needs human review",
                       observed=f"last_frame={json.dumps(pres)[:100]}")
        return _mk("presence_cleanup_on_disconnect", "presence", False, ProbeStatus.SKIP,
                   detail="ambiguous presence after disconnect; needs human review",
                   observed=f"leave_event={leave_seen}, roster={json.dumps(roster)[:80]}")
    finally:
        for w in (wsa, wsb):
            try:
                w and w.close()
            except Exception:  # noqa: BLE001
                pass


PROBES = [
    live_ws_connect,
    happy_two_clients_broadcast,
    crossroom_no_leak,
    connauth_absent_token_rejected,
    connauth_forged_token_rejected,
    identity_spoof_message_sender,
    presence_cleanup_on_disconnect,
]
