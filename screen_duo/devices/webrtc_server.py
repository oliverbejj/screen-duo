"""
WebRTC iPhone camera bridge.

Two-port design required by Safari:
  HTTP  (:8080)  serves /ca.mobileconfig  — iPhone installs this ONCE to trust our CA
  HTTPS (:8443)  serves the camera page   — Safari needs HTTPS for getUserMedia

First-time setup on iPhone (< 2 minutes, never repeated):
  1. Open http://<ip>:8080/ca.mobileconfig in Safari → "Allow" → Settings opens
  2. Settings → General → VPN & Device Management → screen-duo → Install
  3. Settings → General → About → Certificate Trust Settings → Enable screen-duo CA
  4. Now open https://<ip>:8443 → Start Camera → Allow

Certs live in ~/.screen-duo/certs/:
  ca.key / ca.crt          — generated once, CA that iPhone trusts
  server.key / server.crt  — regenerated when IP changes; signed by CA with correct SAN
"""

import asyncio
import socket
import ssl
import subprocess
import threading
import time
from pathlib import Path

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription

# ────────────────────────────────────────────────────────────── iPhone page HTML

_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <title>screen-duo</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #000; color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      display: flex; flex-direction: column; align-items: center;
      justify-content: center; min-height: 100dvh; gap: 24px; padding: 20px;
    }
    video {
      width: min(90vw, 340px); border-radius: 16px;
      background: #111; aspect-ratio: 3/4; object-fit: cover;
    }
    #status { font-size: 15px; color: rgba(255,255,255,.65); text-align: center; line-height: 1.5; }
    button {
      padding: 16px 48px; font-size: 17px; font-weight: 600;
      border: none; border-radius: 14px; background: #0a84ff; color: #fff;
      -webkit-appearance: none; cursor: pointer;
    }
  </style>
</head>
<body>
  <video id="v" autoplay playsinline muted></video>
  <p id="status">Tap Start to stream your camera to screen-duo</p>
  <button id="btn" onclick="go()">Start Camera</button>
  <script>
    async function go() {
      document.getElementById('btn').remove();
      const st = document.getElementById('status');
      st.textContent = 'Opening camera…';
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: 'user', width: { ideal: 1920 }, height: { ideal: 1920 }, frameRate: { min: 25, ideal: 30 } },
          audio: false,
        });
        stream.getTracks().forEach(t => { if (t.kind === 'video') t.contentHint = 'motion'; });
      } catch (e) { st.textContent = '❌ ' + e.message; return; }
      document.getElementById('v').srcObject = stream;
      st.textContent = 'Connecting…';
      const pc = new RTCPeerConnection({ iceServers: [] });
      stream.getTracks().forEach(t => pc.addTrack(t, stream));
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise(r => {
        if (pc.iceGatheringState === 'complete') return r();
        pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === 'complete') r(); };
        setTimeout(r, 3000);
      });
      // Push the encoder toward max quality on local WiFi
      for (const sender of pc.getSenders()) {
        if (!sender.track || sender.track.kind !== 'video') continue;
        const params = sender.getParameters();
        if (!params.encodings || params.encodings.length === 0) params.encodings = [{}];
        params.encodings[0].maxBitrate = 8_000_000; // 8 Mbps ceiling
        params.encodings[0].maxFramerate = 30;
        try { await sender.setParameters(params); } catch (_) {}
      }
      let ans;
      try {
        const res = await fetch('/offer', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type }),
        });
        ans = await res.json();
      } catch (e) { st.textContent = '❌ ' + e.message; return; }
      await pc.setRemoteDescription(ans);
      pc.onconnectionstatechange = () => {
        const s = pc.connectionState;
        st.textContent = s === 'connected' ? '🟢 Streaming to screen-duo'
          : s === 'failed' ? '❌ Failed — refresh to retry' : s;
      };
    }
  </script>
</body>
</html>"""

# ────────────────────────────────────────────────────────────────── cert helpers

def _local_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _run(*args: str) -> None:
    subprocess.run(list(args), check=True, capture_output=True)


def _ensure_certs(ip: str) -> tuple[Path, Path, Path]:
    """
    Return (ca_crt, server_crt, server_key).
    CA is generated once and stays stable (iPhone installs it once).
    Server cert is (re)generated whenever the LAN IP changes.
    """
    d = Path.home() / ".screen-duo" / "certs"
    d.mkdir(parents=True, exist_ok=True)

    ca_key, ca_crt = d / "ca.key", d / "ca.crt"
    if not ca_crt.exists():
        _run("openssl", "genrsa", "-out", str(ca_key), "2048")
        _run(
            "openssl", "req", "-new", "-x509", "-days", "3650",
            "-key", str(ca_key), "-out", str(ca_crt),
            "-subj", "/CN=screen-duo CA",
        )

    srv_key, srv_crt = d / "server.key", d / "server.crt"
    ip_stamp = d / "server.ip"
    current_ip = ip_stamp.read_text().strip() if ip_stamp.exists() else ""

    if not srv_crt.exists() or current_ip != ip:
        _run("openssl", "genrsa", "-out", str(srv_key), "2048")
        csr = d / "server.csr"
        _run(
            "openssl", "req", "-new",
            "-key", str(srv_key), "-out", str(csr),
            "-subj", f"/CN={ip}",
        )
        # SAN must include the LAN IP — iOS 14+ rejects certs without it
        san = d / "san.ext"
        san.write_text(f"subjectAltName=IP:{ip},IP:127.0.0.1\n")
        _run(
            "openssl", "x509", "-req", "-days", "825",
            "-in", str(csr),
            "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(srv_crt), "-extfile", str(san),
        )
        ip_stamp.write_text(ip)

    return ca_crt, srv_crt, srv_key


def _make_mobileconfig(ca_crt: Path) -> bytes:
    """
    Apple configuration profile embedding the CA cert.
    When Safari downloads this with the right Content-Type, iOS offers to install it.
    PEM is base64-encoded DER, so we strip the headers and embed directly in <data>.
    """
    pem = ca_crt.read_text()
    b64 = "".join(ln for ln in pem.splitlines() if not ln.startswith("-----"))
    xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>PayloadContent</key><array><dict>
    <key>PayloadCertificateFileName</key><string>screen-duo-ca.crt</string>
    <key>PayloadContent</key><data>{b64}</data>
    <key>PayloadDescription</key><string>Lets your iPhone stream to screen-duo on your local network</string>
    <key>PayloadDisplayName</key><string>screen-duo CA</string>
    <key>PayloadIdentifier</key><string>com.screenduo.ca</string>
    <key>PayloadType</key><string>com.apple.security.root</string>
    <key>PayloadUUID</key><string>A1B2C3D4-E5F6-7890-ABCD-EF1234567890</string>
    <key>PayloadVersion</key><integer>1</integer>
  </dict></array>
  <key>PayloadDescription</key><string>screen-duo local CA</string>
  <key>PayloadDisplayName</key><string>screen-duo</string>
  <key>PayloadIdentifier</key><string>com.screenduo</string>
  <key>PayloadType</key><string>Configuration</string>
  <key>PayloadUUID</key><string>F0E1D2C3-B4A5-6789-0123-456789ABCDEF</string>
  <key>PayloadVersion</key><integer>1</integer>
</dict></plist>"""
    return xml.encode()


# ──────────────────────────────────────────────────────────────────────── server

class WebRTCServer:
    """
    Local HTTPS WebRTC server.

    Call start() → returns (cert_url, camera_url).
    on_connected / on_disconnected fire from the asyncio thread;
    callers touching Qt must marshal via signals.
    """

    def __init__(self, v4l2_device: str, http_port: int = 8080, https_port: int = 8443):
        self.v4l2_device = v4l2_device
        self.http_port = http_port
        self.https_port = https_port
        self.on_connected: callable | None = None
        self.on_disconnected: callable | None = None

        self._ip: str = ""
        self._ca_crt: Path | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pc: RTCPeerConnection | None = None
        self._ffmpeg: subprocess.Popen | None = None

    def start(self) -> tuple[str, str]:
        """Start in a background thread. Returns (cert_url, camera_url)."""
        self._ip = _local_ip()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        cert_url = f"http://{self._ip}:{self.http_port}/ca.mobileconfig"
        camera_url = f"https://{self._ip}:{self.https_port}"
        return cert_url, camera_url

    def stop(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)
        self._stop_ffmpeg()

    # ── async internals ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        ca_crt, srv_crt, srv_key = await asyncio.get_event_loop().run_in_executor(
            None, _ensure_certs, self._ip
        )
        self._ca_crt = ca_crt

        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(str(srv_crt), str(srv_key))

        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/ca.mobileconfig", self._mobileconfig)
        app.router.add_post("/offer", self._handle_offer)

        runner = web.AppRunner(app)
        await runner.setup()
        # HTTP: cert download (no TLS — Safari can download profiles over plain HTTP)
        await web.TCPSite(runner, "0.0.0.0", self.http_port).start()
        # HTTPS: WebRTC camera page (getUserMedia requires a secure context)
        await web.TCPSite(runner, "0.0.0.0", self.https_port, ssl_context=ssl_ctx).start()

        try:
            await asyncio.Future()
        except (asyncio.CancelledError, RuntimeError):
            pass
        await runner.cleanup()

    async def _index(self, _: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def _mobileconfig(self, _: web.Request) -> web.Response:
        data = await asyncio.get_event_loop().run_in_executor(
            None, _make_mobileconfig, self._ca_crt
        )
        return web.Response(
            body=data,
            content_type="application/x-apple-aspen-config",
            headers={"Content-Disposition": "attachment; filename=screen-duo.mobileconfig"},
        )

    async def _handle_offer(self, request: web.Request) -> web.Response:
        data = await request.json()
        if self._pc:
            await self._pc.close()
        self._pc = RTCPeerConnection()

        @self._pc.on("track")
        async def on_track(track):
            if track.kind == "video":
                asyncio.ensure_future(self._consume(track))

        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=data["sdp"], type=data["type"])
        )
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        return web.json_response(
            {"sdp": self._pc.localDescription.sdp, "type": self._pc.localDescription.type}
        )

    async def _consume(self, track) -> None:
        """Read WebRTC frames and pump them into ffmpeg/v4l2loopback at a steady 30 fps.

        WebRTC's temporal encoder skips frames when the scene is static, which
        would otherwise starve v4l2loopback and make the preview/recording stall.
        We decouple receipt from writing: a reader task grabs frames as they
        arrive and stores the latest; a writer task ticks at 30 fps and repeats
        the last frame whenever no new one has arrived yet.
        """
        loop = asyncio.get_running_loop()
        target_w: int | None = None
        target_h: int | None = None
        last_data: bytes | None = None
        active = True

        async def _reader():
            nonlocal target_w, target_h, last_data, active
            while active:
                try:
                    frame = await track.recv()
                except Exception:
                    active = False
                    return
                if target_w is None:
                    w, h = frame.width, frame.height
                    # Cap at 1280 wide: keeps raw BGR24 frames ≤3.7 MB so the
                    # stdin write doesn't block long enough to drop the rate to 3fps.
                    if w > 1280:
                        h = int(h * 1280 / w) & ~1
                        w = 1280
                    target_w, target_h = w, h
                    await loop.run_in_executor(None, self._start_ffmpeg, target_w, target_h)
                    if self.on_connected:
                        self.on_connected()
                last_data = frame.reformat(
                    width=target_w, height=target_h, format="bgr24"
                ).to_ndarray().tobytes()

        async def _writer():
            nonlocal active
            while active:
                await asyncio.sleep(1.0 / 30)
                data = last_data
                if not data or not self._ffmpeg or not self._ffmpeg.stdin:
                    continue
                try:
                    await loop.run_in_executor(None, self._ffmpeg.stdin.write, data)
                except (BrokenPipeError, OSError):
                    active = False

        reader = asyncio.ensure_future(_reader())
        writer = asyncio.ensure_future(_writer())
        _, pending = await asyncio.wait([reader, writer], return_when=asyncio.FIRST_COMPLETED)
        active = False
        for t in pending:
            t.cancel()

        self._stop_ffmpeg()
        if self.on_disconnected:
            self.on_disconnected()

    # ── ffmpeg subprocess ─────────────────────────────────────────────────────

    def _start_ffmpeg(self, width: int, height: int) -> None:
        self._stop_ffmpeg()
        self._ffmpeg = subprocess.Popen(
            [
                "ffmpeg", "-loglevel", "quiet",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{width}x{height}", "-r", "30",
                "-i", "pipe:0",
                "-vf", "format=yuv420p",
                "-f", "v4l2", self.v4l2_device,
            ],
            stdin=subprocess.PIPE,
        )

    def _stop_ffmpeg(self) -> None:
        if not self._ffmpeg:
            return
        try:
            self._ffmpeg.stdin.write(b"q")
            self._ffmpeg.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._ffmpeg.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._ffmpeg.terminate()
            self._ffmpeg.wait()
        self._ffmpeg = None
