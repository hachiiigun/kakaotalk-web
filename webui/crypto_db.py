"""Pure-Python SQLCipher 4 + WAL in-memory reader and /proc/<pid>/mem key scanner.

Uses only Python 3.12 stdlib + OpenSSL libcrypto (via ctypes) so it requires zero
pip installs inside the container. Keys are never logged or exposed in API responses.
"""
from __future__ import annotations

from collections import OrderedDict
import ctypes
import ctypes.util
import glob
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

SQLITE_MAGIC = b"SQLite format 3\x00"
PAGE_SIZE = 4096
RESERVE_SIZE = 80
WAL_MAGICS = (0x377F0682, 0x377F0683)
_HEX64_RE = re.compile(rb"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")

_DEFAULT_USERS_GLOB = "/data/wineprefix/drive_c/users/*/AppData/Local/Kakao/KakaoTalk/users/*/chat_data/chatListInfo.edb"
_DEFAULT_KEY_CACHE = "/data/kakao-webui/keys.json"


class _LibCrypto:
    """Thread-safe OpenSSL EVP AES-256-CBC wrapper (creates a fresh context per call)."""

    def __init__(self) -> None:
        lib_path = ctypes.util.find_library("crypto") or "libcrypto.so.3"
        try:
            self._lib = ctypes.CDLL(lib_path)
        except OSError:
            self._lib = ctypes.CDLL("libcrypto.so.1.1")
        lib = self._lib
        self._evp_aes_256_cbc = lib.EVP_aes_256_cbc
        self._evp_aes_256_cbc.restype = ctypes.c_void_p
        self._ctx_new = lib.EVP_CIPHER_CTX_new
        self._ctx_new.restype = ctypes.c_void_p
        self._ctx_free = lib.EVP_CIPHER_CTX_free
        self._ctx_free.argtypes = [ctypes.c_void_p]
        self._dec_init = lib.EVP_DecryptInit_ex
        self._dec_init.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_char_p, ctypes.c_char_p,
        ]
        self._dec_init.restype = ctypes.c_int
        self._set_padding = lib.EVP_CIPHER_CTX_set_padding
        self._set_padding.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._set_padding.restype = ctypes.c_int
        self._dec_update = lib.EVP_DecryptUpdate
        self._dec_update.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p, ctypes.c_int,
        ]
        self._dec_update.restype = ctypes.c_int
        self._dec_final = lib.EVP_DecryptFinal_ex
        self._dec_final.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
        ]
        self._dec_final.restype = ctypes.c_int

    def decrypt_with_ctx(self, ctx, out_buf, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        if not self._dec_init(ctx, self._evp_aes_256_cbc(), None, key, iv):
            raise ValueError("EVP_DecryptInit_ex failed")
        self._set_padding(ctx, 0)
        outl = ctypes.c_int(0)
        finl = ctypes.c_int(0)
        if not self._dec_update(ctx, ctypes.byref(out_buf), ctypes.byref(outl), ciphertext, len(ciphertext)):
            raise ValueError("EVP_DecryptUpdate failed")
        if not self._dec_final(ctx, ctypes.c_void_p(ctypes.addressof(out_buf) + outl.value), ctypes.byref(finl)):
            raise ValueError("EVP_DecryptFinal_ex failed")
        return bytes(out_buf[: outl.value + finl.value])

    def aes256_cbc_decrypt(self, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        ctx = self._ctx_new()
        if not ctx:
            raise MemoryError("EVP_CIPHER_CTX_new failed")
        out_buf = (ctypes.c_ubyte * (len(ciphertext) + 16))()
        try:
            return self.decrypt_with_ctx(ctx, out_buf, key, iv, ciphertext)
        finally:
            self._ctx_free(ctx)


_CRYPTO: _LibCrypto | None = None
_CRYPTO_LOCK = threading.Lock()
_BASE_DB_CACHE: OrderedDict[tuple[str, int, int, bytes], tuple[bytes, bytes]] = OrderedDict()
_BASE_CACHE_LOCK = threading.Lock()
_MAX_BASE_CACHE = 6


def _crypto() -> _LibCrypto:
    global _CRYPTO
    if _CRYPTO is None:
        with _CRYPTO_LOCK:
            if _CRYPTO is None:
                _CRYPTO = _LibCrypto()
    return _CRYPTO


def derive_hmac_key(enc_key: bytes, db_salt: bytes) -> bytes:
    if len(enc_key) != 32 or len(db_salt) != 16:
        raise ValueError("expected 32-byte key and 16-byte salt")
    hmac_salt = bytes(b ^ 0x3A for b in db_salt)
    return hashlib.pbkdf2_hmac("sha512", enc_key, hmac_salt, 2, dklen=32)


def _page_hmac(hmac_key: bytes, ciphertext: bytes, iv: bytes, pgno: int) -> bytes:
    return hmac.new(hmac_key, ciphertext + iv + pgno.to_bytes(4, "little"), hashlib.sha512).digest()


def verify_sqlcipher4_key(first_page: bytes, enc_key: bytes) -> bool:
    """Authenticate page 1 against SQLCipher 4 (4096B page, reserve 80, HMAC-SHA512)."""
    if len(enc_key) != 32 or len(first_page) < PAGE_SIZE:
        return False
    page1 = first_page[:PAGE_SIZE]
    hk = derive_hmac_key(enc_key, page1[:16])
    ct = page1[16 : PAGE_SIZE - RESERVE_SIZE]
    iv = page1[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - 64]
    mac = page1[PAGE_SIZE - 64 : PAGE_SIZE]
    return hmac.compare_digest(_page_hmac(hk, ct, iv, 1), mac)


def _decrypt_page(
    page: bytes,
    pgno: int,
    enc_key: bytes,
    hmac_key: bytes,
    crypto: _LibCrypto | None = None,
    ctx=None,
    out_buf=None,
) -> bytes:
    if len(page) != PAGE_SIZE:
        raise ValueError(f"page {pgno}: expected {PAGE_SIZE} bytes, got {len(page)}")
    off = 16 if pgno == 1 else 0
    ct_end = PAGE_SIZE - RESERVE_SIZE
    ct = page[off:ct_end]
    iv = page[ct_end : ct_end + 16]
    mac = page[ct_end + 16 : PAGE_SIZE]
    if not hmac.compare_digest(_page_hmac(hmac_key, ct, iv, pgno), mac):
        raise ValueError(f"page {pgno}: HMAC mismatch")
    c = crypto or _crypto()
    if ctx is not None and out_buf is not None:
        plain = c.decrypt_with_ctx(ctx, out_buf, enc_key, iv, ct)
    else:
        plain = c.aes256_cbc_decrypt(enc_key, iv, ct)
    if pgno == 1:
        plain = SQLITE_MAGIC + plain
    return plain + (b"\x00" * RESERVE_SIZE)


def _decrypt_base_pages(encrypted_db: bytes, enc_key: bytes, hmac_key: bytes, crypto: _LibCrypto, ctx, out_buf) -> bytes:
    n_pages = len(encrypted_db) // PAGE_SIZE
    out = bytearray()
    for i in range(n_pages):
        out.extend(
            _decrypt_page(
                encrypted_db[i * PAGE_SIZE : (i + 1) * PAGE_SIZE],
                i + 1,
                enc_key,
                hmac_key,
                crypto=crypto,
                ctx=ctx,
                out_buf=out_buf,
            )
        )
    return bytes(out)


def decrypt_bytes_with_wal(
    encrypted_db: bytes,
    wal_bytes: bytes,
    enc_key: bytes,
    *,
    predecrypted_base: bytes | None = None,
    base_salt: bytes | None = None,
) -> tuple[bytes, int]:
    """Decrypt main SQLCipher 4 image and overlay committed WAL frames in RAM."""
    if len(enc_key) != 32:
        raise ValueError("enc_key must be 32 bytes")
    if predecrypted_base is not None:
        if len(predecrypted_base) < PAGE_SIZE or len(predecrypted_base) % PAGE_SIZE != 0:
            raise ValueError("predecrypted base must be a non-empty multiple of 4096 bytes")
        salt = base_salt if base_salt is not None else encrypted_db[:16]
        if len(salt) != 16:
            raise ValueError("missing 16-byte DB salt for predecrypted base")
        hmac_key = derive_hmac_key(enc_key, salt)
        n_pages = len(predecrypted_base) // PAGE_SIZE
        out = bytearray(predecrypted_base)
    else:
        if not encrypted_db or len(encrypted_db) % PAGE_SIZE != 0:
            raise ValueError("encrypted DB must be a non-empty multiple of 4096 bytes")
        hmac_key = derive_hmac_key(enc_key, encrypted_db[:16])
        n_pages = len(encrypted_db) // PAGE_SIZE
        out = bytearray()

    crypto = _crypto()
    ctx = crypto._ctx_new()
    if not ctx:
        raise MemoryError("EVP_CIPHER_CTX_new failed")
    out_buf = (ctypes.c_ubyte * (PAGE_SIZE + 16))()
    try:
        if predecrypted_base is None:
            out = bytearray(_decrypt_base_pages(encrypted_db, enc_key, hmac_key, crypto, ctx, out_buf))

        wal_frames = 0
        if len(wal_bytes) >= 32 and int.from_bytes(wal_bytes[:4], "big") in WAL_MAGICS:
            ps = int.from_bytes(wal_bytes[8:12], "big")
            if ps == PAGE_SIZE:
                wsalt = wal_bytes[16:24]
                committed: dict[int, bytes] = {}
                pending: dict[int, bytes] = {}
                db_pages = n_pages
                off = 32
                while off + 24 + PAGE_SIZE <= len(wal_bytes):
                    fh = wal_bytes[off : off + 24]
                    if fh[8:16] != wsalt:
                        break
                    pgno = int.from_bytes(fh[:4], "big")
                    commit = int.from_bytes(fh[4:8], "big")
                    try:
                        pending[pgno] = _decrypt_page(
                            wal_bytes[off + 24 : off + 24 + PAGE_SIZE],
                            pgno,
                            enc_key,
                            hmac_key,
                            crypto=crypto,
                            ctx=ctx,
                            out_buf=out_buf,
                        )
                    except ValueError:
                        break
                    off += 24 + PAGE_SIZE
                    if commit:
                        committed.update(pending)
                        wal_frames += len(pending)
                        pending.clear()
                        db_pages = commit
                if committed:
                    out = bytearray(bytes(out[: db_pages * PAGE_SIZE]).ljust(db_pages * PAGE_SIZE, b"\x00"))
                    for pgno, pdata in committed.items():
                        if 1 <= pgno <= db_pages:
                            out[(pgno - 1) * PAGE_SIZE : pgno * PAGE_SIZE] = pdata

        if len(out) >= 20 and (out[18] == 2 or out[19] == 2):
            out[18] = 1
            out[19] = 1
        return bytes(out), wal_frames
    finally:
        crypto._ctx_free(ctx)


def _read_wal_if_stable(edb_path: str, expected_size: int, expected_mtime: int) -> bytes | None:
    wal_path = edb_path + "-wal"
    wal = b""
    if os.path.exists(wal_path):
        with open(wal_path, "rb") as wf:
            wst1 = os.fstat(wf.fileno())
            wal = wf.read()
            wst2 = os.fstat(wf.fileno())
        if (wst1.st_size, wst1.st_mtime_ns) != (wst2.st_size, wst2.st_mtime_ns):
            return None
    st_now = os.stat(edb_path)
    if (st_now.st_size, st_now.st_mtime_ns) != (expected_size, expected_mtime):
        return None
    return wal


def _read_stable_pair(edb_path: str) -> tuple[bytes, bytes, int, int]:
    """Read .edb and optional .edb-wal while verifying mtime/size did not change mid-read."""
    wal_path = edb_path + "-wal"
    last_err: Exception | None = None
    for _ in range(4):
        try:
            with open(edb_path, "rb") as f:
                st1 = os.fstat(f.fileno())
                edb = f.read()
                st2 = os.fstat(f.fileno())
            if (st1.st_size, st1.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns) or len(edb) != st1.st_size:
                time.sleep(0.12)
                continue
            wal = b""
            if os.path.exists(wal_path):
                with open(wal_path, "rb") as wf:
                    wst1 = os.fstat(wf.fileno())
                    wal = wf.read()
                    wst2 = os.fstat(wf.fileno())
                if (wst1.st_size, wst1.st_mtime_ns) != (wst2.st_size, wst2.st_mtime_ns):
                    time.sleep(0.12)
                    continue
            return edb, wal, st1.st_size, st1.st_mtime_ns
        except OSError as exc:
            last_err = exc
            time.sleep(0.12)
    if last_err is not None:
        raise last_err
    raise OSError(f"database file changed continuously while reading: {os.path.basename(edb_path)}")


def open_decrypted_connection(edb_path: str, enc_key: bytes) -> sqlite3.Connection:
    """Decrypt .edb + -wal and open a read-only in-memory SQLite connection."""
    key_tag = hashlib.sha256(enc_key).digest()[:8]
    last_err: Exception | None = None
    for _ in range(3):
        try:
            st = os.stat(edb_path)
            cache_key = (edb_path, st.st_mtime_ns, st.st_size, key_tag)
            with _BASE_CACHE_LOCK:
                cached = _BASE_DB_CACHE.get(cache_key)
                if cached is not None:
                    _BASE_DB_CACHE.move_to_end(cache_key)

            if cached is not None:
                base_salt, cached_base = cached
                wal_bytes = _read_wal_if_stable(edb_path, st.st_size, st.st_mtime_ns)
                if wal_bytes is None:
                    time.sleep(0.12)
                    continue
                plain, _ = decrypt_bytes_with_wal(
                    b"", wal_bytes, enc_key, predecrypted_base=cached_base, base_salt=base_salt
                )
            else:
                edb_bytes, wal_bytes, sz, mtime_ns = _read_stable_pair(edb_path)
                if not edb_bytes or len(edb_bytes) % PAGE_SIZE != 0:
                    raise ValueError("encrypted DB must be a non-empty multiple of 4096 bytes")
                hmac_key = derive_hmac_key(enc_key, edb_bytes[:16])
                crypto = _crypto()
                ctx = crypto._ctx_new()
                if not ctx:
                    raise MemoryError("EVP_CIPHER_CTX_new failed")
                out_buf = (ctypes.c_ubyte * (PAGE_SIZE + 16))()
                try:
                    base_plain = _decrypt_base_pages(edb_bytes, enc_key, hmac_key, crypto, ctx, out_buf)
                finally:
                    crypto._ctx_free(ctx)
                cache_key = (edb_path, mtime_ns, sz, key_tag)
                with _BASE_CACHE_LOCK:
                    _BASE_DB_CACHE[cache_key] = (edb_bytes[:16], base_plain)
                    _BASE_DB_CACHE.move_to_end(cache_key)
                    while len(_BASE_DB_CACHE) > _MAX_BASE_CACHE:
                        _BASE_DB_CACHE.popitem(last=False)
                plain, _ = decrypt_bytes_with_wal(
                    b"", wal_bytes, enc_key, predecrypted_base=base_plain, base_salt=edb_bytes[:16]
                )

            conn = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                conn.deserialize(plain)
                conn.execute("PRAGMA query_only=ON")
                conn.row_factory = sqlite3.Row
                return conn
            except Exception:
                conn.close()
                raise
        except (ValueError, OSError, sqlite3.DatabaseError) as exc:
            last_err = exc
            time.sleep(0.15)
    assert last_err is not None
    raise last_err


def file_signature(edb_path: str) -> tuple:
    sig = []
    for p in (edb_path, edb_path + "-wal"):
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def find_kakaotalk_pid() -> int | None:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "ignore")
            if "KakaoTalk.exe" in cmd and "bash" not in cmd and "dbus" not in cmd:
                return int(entry)
        except OSError:
            continue
    return None


def find_account_dir(glob_pattern: str = _DEFAULT_USERS_GLOB) -> str | None:
    hits = sorted(glob.glob(glob_pattern), key=os.path.getmtime, reverse=True)
    if not hits:
        return None
    # .../users/<hash>/chat_data/chatListInfo.edb -> .../users/<hash>
    return str(Path(hits[0]).parent.parent)


class KeyStore:
    """Manages cached per-DB SQLCipher 4 keys and discovers keys from KakaoTalk.exe RAM."""

    def __init__(self, cache_path: str = _DEFAULT_KEY_CACHE) -> None:
        self.cache_path = Path(cache_path)
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._keys: dict[str, bytes] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for name, hex_key in raw.items():
            if isinstance(name, str) and isinstance(hex_key, str) and len(hex_key) == 64:
                try:
                    self._keys[name] = bytes.fromhex(hex_key)
                except ValueError:
                    continue

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.cache_path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps({k: v.hex() for k, v in sorted(self._keys.items())}, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(prefix=".keys_", suffix=".tmp", dir=str(self.cache_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cache_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key_id: str) -> bytes | None:
        with self._lock:
            return self._keys.get(key_id)

    def has(self, key_id: str) -> bool:
        with self._lock:
            return key_id in self._keys

    def invalidate_if_stale(self, key_id: str, edb_path: str) -> bool:
        """Only drop a cached key if page 1 genuinely fails HMAC verification."""
        key = self.get(key_id)
        if not key:
            return True
        try:
            with open(edb_path, "rb") as f:
                p1 = f.read(PAGE_SIZE)
            if len(p1) == PAGE_SIZE and not verify_sqlcipher4_key(p1, key):
                with self._lock:
                    self._keys.pop(key_id, None)
                    self._save()
                return True
        except OSError:
            pass
        return False

    def scan_memory(self, targets: dict[str, str], include_hex_fallback: bool = False, fast_only: bool = False) -> int:
        """Scan live KakaoTalk.exe /proc/<pid>/mem for SQLCipher 4 keys matching `targets`.

        `targets` maps canonical key_id (e.g. 'TalkUserDB.edb', 'chatLogs_123.edb') -> absolute path.
        Returns count of newly discovered keys.
        """
        if not self._scan_lock.acquire(timeout=3.0):
            return 0
        try:
            pending: dict[str, tuple[bytes, bytes]] = {}
            with self._lock:
                cached = dict(self._keys)
            for kid, path in targets.items():
                if kid in cached:
                    continue
                try:
                    with open(path, "rb") as f:
                        p1 = f.read(PAGE_SIZE)
                    if len(p1) == PAGE_SIZE:
                        pending[kid] = (p1, p1[:16])
                except OSError:
                    continue

            if not pending:
                return 0
            pid = find_kakaotalk_pid()
            if pid is None:
                return 0

            regions: list[tuple[int, int]] = []
            try:
                with open(f"/proc/{pid}/maps", encoding="utf-8", errors="ignore") as mf:
                    for line in mf:
                        parts = line.split()
                        if len(parts) < 2:
                            continue
                        perms = parts[1]
                        if "r" not in perms or "w" not in perms:
                            continue
                        s_hex, e_hex = parts[0].split("-")
                        s, e = int(s_hex, 16), int(e_hex, 16)
                        sz = e - s
                        if 0 < sz <= 128 * 1024 * 1024:
                            regions.append((s, sz))
            except OSError:
                return 0

            chunk_size = 4 * 1024 * 1024
            chunk_overlap = 1024
            found_now: dict[str, bytes] = {}
            fallback_cands: dict[str, set[bytes]] = {}
            hex_cands: set[bytes] = set()

            try:
                with open(f"/proc/{pid}/mem", "rb", buffering=0) as mem:
                    for start, size in regions:
                        if not pending:
                            break
                        offset = 0
                        while offset < size and pending:
                            read_len = min(chunk_size, size - offset)
                            try:
                                mem.seek(start + offset)
                                buf = mem.read(read_len)
                            except OSError:
                                break
                            if not buf:
                                break
                            if include_hex_fallback and len(hex_cands) < 4096:
                                for m in _HEX64_RE.finditer(buf):
                                    try:
                                        hex_cands.add(bytes.fromhex(m.group().decode("ascii")))
                                        if len(hex_cands) >= 4096:
                                            break
                                    except ValueError:
                                        pass

                            for kid in list(pending.keys()):
                                p1, salt = pending[kid]
                                idx = 0
                                matched = False
                                while True:
                                    pos = buf.find(salt, idx)
                                    if pos == -1:
                                        break
                                    # Fast path: observed KakaoTalk 26.x memory struct places raw key at salt + 144
                                    if pos + 176 <= len(buf):
                                        cand = buf[pos + 144 : pos + 176]
                                        if verify_sqlcipher4_key(p1, cand):
                                            found_now[kid] = cand
                                            del pending[kid]
                                            fallback_cands.pop(kid, None)
                                            matched = True
                                            break
                                    # Collect nearby aligned 32-byte windows in case offset shifts in future updates
                                    if not fast_only:
                                        bucket = fallback_cands.setdefault(kid, set())
                                        if len(bucket) < 256:
                                            lo = max(0, pos - 512)
                                            hi = min(len(buf) - 32, pos + 512)
                                            for off in range(lo - (lo % 8), hi + 1, 8):
                                                cand = buf[off : off + 32]
                                                if len(set(cand)) >= 18:
                                                    bucket.add(cand)
                                                    if len(bucket) >= 256:
                                                        break
                                    idx = pos + 1
                                if matched:
                                    continue

                            if offset + read_len >= size:
                                break
                            offset += read_len - chunk_overlap
            except OSError:
                pass

            if not fast_only:
                for kid in list(pending.keys()):
                    p1, _ = pending[kid]
                    for cand in fallback_cands.get(kid, ()):
                        if verify_sqlcipher4_key(p1, cand):
                            found_now[kid] = cand
                            del pending[kid]
                            break
                    if kid in pending and hex_cands:
                        for cand in hex_cands:
                            if verify_sqlcipher4_key(p1, cand):
                                found_now[kid] = cand
                                del pending[kid]
                                break

            if found_now:
                with self._lock:
                    self._keys.update(found_now)
                    self._save()
            return len(found_now)
        finally:
            self._scan_lock.release()
