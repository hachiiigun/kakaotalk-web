"""KakaoTalk Local DB Web UI HTTP + SSE server."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

WEBUI_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEBUI_DIR / "static"
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from kakao_store import (  # noqa: E402
    KakaoStore,
    decrypt_kakao_webp,
    emoticon_png_filename_from_path,
    is_plain_webp,
)

WEBUI_PORT = int(os.environ.get("WEBUI_PORT", "14501"))
BRIDGE_URL = os.environ.get("KAKAO_BRIDGE_URL", "http://127.0.0.1:14502").rstrip("/")
UPLOAD_DIR = Path(os.environ.get("KAKAO_UPLOAD_DIR", "/tmp/kakao-uploads"))
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_PROXY_BYTES = 10 * 1024 * 1024
MAX_PROXY_CACHE_ITEM = 5 * 1024 * 1024
MAX_PROXY_CACHE_ITEMS = 200
SESSION_COOKIE = "kakao_webui_session"
SESSION_TTL_SEC = 30 * 24 * 3600

_ALLOWED_PROXY_SUFFIXES = (".kakao.com", ".kakaocdn.net", ".daumcdn.net")
_ALLOWED_PROXY_EXACT = {"kakao.com", "kakaocdn.net", "daumcdn.net"}
_EMOT_CODE_RE = re.compile(r"^\d{4,12}$")
_EMOT_PNG_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_EMOT_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.(?:png|webp)$", re.IGNORECASE)
_EMOT_SEND_FILE_RE = re.compile(r"^(\d{4,12})\.(?:thum|emot|emoji)_\d{1,4}\.png$")

_ENV_PW = os.environ.get("VOXL_PASSWORD", "").strip()
AUTH_PASSWORD = _ENV_PW if _ENV_PW else secrets.token_urlsafe(18)
SESSION_SECRET = secrets.token_bytes(32)


def sign_session(secret: bytes = SESSION_SECRET, now: int | None = None) -> str:
    ts = str(int(time.time()) if now is None else int(now))
    sig = hmac.new(secret, ts.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def verify_session(token: str | None, secret: bytes = SESSION_SECRET, now: int | None = None) -> bool:
    if not token or "." not in token:
        return False
    ts_str, sig = token.split(".", 1)
    if not ts_str.isdigit() or len(sig) != 64:
        return False
    expected = hmac.new(secret, ts_str.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    cur = int(time.time()) if now is None else int(now)
    age = cur - int(ts_str)
    return 0 <= age <= SESSION_TTL_SEC


def verify_password(candidate: str | None, expected: str = AUTH_PASSWORD) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def is_allowed_proxy_url(raw_url: str, resolve_dns: bool = True) -> tuple[bool, str]:
    """Strict SSRF guard permitting only public HTTPS URLs on Kakao/Daum CDNs."""
    try:
        parsed = urllib.parse.urlsplit(raw_url)
    except ValueError:
        return False, "invalid url"
    if parsed.scheme.lower() != "https":
        return False, "only https allowed"
    if parsed.username is not None or parsed.password is not None:
        return False, "credentials in url forbidden"
    if parsed.port not in (None, 443):
        return False, "non-443 port forbidden"
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return False, "missing host"
    if host not in _ALLOWED_PROXY_EXACT and not any(host.endswith(s) for s in _ALLOWED_PROXY_SUFFIXES):
        return False, "host not in kakao/daum allowlist"
    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            return False, "dns resolution failed"
        if not infos:
            return False, "no dns records"
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                return False, "invalid resolved ip"
            if not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return False, "non-public target ip forbidden"
    return True, ""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        abs_url = urllib.parse.urljoin(req.full_url, newurl)
        ok, reason = is_allowed_proxy_url(abs_url, resolve_dns=True)
        if not ok:
            raise urllib.error.HTTPError(abs_url, 403, f"Blocked redirect: {reason}", headers, None)
        return super().redirect_request(req, fp, code, msg, headers, abs_url)


_PROXY_OPENER = urllib.request.build_opener(_SafeRedirectHandler())
_PROXY_CACHE: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
_PROXY_LOCK = threading.Lock()


def fetch_proxied_resource(url: str) -> tuple[int, str, bytes]:
    ok, reason = is_allowed_proxy_url(url, resolve_dns=True)
    if not ok:
        return 403, "text/plain; charset=utf-8", reason.encode("utf-8")

    with _PROXY_LOCK:
        cached = _PROXY_CACHE.get(url)
        if cached is not None:
            _PROXY_CACHE.move_to_end(url)
            return 200, cached[0], cached[1]

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 KakaoTalkWebUI/1.0"})
    try:
        with _PROXY_OPENER.open(req, timeout=12) as resp:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            data = resp.read(MAX_PROXY_BYTES + 1)
            if len(data) > MAX_PROXY_BYTES:
                return 413, "text/plain; charset=utf-8", b"resource too large"
    except urllib.error.HTTPError as exc:
        return exc.code or 502, "text/plain; charset=utf-8", str(exc).encode("utf-8")
    except Exception as exc:  # noqa: BLE001
        return 502, "text/plain; charset=utf-8", str(exc).encode("utf-8")

    if len(data) <= MAX_PROXY_CACHE_ITEM:
        with _PROXY_LOCK:
            _PROXY_CACHE[url] = (ctype, data)
            _PROXY_CACHE.move_to_end(url)
            while len(_PROXY_CACHE) > MAX_PROXY_CACHE_ITEMS:
                _PROXY_CACHE.popitem(last=False)
    return 200, ctype, data


def is_valid_emoticon_asset(item_code: str, filename: str) -> bool:
    """Validate emoticon item code and PNG/WebP filename against path traversal."""
    if not isinstance(item_code, str) or not isinstance(filename, str):
        return False
    if ".." in filename or "/" in filename or "\\" in filename:
        return False
    return bool(_EMOT_CODE_RE.fullmatch(item_code) and _EMOT_ASSET_RE.fullmatch(filename))


def load_emoticon_png_bytes(store: KakaoStore, item_code: str, filename: str) -> tuple[int, str, bytes]:
    """Load emoticon WebP (decrypting local/CDN header obfuscation) or PNG bytes with PNG fallback."""
    if not is_valid_emoticon_asset(item_code, filename):
        return 400, "text/plain; charset=utf-8", b"invalid emoticon asset"

    if filename.lower().endswith(".webp"):
        raw: bytes | None = None
        local_p = store.resolve_emoticon_asset_path(item_code, filename)
        if local_p is not None:
            try:
                raw = local_p.read_bytes()
            except OSError:
                raw = None
        if raw is None:
            cdn_code, _cdn_ctype, cdn_data = fetch_proxied_resource(f"https://item.kakaocdn.net/dw/{filename}")
            if cdn_code == 200:
                raw = cdn_data
        if raw:
            dec = decrypt_kakao_webp(raw)
            if is_plain_webp(dec):
                return 200, "image/webp", dec
        fb = emoticon_png_filename_from_path(filename)
        if fb and fb[1] != filename:
            fb_code, fb_ctype, fb_data = load_emoticon_png_bytes(store, fb[0], fb[1])
            if fb_code == 200:
                return fb_code, fb_ctype, fb_data
        return 404, "text/plain; charset=utf-8", b"emoticon asset unavailable"

    local_p = store.resolve_emoticon_asset_path(item_code, filename)
    if local_p is not None:
        try:
            return 200, "image/png", local_p.read_bytes()
        except OSError:
            pass
    return fetch_proxied_resource(f"https://item.kakaocdn.net/dw/{filename}")


def bridge_call(method: str, path: str, payload: dict | None = None, timeout: float = 12.0) -> dict:
    url = f"{BRIDGE_URL}{path}"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "error": f"bridge HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bridge unavailable: {exc}"}


def sanitize_filename(raw_name: str) -> str:
    try:
        decoded = urllib.parse.unquote(raw_name or "")
    except Exception:
        decoded = raw_name or ""
    base = Path(decoded.replace("\\", "/")).name
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .")
    return clean[:120] or "upload.bin"


def _delayed_unlink(path: Path, delay: float = 8.0) -> None:
    def _rm():
        time.sleep(delay)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    threading.Thread(target=_rm, daemon=True).start()


_LOGIN_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>카카오톡 웹 로그인</title>
<style>
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"Noto Sans KR","Malgun Gothic",sans-serif;
         background:#1E2229; color:#F2F4F7; display:flex; align-items:center; justify-content:center; min-height:100vh; }
  .card { background:#2A2F3A; padding:32px; border-radius:16px; width:min(360px,90vw); box-shadow:0 16px 40px rgba(0,0,0,.35); }
  .badge { display:inline-block; background:#FEE500; color:#191919; font-weight:700; font-size:12px; padding:4px 10px; border-radius:999px; margin-bottom:12px; }
  h1 { margin:0 0 8px; font-size:20px; }
  p { margin:0 0 20px; font-size:13px; color:#A8B0BE; line-height:1.5; }
  input { width:100%; box-sizing:border-box; padding:12px 14px; border-radius:10px; border:1px solid #3D4454;
          background:#181B22; color:#fff; font-size:15px; outline:none; }
  input:focus { border-color:#FEE500; }
  button { width:100%; margin-top:12px; padding:12px; border:0; border-radius:10px; background:#FEE500; color:#191919;
           font-weight:700; font-size:15px; cursor:pointer; }
  button:hover { filter:brightness(0.95); }
  .err { color:#FF7B7B; font-size:13px; margin-top:10px; min-height:18px; }
</style>
</head>
<body>
  <form class="card" id="f">
    <span class="badge">KakaoTalk Web UI</span>
    <h1>비밀번호 입력</h1>
    <p><code>.env</code>에 설정한 <code>VOXL_PASSWORD</code> 또는 컨테이너 로그의 접속 토큰을 입력하세요.</p>
    <input type="password" id="pw" placeholder="비밀번호" autofocus required />
    <button type="submit">열기</button>
    <div class="err" id="err">__ERR__</div>
  </form>
  <script>
    document.getElementById('f').addEventListener('submit', async (e) => {
      e.preventDefault();
      const pw = document.getElementById('pw').value;
      const r = await fetch('/api/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({password: pw})
      });
      if (r.ok) location.replace('/');
      else document.getElementById('err').textContent = '비밀번호가 올바르지 않습니다.';
    });
  </script>
</body>
</html>
"""


class KakaoWebServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], store: KakaoStore) -> None:
        self.store = store
        super().__init__(addr, KakaoRequestHandler)


class KakaoRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15.0

    def log_message(self, fmt: str, *args) -> None:
        # Never log query tokens or sensitive parameters
        pass

    @property
    def store(self) -> KakaoStore:
        return self.server.store  # type: ignore[attr-defined]

    def _is_authenticated(self) -> bool:
        hdr_tok = self.headers.get("X-Auth-Token")
        if hdr_tok and verify_password(hdr_tok):
            return True
        raw_cookie = self.headers.get("Cookie")
        if raw_cookie:
            c = SimpleCookie()
            try:
                c.load(raw_cookie)
            except Exception:
                return False
            m = c.get(SESSION_COOKIE)
            if m and verify_session(m.value):
                return True
        return False

    def _send_bytes(self, code: int, ctype: str, body: bytes, extra_headers: dict[str, str] | None = None) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, code: int, obj: dict | list, extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(code, "application/json; charset=utf-8", body, extra_headers)

    def _read_body(self, limit: int = 256 * 1024) -> bytes | None:
        try:
            cl = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "invalid content-length"})
            return None
        if cl < 0 or cl > limit:
            self._send_json(413, {"ok": False, "error": "payload too large"})
            return None
        return self.rfile.read(cl) if cl > 0 else b""

    def _read_json(self) -> dict | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                self._send_json(400, {"ok": False, "error": "expected json object"})
                return None
            return obj
        except (UnicodeDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return None

    def _serve_static(self, rel_path: str) -> None:
        clean = rel_path.lstrip("/") or "index.html"
        target = (STATIC_DIR / clean).resolve()
        try:
            static_root = STATIC_DIR.resolve()
            if not target.is_relative_to(static_root) or not target.is_file():
                self._send_json(404, {"ok": False, "error": "not found"})
                return
        except Exception:
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        ctype, _ = mimetypes.guess_type(str(target))
        if not ctype:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send_bytes(200, ctype, target.read_bytes(), {"Cache-Control": "no-cache"})

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/health":
            self._send_json(200, {"ok": True})
            return

        if path == "/" and "token" in qs:
            tok = qs["token"][0]
            if verify_password(tok):
                cookie = f"{SESSION_COOKIE}={sign_session()}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL_SEC}"
                self.close_connection = True
                self._send_bytes(
                    302,
                    "text/plain; charset=utf-8",
                    b"Redirecting",
                    {"Location": "/", "Set-Cookie": cookie, "Connection": "close"},
                )
            else:
                html = _LOGIN_HTML.replace("__ERR__", "토큰이 올바르지 않습니다.").encode("utf-8")
                self._send_bytes(401, "text/html; charset=utf-8", html)
            return

        if not self._is_authenticated():
            if path == "/" or path == "/index.html":
                html = _LOGIN_HTML.replace("__ERR__", "").encode("utf-8")
                self._send_bytes(200, "text/html; charset=utf-8", html)
            else:
                self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return

        if path == "/api/status":
            st = self.store.status()
            probe = bridge_call("GET", "/probe", timeout=2.5)
            st["bridge_ok"] = bool(probe.get("ok"))
            st["bridge_main_hwnd"] = probe.get("main_hwnd", 0)
            st["bridge_chat_windows"] = len(probe.get("chat_windows") or [])
            st["voxl_port"] = 14500
            self._send_json(200, st)
            return

        if path == "/api/rooms":
            tab = qs.get("tab", ["all"])[0]
            q = qs.get("q", [""])[0]
            try:
                rooms = self.store.list_rooms(tab=tab, q=q)
                self._send_json(200, {"ok": True, "rooms": rooms})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/friends":
            q = qs.get("q", [""])[0]
            lim = int(qs.get("limit", ["250"])[0] or "250")
            try:
                friends = self.store.list_friends(q=q, limit=lim)
                self._send_json(200, {"ok": True, "friends": friends})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        m_msg = re.fullmatch(r"/api/rooms/(\d+)/messages", path)
        if m_msg:
            chat_id = m_msg.group(1)
            lim = int(qs.get("limit", ["80"])[0] or "80")
            before = int(qs.get("before_log_id", ["0"])[0] or "0")
            q = qs.get("q", [""])[0]
            try:
                res = self.store.get_messages(chat_id, limit=lim, before_log_id=before, q=q)
                if not res.get("found"):
                    self._send_json(404, {"ok": False, "error": "room not found"})
                    return
                res["ok"] = True
                self._send_json(200, res)
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        if path == "/api/proxy":
            raw_url = qs.get("url", [""])[0]
            code, ctype, data = fetch_proxied_resource(raw_url)
            hdrs = {"Cache-Control": "private, max-age=3600"} if code == 200 else None
            self._send_bytes(code, ctype, data, hdrs)
            return

        if path == "/api/emoticons":
            try:
                emot_data = self.store.get_emoticons()
                self._send_json(200, {"ok": True, **emot_data})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"ok": False, "error": str(exc)})
            return

        m_ea = re.fullmatch(r"/api/emoticon-asset/([^/]+)/([^/]+)", path)
        if m_ea:
            item_code = urllib.parse.unquote(m_ea.group(1))
            filename = urllib.parse.unquote(m_ea.group(2))
            code, ctype, data = load_emoticon_png_bytes(self.store, item_code, filename)
            hdrs = {"Cache-Control": "private, max-age=86400"} if code == 200 else None
            self._send_bytes(code, ctype, data, hdrs)
            return

        if path == "/api/events":
            self._handle_sse()
            return

        self._send_json(404, {"ok": False, "error": "not found"})

    def _handle_sse(self) -> None:
        self.close_connection = True
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        q, ev = self.store.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"event: connected\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                triggered = ev.wait(timeout=15.0)
                ev.clear()
                while q:
                    msg = q.pop(0)
                    payload = f"event: {msg['event']}\ndata: {json.dumps(msg['data'], ensure_ascii=False)}\n\n"
                    self.wfile.write(payload.encode("utf-8"))
                if not triggered:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.store.unsubscribe(q, ev)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/api/login":
            data = self._read_json()
            if data is None:
                return
            if verify_password(data.get("password")):
                cookie = f"{SESSION_COOKIE}={sign_session()}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL_SEC}"
                self._send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            else:
                self._send_json(401, {"ok": False, "error": "invalid password"})
            return

        if path == "/api/logout":
            cookie = f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
            self._send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return

        if not self._is_authenticated():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return

        if path == "/api/scan":
            found = self.store.scan_all_open_keys()
            st = self.store.status()
            self._send_json(200, {"ok": True, "newly_unlocked": found, **st})
            return

        m_unlock = re.fullmatch(r"/api/rooms/(\d+)/unlock", path)
        if m_unlock:
            chat_id = m_unlock.group(1)
            room = self.store.get_room(chat_id)
            if not room:
                self._send_json(404, {"ok": False, "error": "room not found"})
                return
            if self.store.scan_single_room_key(chat_id):
                self._send_json(200, {"ok": True, "unlocked": True, "via": "memory"})
                return
            alt = room.get("alt_title") or room.get("raw_title") or ""
            if alt == room["name"]:
                alt = ""
            res = bridge_call("POST", "/open", {"room": room["name"], "alt_room": alt}, timeout=12.0)
            last_err = res.get("error", "")
            opened = bool(res.get("ok"))
            if opened:
                for _ in range(4):
                    time.sleep(0.5)
                    if self.store.scan_single_room_key(chat_id):
                        self._send_json(200, {"ok": True, "unlocked": True, "via": "auto_open"})
                        return
                self._send_json(200, {
                    "ok": False,
                    "unlocked": False,
                    "error": "채팅방을 열었지만 DB 키를 아직 찾지 못했습니다. 잠시 후 다시 시도해 주세요.",
                })
                return
            # Even if window title matching missed, check once whether KakaoTalk touched the DB in RAM
            if self.store.scan_single_room_key(chat_id):
                self._send_json(200, {"ok": True, "unlocked": True, "via": "memory_after_open"})
                return
            self._send_json(200, {
                "ok": False,
                "unlocked": False,
                "error": last_err or "채팅방을 자동으로 열지 못했습니다. 원격 화면(:14500)에서 해당 방을 한 번 열어 주세요.",
            })
            return

        m_send = re.fullmatch(r"/api/rooms/(\d+)/send", path)
        if m_send:
            chat_id = m_send.group(1)
            room = self.store.get_room(chat_id)
            if not room:
                self._send_json(404, {"ok": False, "error": "room not found"})
                return
            if not room["writable"]:
                self._send_json(403, {"ok": False, "error": "채널(PlusChat)에는 메시지를 보낼 수 없습니다."})
                return
            data = self._read_json()
            if data is None:
                return
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json(400, {"ok": False, "error": "메시지 내용을 입력해 주세요."})
                return
            if len(text) > 2000:
                self._send_json(400, {"ok": False, "error": "메시지는 2000자 이내로 입력해 주세요."})
                return
            alt = room.get("alt_title") or room.get("raw_title") or ""
            if alt == room["name"]:
                alt = ""
            res = bridge_call("POST", "/send", {"room": room["name"], "alt_room": alt, "text": text}, timeout=15.0)
            if res.get("ok"):
                threading.Thread(target=self.store.scan_single_room_key, args=(chat_id,), daemon=True).start()
                self._send_json(200, res)
            else:
                self._send_json(502, res)
            return

        m_up = re.fullmatch(r"/api/rooms/(\d+)/upload", path)
        if m_up:
            chat_id = m_up.group(1)
            room = self.store.get_room(chat_id)
            if not room:
                self._send_json(404, {"ok": False, "error": "room not found"})
                return
            if not room["writable"]:
                self._send_json(403, {"ok": False, "error": "채널(PlusChat)에는 파일을 보낼 수 없습니다."})
                return
            body = self._read_body(limit=MAX_UPLOAD_BYTES)
            if body is None:
                return
            if not body:
                self._send_json(400, {"ok": False, "error": "빈 파일입니다."})
                return
            fname = sanitize_filename(self.headers.get("X-Filename", "upload.bin"))
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(UPLOAD_DIR, 0o700)
            except OSError:
                pass
            saved = UPLOAD_DIR / f"{uuid.uuid4().hex[:10]}_{fname}"
            saved.write_bytes(body)
            win_path = "Z:" + str(saved).replace("/", "\\")
            alt = room.get("alt_title") or room.get("raw_title") or ""
            if alt == room["name"]:
                alt = ""
            res = bridge_call("POST", "/send-file", {"room": room["name"], "alt_room": alt, "win_path": win_path}, timeout=20.0)
            _delayed_unlink(saved, 20.0)
            if res.get("ok"):
                self._send_json(200, res)
            else:
                self._send_json(502, res)
            return

        m_se = re.fullmatch(r"/api/rooms/(\d+)/send-emoticon", path)
        if m_se:
            chat_id = m_se.group(1)
            room = self.store.get_room(chat_id)
            if not room:
                self._send_json(404, {"ok": False, "error": "room not found"})
                return
            if not room["writable"]:
                self._send_json(403, {"ok": False, "error": "채널(PlusChat)에는 이모티콘을 보낼 수 없습니다."})
                return
            data = self._read_json()
            if data is None:
                return
            item_code = str(data.get("item_code") or "").strip()
            filename = str(data.get("filename") or "").strip()
            kind = str(data.get("kind") or "").strip()
            try:
                pack_pos = int(data["pack_pos"]) if "pack_pos" in data and data["pack_pos"] is not None else -1
                grid_idx = int(data["grid_idx"]) if "grid_idx" in data and data["grid_idx"] is not None else -1
            except (TypeError, ValueError):
                pack_pos, grid_idx = -1, -1
            dry_run = bool(data.get("dry_run", False))

            if (kind not in ("sticker", "emoji") or pack_pos < 0 or grid_idx < 0) and item_code and filename:
                resolved = self.store.resolve_emoticon_target(item_code, filename)
                if resolved:
                    kind = resolved["kind"]
                    pack_pos = resolved["pack_pos"]
                    grid_idx = resolved["grid_idx"]

            if kind not in ("sticker", "emoji") or pack_pos < 0 or pack_pos >= 64 or grid_idx < 0 or grid_idx >= 256:
                self._send_json(400, {"ok": False, "error": "이모티콘 위치 정보를 확인할 수 없습니다."})
                return

            alt = room.get("alt_title") or room.get("raw_title") or ""
            if alt == room["name"]:
                alt = ""
            res = bridge_call(
                "POST",
                "/send-emoticon",
                {
                    "room": room["name"],
                    "alt_room": alt,
                    "kind": kind,
                    "pack_pos": pack_pos,
                    "grid_idx": grid_idx,
                    "dry_run": dry_run,
                },
                timeout=20.0,
            )
            if res.get("ok"):
                if not dry_run:
                    threading.Thread(target=self.store.scan_single_room_key, args=(chat_id,), daemon=True).start()
                self._send_json(200, res)
            else:
                self._send_json(502, res)
            return

        self._send_json(404, {"ok": False, "error": "not found"})


def _bootstrap_store(store: KakaoStore) -> None:
    # Retry briefly on container startup while Wine & KakaoTalk initialize
    for _ in range(12):
        try:
            st = store.status()
            if st["kakaotalk_running"] and st["account_found"]:
                store.scan_all_open_keys()
                store.ensure_loaded()
                return
        except Exception:
            pass
        time.sleep(1.5)


def main() -> None:
    store = KakaoStore()
    store.start_watcher()
    threading.Thread(target=_bootstrap_store, args=(store,), daemon=True).start()

    if _ENV_PW:
        print(f"KakaoTalk Web UI listening on 0.0.0.0:{WEBUI_PORT} (VOXL_PASSWORD auth enabled)", flush=True)
    else:
        print(f"KakaoTalk Web UI ready: http://localhost:{WEBUI_PORT}/?token={AUTH_PASSWORD}", flush=True)

    srv = KakaoWebServer(("0.0.0.0", WEBUI_PORT), store)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.stop_watcher()
        srv.server_close()


if __name__ == "__main__":
    main()
