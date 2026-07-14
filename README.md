# MaxPay Video KYC (Python / Flask)

A minimal proof of concept for live video identity verification (Video KYC):
an agent starts a session and shares the link, the customer opens it, and
both join the same live video call to complete verification. Camera and
microphone are both mandatory — verification can't proceed without seeing and
hearing the customer.

## How it actually works (important to understand before you extend this)

Agora doesn't have a browser/camera-capture SDK for Python — no server-side
language does, because capturing a webcam and rendering video is a *browser*
job. So the split is:

- **Python (Flask)** — the only two things a server needs to do for Agora:
  1. Hand out a **channel name** (that's your "room" / "link" — Agora needs
     no pre-registration, the channel exists the instant someone joins it
     and disappears when the last person leaves).
  2. Issue a short-lived **token** (server-side, using your secret App
     Certificate) so random people can't join your channels without
     permission.
- **Browser (Agora Web SDK, `agora-rtc-sdk-ng`, loaded via CDN)** — does the
  actual camera/mic capture, encoding, and rendering. This is plain
  JavaScript embedded in `templates/room.html`. There is no way around this
  part being JS — it's inherent to how browsers expose media devices.

So "Python for video calling" in practice means: Python is your link/room/
auth backend, and the call itself runs in the Agora Web SDK.

```
app.py                          Flask backend (links, tokens, capture uploads)
templates/index.html            Landing: create a link (host) or join by code
templates/room.html             Lobby -> in-call -> ended (Agora Web SDK)
static/css/app.css              Shared MaxPay design system (tokens + components)
recordings/                     Host snapshots/recordings land here (gitignored)
server/agora_token/             Agora's official AccessToken2 token builder
  ├─ AccessToken2.py            (copied verbatim from AgoraIO/Tools on GitHub —
  ├─ RtcTokenBuilder2.py         don't hand-roll this, it's a specific
  ├─ RtmTokenBuilder2.py         HMAC-SHA256 + versioned binary format)
  └─ Packer.py
```

## Setup

1. **Create an Agora project**: go to https://console.agora.io → Project
   Management → Create. Copy the **App ID**.
   - For this POC, you can leave the project in "Testing Mode" (no App
     Certificate) — the app will detect this and skip tokens automatically,
     for **both** RTC and RTM (see the RTM note below).
   - For anything beyond a local demo, enable the **App Certificate** on the
     project (Console → your project → Config → enable Primary Certificate)
     and copy it too — this turns on token authentication for RTC *and*
     unlocks real RTM (chat/presence) login tokens (see the RTM note below).

2. **Configure the app**:
   ```bash
   cd agora-poc
   cp .env.example .env
   # edit .env and paste in your App ID (and Certificate, if enabled)
   ```

3. **Install & run**:
   ```bash
   pip install -r requirements.txt --break-system-packages
   python app.py
   ```
   Visit `http://localhost:5000`.

### Sharing links beyond your machine

Camera/mic (and therefore joining) only work on `https://` or `localhost`. To
let someone else join, expose the app over HTTPS — VS Code port forwarding,
ngrok, or any reverse proxy — and set `PUBLIC_BASE_URL` in `.env` to that
public address, e.g.:

```
PUBLIC_BASE_URL=https://your-tunnel-id.devtunnels.ms
```

Invite links are then built from that URL instead of `http://localhost:5000`,
so they're shareable and open with a valid secure context. The app also honors
`X-Forwarded-Proto`/`X-Forwarded-Host` (via `ProxyFix`), so links come out as
`https://` even if you forget to set `PUBLIC_BASE_URL`. Leave it blank for
purely local testing.

## Testing the actual video call

1. Open `http://localhost:5000`, click **Create meeting link**.
2. Open the generated link (`/room/<id>`) in one browser tab — allow camera/
   mic when prompted.
3. Open the **same link** in a second tab, or a different browser, or send
   it to a phone on the same network. Both sides should see each other.

> Browsers only allow camera/mic access on `https://` or `http://localhost`.
> If you deploy this anywhere other than localhost, you need HTTPS or the
> `AgoraRTC.createMicrophoneAudioTrack()` / `createCameraVideoTrack()` calls
> will be blocked by the browser.

## Gotcha worth knowing

Agora's token builder does **not** raise an error on a malformed App ID or
Certificate — it silently returns an empty string, which is a nasty trap in
production. Real Agora App IDs and Certificates are always exactly 32
characters; `app.py`'s `/api/token` route validates this and returns a clear
error instead of a silently-broken token.

## In-call features

> **Terminology note**: the code/URLs still use `host`/`guest` (`is_host`,
> `host_token`, `?host=<token>`) since that's the underlying capability model
> (creator vs. joiner). The UI displays these roles as **Agent** / **Customer**
> to match the Video KYC framing — that's a display-label change only, not a
> renamed API.

- **Camera + microphone are mandatory** — unlike a generic call, Video KYC
  can't proceed without seeing and hearing the customer. If either device is
  unavailable (denied permission, not found, or in use by another app),
  `room.html` shows a blocking warning (reusing the existing warning-pill
  style) and disables Start/Join — see `friendlyMediaError()` and
  `updateMediaRequirement()`.
- **Lobby / join preview** — before joining, the agent and customer both see a
  live local camera preview (`AgoraRTC.createCameraVideoTrack()`), a **mic-level
  meter** driven by the track's `getVolumeLevel()`, mic/camera toggles, and
  **camera + microphone dropdowns** populated from `AgoraRTC.getCameras()` /
  `getMicrophones()` (switching calls `track.setDevice()`). Those same tracks
  are published on join, so there's no second permission prompt. A ⋯ More menu
  in-call also offers camera-switch when 2+ cameras are present.
- **Target video quality** — the camera publishes at a defined 720p @ 24fps
  `encoderConfig` (`VIDEO_ENCODER_CONFIG` in `room.html`). Agora still adapts
  the actual stream downward automatically on poor networks.
- **Pre-call network test** — on the lobby/join screen the page briefly joins a
  throwaway channel and samples Agora's `network-quality` metric — Agora's
  *web* pre-call test (the native last-mile *probe* API doesn't exist on web).
  The result shows as a chip and warns before joining on a poor link. It's
  passive (publishes nothing), so it never contends with the preview tracks.
- **Live network indicator** — during the call, Agora's `network-quality`
  event drives the signal bars and warns on poor connections.
- **Host-only recording** — the creator's link carries a signed host token,
  which unlocks a **Record** option in the control bar's **⋯ More** menu. It
  screen-captures the tab (whole call view + audio) via the browser, then
  **uploads the `.webm` to the server** (`POST /api/recordings`), saved under
  `recordings/`.
- **Host-only snapshot** — a **Take snapshot** option (also in the ⋯ More menu)
  composites the live video tiles into a `.png` and uploads it to the server's
  `recordings/` folder, instantly and with no permission prompt.
- **Chat & participants panels (Agora RTM / Signaling)** — real, live in-call
  chat and presence, backed by Agora's **RTM 2.x** SDK (`agora-rtm`, loaded via
  CDN in `room.html`). The browser logs in to RTM with the **same uid** it
  joined RTC with (as a string), so presence maps 1:1 to video tiles:
  - Chat messages are sent with `rtm.publish()` and received via the `message`
    event — real messages only, no canned/sample data. Own messages render
    right-aligned, others left-aligned, using the existing bubble/pill tokens.
  - The roster is seeded from RTM **presence** (the `SNAPSHOT` event on
    subscribe, refreshed on every subsequent `REMOTE_JOIN`/`REMOTE_LEAVE`/
    `REMOTE_STATE_CHANGED` event) with each person's *real* entered name (or
    "Host" for the creator) published into presence state on join —
    replacing the old generic "Participant" label.
  - If RTM fails to initialize (e.g. a network hiccup), the call itself is
    unaffected — chat/roster just fall back to a disabled state with a toast,
    rather than blocking the video call.
- **Token renewal** — the call page listens for `token-privilege-will-expire`
  (RTC) and `tokenPrivilegeWillExpire` (RTM) and re-fetches `/api/token` /
  `/api/rtm-token` to renew both before they expire (default 1 hour,
  `AGORA_TOKEN_TTL_SECONDS`), so long calls aren't dropped.

### RTM (Signaling) setup note

`GET /api/rtm-token?uid=<uid>` issues the RTM login token, mirroring
`/api/token`'s pattern (`server/agora_token/RtmTokenBuilder2.py`, copied
verbatim from AgoraIO/Tools, same source as the RTC builder). Like RTC:

- **No App Certificate** (testing mode) → the endpoint returns `token: null`
  and the browser logs in to RTM without a token. This works out of the box
  for local testing, same as the RTC token flow.
- **App Certificate enabled** → real signed RTM tokens are issued, required
  before deploying chat/presence beyond a local demo.

## What's deliberately left out (POC scope)

- **Persistence** — there's no database. The "room" only exists as an Agora
  channel name in a URL; nothing about who created it or who's in it is
  stored anywhere. Fine for a POC, but you'll want a `meetings` table (host,
  created_at, expiry, invite-only flag, etc.) for a real feature.
- **Auth** — anyone with the link can join. Host status is a **signed
  capability token** in the creator's link (`?host=<token>`, an HMAC of the
  room id validated server-side), so it can't be forged by editing the URL —
  but it only gates the host-only UI (recording/snapshot); it is not a user
  identity/authentication system. Add your app's normal auth before rendering
  `/room/<id>` for a real deployment.
- **Cloud / all-participant recording** — recordings and snapshots here are a
  browser screen-capture of the **host's own tab**, uploaded to *this* server's
  `recordings/` folder (fine for a single-host POC). For centralized recording
  of every participant's original streams saved to cloud storage (S3, etc.),
  use **Agora Cloud Recording** — a separate server-side API needing your
  Customer ID/Secret and a storage bucket. Note the uploaded files aren't
  access-controlled; add your app's auth before a real deployment.
- **TURN/firewall fallback** — supported by Agora, not configured here.

## Pricing (as of writing — verify at agora.io/en/pricing)

Video Calling: **10,000 free minutes/month**, then ~$3.99 per 1,000 minutes
beyond that. No card required to start building.
