"""
Agora Video Calling POC
------------------------
Flask backend that:
  1. Generates a shareable meeting link (a unique Agora channel name).
  2. Serves a browser page for that link which joins the Agora video call
     (actual audio/video capture + rendering happens client-side via
     Agora's Web SDK, since there's no browser-media-capture SDK for
     server-side Python -- Python's job here is channel/link management
     and secure token issuance).
  3. Issues short-lived Agora RTC tokens so the project can run in
     production ("token authentication") mode, not just testing mode.

Run:
    pip install -r requirements.txt --break-system-packages
    cp .env.example .env   # fill in your Agora App ID (+ certificate)
    python app.py
"""
import hashlib
import hmac
import os
import secrets
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from server.agora_token.RtcTokenBuilder2 import RtcTokenBuilder, Role_Publisher

# override=True so edits to .env win over any stale value inherited from a
# parent process (e.g. Flask's auto-reloader keeps the original environment).
load_dotenv(override=True)

APP_ID = os.environ.get("AGORA_APP_ID", "")
APP_CERTIFICATE = os.environ.get("AGORA_APP_CERTIFICATE", "")
TOKEN_TTL_SECONDS = int(os.environ.get("AGORA_TOKEN_TTL_SECONDS", "3600"))

# When the app is reached through a public HTTPS tunnel/proxy (e.g. VS Code
# port forwarding, ngrok, a load balancer), set PUBLIC_BASE_URL so the invite
# links we hand out point at that shareable https:// address instead of the
# internal http://localhost:5000 the server actually binds to. Without this,
# links generated behind a TLS-terminating proxy come out as http:// and are
# unusable / trigger mixed-content warnings on the https page.
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")

# Host snapshots (.png) and screen recordings (.webm) are uploaded here by the
# browser (see /api/recordings). Kept out of source control (see .gitignore) --
# these are user-generated call artifacts, not application code.
RECORDINGS_DIR = Path(__file__).resolve().parent / "recordings"

# Secret used to sign the per-room "host" capability token (see host_token).
# Prefer a dedicated secret; fall back to the Agora App Certificate (already a
# server secret) or a random per-process key. Set a stable ROOM_SIGNING_SECRET
# in .env so host links survive a server restart.
ROOM_SIGNING_SECRET = (
    os.environ.get("ROOM_SIGNING_SECRET")
    or APP_CERTIFICATE
    or secrets.token_hex(32)
)

app = Flask(__name__)

# Trust the X-Forwarded-* headers a tunnel/proxy sets, so request.url_root
# reports the real https:// scheme and host even when PUBLIC_BASE_URL is unset.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


def base_url():
    """The externally-visible origin to build shareable links from."""
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    return request.host_url.rstrip("/")


def host_token(room_id):
    """An unforgeable per-room host capability token.

    Replaces the old guessable `?host=1` flag: the creator's link carries a
    token that's an HMAC of the room id under a server secret, so a guest can't
    grant themselves host powers by editing the URL. This is a *capability*
    token -- it gates the host-only UI (recording/snapshot) -- not a substitute
    for real user auth; add that before a production deployment.
    """
    return hmac.new(
        ROOM_SIGNING_SECRET.encode(), room_id.encode(), hashlib.sha256
    ).hexdigest()[:32]


def is_host_request(room_id):
    """True iff the request carries a valid host token for this room."""
    token = request.args.get("host", "")
    # compare_digest avoids leaking the answer via timing.
    return bool(token) and hmac.compare_digest(token, host_token(room_id))


@app.get("/")
def index():
    """Home page: lets someone generate a fresh meeting link."""
    return render_template("index.html", app_id_configured=bool(APP_ID))


@app.post("/api/create-room")
def create_room():
    """Generate a unique channel name and return the shareable call link."""
    if not APP_ID:
        return jsonify(error="AGORA_APP_ID is not configured on the server."), 500

    # Channel name = the "room". Agora doesn't need this pre-registered
    # anywhere -- the channel is created implicitly the moment the first
    # person joins it, and destroyed when the last person leaves.
    room_id = uuid.uuid4().hex[:12]
    base = base_url()
    return jsonify(
        room_id=room_id,
        # Plain invite link to share with participants.
        call_url=f"{base}/room/{room_id}",
        # The creator's own link. The signed host token unlocks the host-only
        # controls (recording/snapshot) and can't be forged by editing the URL.
        host_url=f"{base}/room/{room_id}?host={host_token(room_id)}",
    )


@app.get("/room/<room_id>")
def room(room_id):
    """The page the shared link points to. Joins the video call.

    Host status is validated server-side from the signed token in the URL and
    passed to the template, so a guest can't unlock host controls by guessing.
    """
    return render_template(
        "room.html", room_id=room_id, app_id=APP_ID, is_host=is_host_request(room_id)
    )


@app.get("/api/token")
def get_token():
    """
    Issue an Agora RTC token for a given channel.
    Called by the browser page right before it joins the channel.

    Query params:
      channel (required): the room/channel name
      uid (optional): integer uid; a random one is generated if omitted
    """
    channel_name = request.args.get("channel", "").strip()
    if not channel_name:
        return jsonify(error="channel is required"), 400

    if not APP_ID:
        return jsonify(error="AGORA_APP_ID is not configured on the server."), 500

    uid = request.args.get("uid", type=int)
    if uid is None:
        # 32-bit unsigned int range required by Agora; 0 means "let the
        # SDK auto-assign," so we pick a random non-zero id per user.
        uid = secrets.randbelow(2**31 - 1) + 1

    if not APP_CERTIFICATE:
        # The Agora project is in "testing mode" (no App Certificate
        # enabled) -- clients join with a null token in that case.
        return jsonify(app_id=APP_ID, channel=channel_name, uid=uid, token=None)

    # Agora's token builder doesn't raise on malformed App ID/Certificate --
    # it silently returns an empty string. Real Agora IDs/certificates are
    # always exactly 32 characters, so catch typos/placeholders here instead
    # of shipping a broken-but-200-OK response to the client.
    if len(APP_ID) != 32 or len(APP_CERTIFICATE) != 32:
        return jsonify(
            error=(
                "AGORA_APP_ID or AGORA_APP_CERTIFICATE looks malformed "
                "(expected 32 characters each, as copied from the Agora "
                "Console). Token generation fails silently on bad input, "
                "so check your .env values."
            )
        ), 500

    token = RtcTokenBuilder.build_token_with_uid(
        app_id=APP_ID,
        app_certificate=APP_CERTIFICATE,
        channel_name=channel_name,
        uid=uid,
        role=Role_Publisher,
        token_expire=TOKEN_TTL_SECONDS,
        privilege_expire=TOKEN_TTL_SECONDS,
    )
    return jsonify(app_id=APP_ID, channel=channel_name, uid=uid, token=token)


@app.post("/api/recordings")
def save_recording():
    """Save a host's snapshot (.png) or screen recording (.webm) to the server.

    The browser does the capture locally (a canvas composite for snapshots, a
    MediaRecorder screen-capture for recordings) and POSTs the resulting blob
    here as multipart form-data; we stream it to the project's recordings/
    folder. This is the lightweight, single-server alternative to Agora Cloud
    Recording (which you'd reach for to get centralised, all-participant,
    cloud-stored recordings).
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="no file uploaded"), 400

    # secure_filename strips directory separators and traversal (../) sequences;
    # fall back to a safe default if it reduces the name to nothing.
    name = secure_filename(f.filename) or "capture"
    if not name.lower().endswith((".png", ".webm")):
        return jsonify(error="unsupported file type (only .png / .webm)"), 400

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    f.save(RECORDINGS_DIR / name)  # streams to disk, no full in-memory buffering
    return jsonify(ok=True, filename=name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
