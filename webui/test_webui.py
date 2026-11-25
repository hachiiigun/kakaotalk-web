"""Unit tests for SQLCipher 4 + WAL decryption, SSRF guard, and auth helpers."""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

WEBUI_DIR = Path(__file__).resolve().parent
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from crypto_db import PAGE_SIZE, RESERVE_SIZE, decrypt_bytes_with_wal, derive_hmac_key, verify_sqlcipher4_key
from kakao_store import (
    _KAKAO_WEBP_XOR_PAD,
    decrypt_kakao_webp,
    emoticon_play_filename_from_path,
    emoticon_png_filename_from_path,
    format_last_preview,
    is_plain_webp,
)
from server import (
    is_allowed_proxy_url,
    is_valid_emoticon_asset,
    sanitize_filename,
    sign_session,
    verify_password,
    verify_session,
)


def _aes256_cbc_encrypt(key: bytes, iv: bytes, pt: bytes) -> bytes:
    lib = ctypes.CDLL(ctypes.util.find_library("crypto") or "libcrypto.so.3")
    ctx_new = lib.EVP_CIPHER_CTX_new
    ctx_new.restype = ctypes.c_void_p
    ctx_free = lib.EVP_CIPHER_CTX_free
    ctx_free.argtypes = [ctypes.c_void_p]
    evp = lib.EVP_aes_256_cbc
    evp.restype = ctypes.c_void_p
    enc_init = lib.EVP_EncryptInit_ex
    enc_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    set_pad = lib.EVP_CIPHER_CTX_set_padding
    set_pad.argtypes = [ctypes.c_void_p, ctypes.c_int]
    enc_upd = lib.EVP_EncryptUpdate
    enc_upd.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, ctypes.c_int]
    enc_fin = lib.EVP_EncryptFinal_ex
    enc_fin.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]

    ctx = ctx_new()
    try:
        enc_init(ctx, evp(), None, key, iv)
        set_pad(ctx, 0)
        out = (ctypes.c_ubyte * (len(pt) + 16))()
        ol = ctypes.c_int(0)
        fl = ctypes.c_int(0)
        enc_upd(ctx, ctypes.byref(out), ctypes.byref(ol), pt, len(pt))
        enc_fin(ctx, ctypes.c_void_p(ctypes.addressof(out) + ol.value), ctypes.byref(fl))
        return bytes(out[: ol.value + fl.value])
    finally:
        ctx_free(ctx)


def _encrypt_page(plain: bytes, pgno: int, enc_key: bytes, db_salt: bytes) -> bytes:
    assert len(plain) == PAGE_SIZE
    hk = derive_hmac_key(enc_key, db_salt)
    iv = hashlib.sha256(db_salt + pgno.to_bytes(4, "little")).digest()[:16]
    pt = plain[16 : PAGE_SIZE - RESERVE_SIZE] if pgno == 1 else plain[: PAGE_SIZE - RESERVE_SIZE]
    ct = _aes256_cbc_encrypt(enc_key, iv, pt)
    mac = hmac.new(hk, ct + iv + pgno.to_bytes(4, "little"), hashlib.sha512).digest()
    prefix = db_salt if pgno == 1 else b""
    return prefix + ct + iv + mac


def _make_sqlite_with_reserve(sql_stmts: list[str]) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        p = tf.name
    try:
        conn = sqlite3.connect(p)
        conn.execute(f"PRAGMA page_size = {PAGE_SIZE}")
        for st in sql_stmts:
            conn.execute(st)
        conn.commit()
        conn.close()
        raw = bytearray(Path(p).read_bytes())
        # Shift B-tree page cell content left by RESERVE_SIZE so SQLite reserves 80 trailing bytes per page
        raw[20] = RESERVE_SIZE
        n_pages = len(raw) // PAGE_SIZE
        for i in range(n_pages):
            pg = i * PAGE_SIZE
            hdr = 100 if i == 0 else 0
            n_cells = int.from_bytes(raw[pg + hdr + 3 : pg + hdr + 5], "big")
            cell_start = int.from_bytes(raw[pg + hdr + 5 : pg + hdr + 7], "big") or 65536
            if n_cells > 0 and cell_start <= PAGE_SIZE:
                content = bytes(raw[pg + cell_start : pg + PAGE_SIZE])
                new_start = cell_start - RESERVE_SIZE
                raw[pg + new_start : pg + PAGE_SIZE - RESERVE_SIZE] = content
                raw[pg + hdr + 5 : pg + hdr + 7] = new_start.to_bytes(2, "big")
                for c in range(n_cells):
                    cp = pg + hdr + 8 + 2 * c
                    old_off = int.from_bytes(raw[cp : cp + 2], "big")
                    raw[cp : cp + 2] = (old_off - RESERVE_SIZE).to_bytes(2, "big")
            raw[pg + PAGE_SIZE - RESERVE_SIZE : pg + PAGE_SIZE] = b"\x00" * RESERVE_SIZE
        return bytes(raw)
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass


class TestCryptoAndWal(unittest.TestCase):
    def test_roundtrip_sqlcipher4_and_wal_overlay(self) -> None:
        key = os.urandom(32)
        db_salt = os.urandom(16)
        base_plain = _make_sqlite_with_reserve([
            "CREATE TABLE t(id INTEGER PRIMARY KEY, msg TEXT)",
            "INSERT INTO t VALUES(1, 'hello baseline')",
        ])
        new_plain = _make_sqlite_with_reserve([
            "CREATE TABLE t(id INTEGER PRIMARY KEY, msg TEXT)",
            "INSERT INTO t VALUES(1, 'hello baseline')",
            "INSERT INTO t VALUES(2, 'from wal frame')",
        ])

        enc_base = b"".join(
            _encrypt_page(base_plain[i * PAGE_SIZE : (i + 1) * PAGE_SIZE], i + 1, key, db_salt)
            for i in range(len(base_plain) // PAGE_SIZE)
        )
        self.assertTrue(verify_sqlcipher4_key(enc_base[:PAGE_SIZE], key))
        self.assertFalse(verify_sqlcipher4_key(enc_base[:PAGE_SIZE], os.urandom(32)))

        # Build WAL containing all pages from new_plain with commit on final frame
        wsalt = os.urandom(8)
        wal = bytearray()
        wal.extend((0x377F0682).to_bytes(4, "big"))
        wal.extend((3007000).to_bytes(4, "big"))
        wal.extend(PAGE_SIZE.to_bytes(4, "big"))
        wal.extend((0).to_bytes(4, "big"))
        wal.extend(wsalt)
        wal.extend(b"\x00" * 8)
        n_new = len(new_plain) // PAGE_SIZE
        for i in range(n_new):
            pgno = i + 1
            commit = n_new if pgno == n_new else 0
            wal.extend(pgno.to_bytes(4, "big"))
            wal.extend(commit.to_bytes(4, "big"))
            wal.extend(wsalt)
            wal.extend(b"\x00" * 8)
            wal.extend(_encrypt_page(new_plain[i * PAGE_SIZE : (i + 1) * PAGE_SIZE], pgno, key, db_salt))

        dec_bytes, applied = decrypt_bytes_with_wal(enc_base, bytes(wal), key)
        self.assertEqual(applied, n_new)

        conn = sqlite3.connect(":memory:")
        conn.deserialize(dec_bytes)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchall(), [("ok",)])
        rows = conn.execute("SELECT id, msg FROM t ORDER BY id").fetchall()
        self.assertEqual(rows, [(1, "hello baseline"), (2, "from wal frame")])
        conn.close()


class TestSecurityAndHelpers(unittest.TestCase):
    def test_ssrf_allowlist(self) -> None:
        self.assertTrue(is_allowed_proxy_url("https://k.kakaocdn.net/dn/abc.jpg", resolve_dns=False)[0])
        self.assertTrue(is_allowed_proxy_url("https://talk.kakaocdn.net/dna/123", resolve_dns=False)[0])
        self.assertTrue(is_allowed_proxy_url("https://t1.daumcdn.net/msg/1.png", resolve_dns=False)[0])
        self.assertFalse(is_allowed_proxy_url("http://k.kakaocdn.net/a.jpg", resolve_dns=False)[0])
        self.assertFalse(is_allowed_proxy_url("https://127.0.0.1/a.jpg", resolve_dns=False)[0])
        self.assertFalse(is_allowed_proxy_url("https://kakao.com.evil.com/a.jpg", resolve_dns=False)[0])
        self.assertFalse(is_allowed_proxy_url("https://user:pass@k.kakaocdn.net/a.jpg", resolve_dns=False)[0])
        self.assertFalse(is_allowed_proxy_url("https://k.kakaocdn.net:8443/a.jpg", resolve_dns=False)[0])

    def test_auth_and_session(self) -> None:
        secret = b"0" * 32
        tok = sign_session(secret, now=1000)
        self.assertTrue(verify_session(tok, secret, now=1005))
        self.assertFalse(verify_session(tok + "x", secret, now=1005))
        self.assertFalse(verify_session(tok, secret, now=1000 + 31 * 86400))
        self.assertTrue(verify_password("secret-pw", "secret-pw"))
        self.assertFalse(verify_password("wrong", "secret-pw"))

    def test_sanitize_filename_and_preview(self) -> None:
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("C:\\Users\\test\\사진.png"), "사진.png")
        self.assertEqual(format_last_preview(18, "", '{"name":"report.pdf"}'), "[파일] report.pdf")
        self.assertEqual(format_last_preview(2, "사진", ""), "[사진]")


class TestEmoticonHelpers(unittest.TestCase):
    def test_emoticon_path_normalization(self) -> None:
        self.assertEqual(
            emoticon_png_filename_from_path("4419790.emot_006.webp"),
            ("4419790", "4419790.thum_006.png"),
        )
        self.assertEqual(
            emoticon_png_filename_from_path("dw/2212560.emot_001.png", prefer_static_full=True),
            ("2212560", "2212560.emot_001.png"),
        )
        self.assertEqual(
            emoticon_png_filename_from_path("dw/1400100.emoji_001.webp", prefer_static_full=True),
            ("1400100", "1400100.thum_001.png"),
        )
        self.assertIsNone(emoticon_png_filename_from_path("../evil.png"))

    def test_emoticon_asset_validation(self) -> None:
        self.assertTrue(is_valid_emoticon_asset("4419790", "4419790.thum_001.png"))
        self.assertTrue(is_valid_emoticon_asset("4419790", "4419790.emot_001.webp"))
        self.assertFalse(is_valid_emoticon_asset("4419790", "4419790.emot_001.gif"))
        self.assertFalse(is_valid_emoticon_asset("../123", "a.png"))
        self.assertFalse(is_valid_emoticon_asset("4419790", "../a.png"))
        self.assertFalse(is_valid_emoticon_asset("4419790", "../a.webp"))

    def test_emoticon_play_path_and_webp_decrypt(self) -> None:
        self.assertEqual(
            emoticon_play_filename_from_path("dw/4419790.emot_006.webp"),
            ("4419790", "4419790.emot_006.webp"),
        )
        self.assertEqual(
            emoticon_play_filename_from_path("dw/1400100.emoji_001.webp"),
            ("1400100", "1400100.emoji_001.webp"),
        )
        self.assertIsNone(emoticon_play_filename_from_path("../evil.webp"))
        self.assertEqual(_KAKAO_WEBP_XOR_PAD[:16].hex(), "e7ad425b16f1811966423c44678d44b8")

        plain = b"RIFF" + (152).to_bytes(4, "little") + b"WEBPVP8X" + (b"\x11\x22\x33\x44" * 36)
        self.assertEqual(len(plain), 160)
        obfuscated = bytes(a ^ b for a, b in zip(plain[:128], _KAKAO_WEBP_XOR_PAD)) + plain[128:]
        self.assertFalse(is_plain_webp(obfuscated))
        self.assertEqual(decrypt_kakao_webp(obfuscated), plain)
        self.assertEqual(decrypt_kakao_webp(plain), plain)


if __name__ == "__main__":
    unittest.main()
