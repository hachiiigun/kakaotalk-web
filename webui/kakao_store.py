"""High-level KakaoTalk data access, attachment normalization, and SSE change watcher."""
from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path

from crypto_db import (
    KeyStore,
    file_signature,
    find_account_dir,
    find_kakaotalk_pid,
    open_decrypted_connection,
)

KST = dt.timezone(dt.timedelta(hours=9))
MAX_CACHED_ROOMS = 16
_CHAT_ID_RE = re.compile(r"^\d+$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_EMOT_CODE_RE = re.compile(r"^\d{4,12}$")
_EMOT_PNG_RE = re.compile(r"^[A-Za-z0-9._-]+\.png$")
_EMOT_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.(?:png|webp)$", re.IGNORECASE)
_EMOT_PATH_RE = re.compile(r"(?:^|/)(\d{4,12})\.(thum|emot|emoji)_(\d{1,4})\.(png|webp)$", re.IGNORECASE)


def emoticon_png_filename_from_path(raw_path: str | None, prefer_static_full: bool = False) -> tuple[str, str] | None:
    """Normalize a KakaoTalk emoticon path (including obfuscated .webp) into (item_code, plain_png_filename)."""
    if not raw_path or not isinstance(raw_path, str):
        return None
    clean = raw_path.strip().replace("\\", "/")
    m = _EMOT_PATH_RE.search(clean)
    if not m:
        return None
    code, kind, idx, ext = m.group(1), m.group(2).lower(), m.group(3), m.group(4).lower()
    if prefer_static_full and ext == "png":
        return code, f"{code}.{kind}_{idx}.png"
    return code, f"{code}.thum_{idx}.png"


def emoticon_play_filename_from_path(raw_path: str | None) -> tuple[str, str] | None:
    """Extract (item_code, filename) preserving animated .webp when present in raw_path."""
    if not raw_path or not isinstance(raw_path, str):
        return None
    clean = raw_path.strip().replace("\\", "/")
    m = _EMOT_PATH_RE.search(clean)
    if not m:
        return None
    code, kind, idx, ext = m.group(1), m.group(2).lower(), m.group(3), m.group(4).lower()
    return code, f"{code}.{kind}_{idx}.{ext}"


def _build_kakao_webp_xor_pad(
    key: str = "a271730728cbe141e47fd9d677e9006d", length: int = 128
) -> bytes:
    """Generate KakaoTalk's 128-byte LFSR-XOR header pad for DigitalItem WebP assets."""
    kb = (key * 2).encode("ascii")
    s0 = int.from_bytes(kb[0:4], "big")
    s1 = int.from_bytes(kb[4:8], "big")
    s2 = int.from_bytes(kb[8:12], "big")
    out = bytearray(length)
    for idx in range(length):
        flag1 = 1
        flag2 = 0
        res = 0
        for _ in range(8):
            if s0 & 1:
                s0 = (s0 >> 1) ^ 0xC0000031
                if s1 & 1:
                    s1 = ((s1 >> 1) | 0xC0000000) ^ 0x20000010
                    flag1 = 1
                else:
                    s1 = (s1 >> 1) & 0x3FFFFFFF
                    flag1 = 0
            else:
                s0 >>= 1
                if s2 & 1:
                    s2 = ((s2 >> 1) | 0xF0000000) ^ 0x08000001
                    flag2 = 1
                else:
                    s2 = (s2 >> 1) & 0x0FFFFFFF
                    flag2 = 0
            res = ((res << 1) | (flag1 ^ flag2)) & 0xFF
        out[idx] = res
    return bytes(out)


_KAKAO_WEBP_XOR_PAD = _build_kakao_webp_xor_pad()


def is_plain_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def decrypt_kakao_webp(data: bytes) -> bytes:
    """Decode KakaoTalk's 128-byte LFSR-XOR header obfuscation on .webp emoticons."""
    if not data or is_plain_webp(data):
        return data
    n = min(len(data), len(_KAKAO_WEBP_XOR_PAD))
    head = bytes(a ^ b for a, b in zip(data[:n], _KAKAO_WEBP_XOR_PAD[:n]))
    return head + data[n:]

TYPE_LABELS = {
    "DirectChat": "1:1 채팅",
    "MultiChat": "그룹 채팅",
    "OM": "오픈채팅",
    "OD": "1:1 오픈채팅",
    "MemoChat": "나와의 채팅",
    "PlusChat": "채널",
}

MESSAGE_PLACEHOLDERS = {
    2: "[사진]",
    3: "[동영상]",
    5: "[음성 메시지]",
    12: "[이모티콘]",
    14: "[투표]",
    16: "[위치]",
    17: "[연락처]",
    18: "[파일]",
    20: "[이모티콘]",
    22: "[샵검색]",
    23: "[선물]",
    24: "[알림]",
    25: "[이모티콘]",
    26: "[답장]",
    27: "[사진 묶음]",
    51: "[통화]",
    71: "[알림톡/카드]",
    73: "[알림]",
}


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def proxy_url(url: str | None) -> str:
    if not url or not isinstance(url, str):
        return ""
    u = url.strip()
    if not u.startswith("https://"):
        return ""
    return "/api/proxy?url=" + urllib.parse.quote(u, safe="")


def _parse_json_obj(raw: str | None) -> dict | list | None:
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or (s[0] not in "{["):
        return None
    try:
        return json.loads(s)
    except ValueError:
        return None


def _format_feed_text(raw_msg: str, names_for_room: dict[str, str]) -> str:
    obj = _parse_json_obj(raw_msg)
    if isinstance(obj, dict):
        ft = obj.get("feedType")
        member = obj.get("member")
        members = obj.get("members")
        names: list[str] = []
        if isinstance(member, dict):
            nm = member.get("nickName") or names_for_room.get(str(member.get("userId", "")))
            if nm:
                names.append(str(nm))
        if isinstance(members, list):
            for m in members:
                if isinstance(m, dict):
                    nm = m.get("nickName") or names_for_room.get(str(m.get("userId", "")))
                    if nm:
                        names.append(str(nm))
        joined = ", ".join(names[:5]) + (f" 외 {len(names) - 5}명" if len(names) > 5 else "")
        if ft == 4:
            return f"{joined}님이 들어왔습니다." if joined else "새 멤버가 입장했습니다."
        if ft == 2:
            return f"{joined}님이 나갔습니다." if joined else "멤버가 나갔습니다."
        if ft == 6:
            return f"{joined}님이 내보내졌습니다." if joined else "멤버가 퇴장했습니다."
        if ft == 14:
            return "메시지가 삭제되었습니다."
        if obj.get("text"):
            return str(obj["text"])
        return "[대화 알림]"
    return raw_msg or "[알림]"


def _extract_rich_card(raw_msg: str, raw_att: str) -> tuple[str, dict | None]:
    obj = _parse_json_obj(raw_att) or _parse_json_obj(raw_msg)
    if not isinstance(obj, dict):
        return (raw_msg or "[알림 카드]"), None
    p = obj.get("P") if isinstance(obj.get("P"), dict) else {}
    c = obj.get("C") if isinstance(obj.get("C"), dict) else {}
    title = ""
    desc = ""
    link = ""
    if isinstance(p, dict):
        title = str(p.get("TP") or p.get("TT") or "")
        desc = str(p.get("ME") or p.get("DESC") or "")
    thl = c.get("THL") if isinstance(c, dict) else None
    if isinstance(thl, list) and thl and isinstance(thl[0], dict):
        th = thl[0].get("TH") or {}
        if isinstance(th, dict) and not title:
            title = str(th.get("T") or "")
    hd = c.get("HD") if isinstance(c, dict) else None
    if isinstance(hd, dict):
        td = hd.get("TD") or {}
        if isinstance(td, dict) and not title:
            title = str(td.get("T") or "")
    if isinstance(c, dict) and isinstance(c.get("ITL"), list) and c["ITL"]:
        first = c["ITL"][0]
        if isinstance(first, dict):
            td = first.get("TD") or {}
            if isinstance(td, dict):
                title = title or str(td.get("T") or "")
                desc = desc or str(td.get("D") or "")
    # Find any https link inside C/P
    m = _URL_RE.search(json.dumps(obj, ensure_ascii=False))
    if m:
        link = m.group(0).rstrip('",]}')
    summary = title or desc or (raw_msg if raw_msg and not raw_msg.startswith("{") else "") or "[알림톡/카드]"
    card = {"title": title or summary, "description": desc, "url": link}
    return summary, card


def format_last_preview(typ: int, raw_msg: str | None, raw_att: str | None = None) -> str:
    msg = (raw_msg or "").strip()
    if typ in (1, 26):
        if msg:
            return msg
    if typ == 18:
        att = _parse_json_obj(raw_att)
        if isinstance(att, dict) and att.get("name"):
            return f"[파일] {att['name']}"
        return "[파일]"
    if typ == 24:
        return _format_feed_text(msg, {})
    if typ == 71:
        summary, _ = _extract_rich_card(msg, raw_att or "")
        return summary
    if typ in MESSAGE_PLACEHOLDERS:
        base = MESSAGE_PLACEHOLDERS[typ]
        return f"{base} {msg}".strip() if typ in (12, 20, 25) and msg else base
    return msg or f"[메시지 {typ}]"


class KakaoStore:
    """Thread-safe cache + incremental reloader for KakaoTalk local databases."""

    def __init__(self, key_cache_path: str | None = None) -> None:
        self.keys = KeyStore(key_cache_path) if key_cache_path else KeyStore()
        self._lock = threading.RLock()
        self._account_dir: str | None = None
        self._my_id: str = ""
        self._users: dict[str, dict] = {}
        self._open_users: dict[tuple[str, str], dict] = {}
        self._rooms: list[dict] = []
        self._rooms_by_id: dict[str, dict] = {}
        self._room_watermarks: dict[str, list[tuple[str, int]]] = {}
        self._user_sig: tuple | None = None
        self._list_sig: tuple | None = None
        self._last_rooms_reload: float = 0.0
        self._chat_cache: OrderedDict[str, tuple[tuple, list[dict]]] = OrderedDict()
        self._emoticon_sig: tuple | None = None
        self._emoticon_cache: dict | None = None

        # SSE subscribers
        self._sub_lock = threading.Lock()
        self._subscribers: list[list[dict]] = []
        self._sub_events: list[threading.Event] = []
        self._stop_watch = threading.Event()
        self._watcher: threading.Thread | None = None

    def start_watcher(self) -> None:
        if self._watcher is not None:
            return
        self._watcher = threading.Thread(target=self._watch_loop, name="kakao-db-watcher", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop_watch.set()

    def subscribe(self) -> tuple[list[dict], threading.Event]:
        q: list[dict] = []
        ev = threading.Event()
        with self._sub_lock:
            self._subscribers.append(q)
            self._sub_events.append(ev)
        return q, ev

    def unsubscribe(self, q: list[dict], ev: threading.Event) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
            if ev in self._sub_events:
                self._sub_events.remove(ev)

    def _broadcast(self, event_name: str, data: dict) -> None:
        with self._sub_lock:
            for q, ev in zip(self._subscribers, self._sub_events):
                if len(q) < 64:
                    q.append({"event": event_name, "data": data})
                ev.set()

    def _acc(self) -> str | None:
        acc = find_account_dir()
        if acc:
            self._account_dir = acc
        return self._account_dir

    def _db_path(self, key_id: str) -> str | None:
        acc = self._acc()
        if not acc:
            return None
        if key_id in ("TalkUserDB.edb", "emoticon.edb"):
            return os.path.join(acc, key_id)
        return os.path.join(acc, "chat_data", key_id)

    def _ensure_core_keys(self) -> None:
        acc = self._acc()
        if not acc:
            return
        targets: dict[str, str] = {}
        for kid in ("TalkUserDB.edb", "chatListInfo.edb", "emoticon.edb"):
            p = self._db_path(kid)
            if p and os.path.exists(p):
                self.keys.invalidate_if_stale(kid, p)
                if not self.keys.has(kid):
                    targets[kid] = p
        if targets:
            self.keys.scan_memory(targets, include_hex_fallback=True)

    def scan_all_open_keys(self) -> int:
        acc = self._acc()
        if not acc:
            return 0
        targets: dict[str, str] = {}
        for kid in ("TalkUserDB.edb", "chatListInfo.edb", "emoticon.edb"):
            p = self._db_path(kid)
            if p and os.path.exists(p):
                self.keys.invalidate_if_stale(kid, p)
                if not self.keys.has(kid):
                    targets[kid] = p
        chat_dir = Path(acc) / "chat_data"
        if chat_dir.is_dir():
            wal_targets: list[tuple[float, str, str]] = []
            for p in chat_dir.glob("chatLogs_*.edb"):
                kid = p.name
                if self.keys.has(kid):
                    continue
                sp = str(p)
                if os.path.exists(sp + "-wal") or os.path.exists(sp + "-shm"):
                    try:
                        mtime = p.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    wal_targets.append((mtime, kid, sp))
            wal_targets.sort(reverse=True)
            for _, kid, sp in wal_targets:
                targets[kid] = sp
        if not targets:
            return 0
        found = self.keys.scan_memory(targets, include_hex_fallback=False, fast_only=True)
        if found:
            self._refresh_room_unlock_flags()
            self._broadcast("rooms_updated", {"reason": "scan", "found": found})
        return found

    def scan_single_room_key(self, chat_id: str) -> bool:
        if not _CHAT_ID_RE.match(chat_id):
            return False
        kid = f"chatLogs_{chat_id}.edb"
        p = self._db_path(kid)
        if not p or not os.path.exists(p):
            return False
        self.keys.invalidate_if_stale(kid, p)
        if self.keys.has(kid):
            return True
        self.keys.scan_memory({kid: p}, include_hex_fallback=False)
        ok = self.keys.has(kid)
        if ok:
            self._refresh_room_unlock_flags()
            self._broadcast("rooms_updated", {"reason": "unlock", "chat_id": chat_id})
        return ok

    def _refresh_room_unlock_flags(self) -> None:
        with self._lock:
            for r in self._rooms:
                r["unlocked"] = self.keys.has(f"chatLogs_{r['id']}.edb")

    def _load_users_if_changed(self) -> bool:
        p = self._db_path("TalkUserDB.edb")
        if not p or not os.path.exists(p):
            return False
        sig = file_signature(p)
        if sig == self._user_sig and self._users:
            return False
        key = self.keys.get("TalkUserDB.edb")
        if not key:
            self._ensure_core_keys()
            key = self.keys.get("TalkUserDB.edb")
        if not key:
            return False
        conn = open_decrypted_connection(p, key)
        try:
            users: dict[str, dict] = {}
            open_users: dict[tuple[str, str], dict] = {}
            sql = (
                "SELECT userId, linkId, type, nickName, friendNickName, profileImageUrl, "
                "statusMessage, directChatId, hidden, purged, favorite "
                "FROM talkUser WHERE COALESCE(purged, 0) = 0"
            )
            for row in conn.execute(sql):
                uid = str(row["userId"])
                lid = str(_safe_int(row["linkId"]))
                fnick = (row["friendNickName"] or "").strip()
                nick = (row["nickName"] or "").strip()
                display = fnick or nick or f"사용자 {uid[-4:]}"
                info = {
                    "id": uid,
                    "link_id": lid,
                    "name": display,
                    "nick": nick,
                    "friend_nick": fnick,
                    "avatar": proxy_url(row["profileImageUrl"]),
                    "status": (row["statusMessage"] or "").strip(),
                    "direct_chat_id": str(_safe_int(row["directChatId"]) or ""),
                    "user_type": _safe_int(row["type"]),
                    "hidden": bool(_safe_int(row["hidden"])),
                    "favorite": bool(_safe_int(row["favorite"])),
                }
                if lid == "0" or uid not in users:
                    users[uid] = info
                if lid != "0":
                    open_users[(uid, lid)] = info
            with self._lock:
                self._users = users
                self._open_users = open_users
                self._user_sig = sig
            return True
        finally:
            conn.close()

    def _resolve_user(self, uid: str, link_id: str = "0") -> dict | None:
        if link_id and link_id != "0":
            u = self._open_users.get((uid, link_id))
            if u:
                return u
        return self._users.get(uid)

    def _load_rooms_if_changed(self) -> bool:
        p = self._db_path("chatListInfo.edb")
        if not p or not os.path.exists(p):
            return False
        sig = file_signature(p)
        if sig == self._list_sig and self._rooms:
            return False
        key = self.keys.get("chatListInfo.edb")
        if not key:
            self._ensure_core_keys()
            key = self.keys.get("chatListInfo.edb")
        if not key:
            return False

        self._load_users_if_changed()
        conn = open_decrypted_connection(p, key)
        try:
            members: dict[str, list[tuple[str, int]]] = {}
            freq: dict[str, int] = {}
            for r in conn.execute("SELECT chatId, userId, COALESCE(watermark, 0) AS wm FROM chatMembers WHERE COALESCE(isActive, 1) = 1"):
                cid = str(r["chatId"])
                uid = str(r["userId"])
                wm = _safe_int(r["wm"])
                members.setdefault(cid, []).append((uid, wm))
                freq[uid] = freq.get(uid, 0) + 1

            # MemoChat always has at most 1 member (my_id)
            my_id = max(freq, key=freq.get) if freq else ""
            raw_rooms = list(conn.execute(
                "SELECT chatId, type, activeMembersCount, newMessageCount, useCustomChatRoomTitle, "
                "chatRoomTitle, lastUpdatedAt, lastChatMessage, lastLogId, chatRoomImageUrl, "
                "isFavorite, alarmOff, directChatMemberId, lastChatMessageType, linkId, "
                "lastChatMessageAttachment "
                "FROM chatRoomList"
            ))
            for r in raw_rooms:
                if r["type"] == "MemoChat":
                    cid = str(r["chatId"])
                    ms = members.get(cid, [])
                    if ms:
                        my_id = ms[0][0]
                    break

            rooms: list[dict] = []
            by_id: dict[str, dict] = {}
            wms: dict[str, list[tuple[str, int]]] = {}

            for r in raw_rooms:
                cid = str(r["chatId"])
                typ = str(r["type"] or "")
                link_id = str(_safe_int(r["linkId"]))
                room_members = members.get(cid, [])
                peer_ids = [uid for uid, _ in room_members if uid != my_id]
                wms[cid] = [(uid, wm) for uid, wm in room_members if uid != my_id]

                raw_title = (r["chatRoomTitle"] or "").strip()
                use_custom = _safe_int(r["useCustomChatRoomTitle"])
                known: list[str] = []
                if typ == "MemoChat":
                    name = "나와의 채팅"
                elif use_custom and raw_title:
                    name = raw_title
                elif typ in ("DirectChat", "PlusChat", "OD"):
                    partner = str(_safe_int(r["directChatMemberId"]) or (peer_ids[0] if peer_ids else ""))
                    u = self._resolve_user(partner, link_id)
                    name = (u["name"] if u else "") or raw_title or f"{TYPE_LABELS.get(typ, typ)} {cid[-4:]}"
                elif raw_title:
                    name = raw_title
                else:
                    for uid in peer_ids[:6]:
                        u = self._resolve_user(uid, link_id)
                        if u and u["name"]:
                            known.append(u["name"])
                    if known:
                        name = ", ".join(known[:4]) + (f" 외 {len(peer_ids) - 4}명" if len(peer_ids) > 4 else "")
                    else:
                        name = f"{TYPE_LABELS.get(typ, typ or '채팅방')} {cid[-4:]}"

                # Resolve up to 4 avatars
                avatars: list[str] = []
                room_img = proxy_url(r["chatRoomImageUrl"])
                if room_img:
                    avatars.append(room_img)
                elif typ == "MemoChat":
                    me = self._resolve_user(my_id)
                    if me and me["avatar"]:
                        avatars.append(me["avatar"])
                elif typ in ("DirectChat", "PlusChat", "OD"):
                    partner = str(_safe_int(r["directChatMemberId"]) or (peer_ids[0] if peer_ids else ""))
                    u = self._resolve_user(partner, link_id)
                    if u and u["avatar"]:
                        avatars.append(u["avatar"])
                else:
                    for uid in peer_ids[:4]:
                        u = self._resolve_user(uid, link_id)
                        if u and u["avatar"]:
                            avatars.append(u["avatar"])

                last_typ = _safe_int(r["lastChatMessageType"], 1)
                last_preview = format_last_preview(last_typ, r["lastChatMessage"], r["lastChatMessageAttachment"])
                me_user = self._resolve_user(my_id)
                if typ == "MemoChat":
                    alt_title = raw_title or (me_user["nick"] if me_user and me_user.get("nick") else "")
                elif typ == "MultiChat" and not raw_title and known:
                    alt_title = ", ".join(known[:3])
                else:
                    alt_title = raw_title
                item = {
                    "id": cid,
                    "type": typ,
                    "type_label": TYPE_LABELS.get(typ, typ),
                    "name": name,
                    "raw_title": raw_title,
                    "alt_title": alt_title,
                    "link_id": link_id,
                    "member_count": _safe_int(r["activeMembersCount"], len(room_members)),
                    "unread_count": _safe_int(r["newMessageCount"]),
                    "last_at": _safe_int(r["lastUpdatedAt"]),
                    "last_log_id": _safe_int(r["lastLogId"]),
                    "last_message": last_preview,
                    "avatars": avatars,
                    "favorite": bool(_safe_int(r["isFavorite"])),
                    "alarm_off": bool(_safe_int(r["alarmOff"])),
                    "unlocked": self.keys.has(f"chatLogs_{cid}.edb"),
                    "writable": typ != "PlusChat",
                }
                rooms.append(item)
                by_id[cid] = item

            rooms.sort(key=lambda x: (not x["favorite"], -x["last_at"]))
            with self._lock:
                self._my_id = my_id
                self._rooms = rooms
                self._rooms_by_id = by_id
                self._room_watermarks = wms
                self._list_sig = sig
            return True
        finally:
            conn.close()

    def ensure_loaded(self) -> None:
        self._ensure_core_keys()
        self._load_users_if_changed()
        self._load_rooms_if_changed()

    def status(self) -> dict:
        pid = find_kakaotalk_pid()
        acc = self._acc()
        try:
            self.ensure_loaded()
        except Exception:
            pass
        with self._lock:
            total = len(self._rooms)
            unlocked = sum(1 for r in self._rooms if r["unlocked"])
            friends = sum(1 for u in self._users.values() if not u["hidden"] and u["link_id"] == "0")
        return {
            "kakaotalk_running": pid is not None,
            "pid": pid,
            "account_found": acc is not None,
            "rooms_total": total,
            "rooms_unlocked": unlocked,
            "friends_count": friends,
            "my_id": self._my_id,
        }

    def list_rooms(self, tab: str = "all", q: str = "") -> list[dict]:
        self.ensure_loaded()
        needle = q.strip().lower()
        with self._lock:
            items = list(self._rooms)
        out = []
        for r in items:
            t = r["type"]
            if tab == "chats" and t in ("OM", "OD", "PlusChat"):
                continue
            elif tab == "open" and t not in ("OM", "OD"):
                continue
            elif tab == "channels" and t != "PlusChat":
                continue
            if needle:
                if needle not in r["name"].lower() and needle not in r["last_message"].lower():
                    continue
            out.append(r)
        return out

    def get_room(self, chat_id: str) -> dict | None:
        if not _CHAT_ID_RE.match(chat_id):
            return None
        self.ensure_loaded()
        with self._lock:
            return dict(self._rooms_by_id[chat_id]) if chat_id in self._rooms_by_id else None

    def list_friends(self, q: str = "", limit: int = 250) -> list[dict]:
        self.ensure_loaded()
        needle = q.strip().lower()
        with self._lock:
            all_u = [u for u in self._users.values() if u["link_id"] == "0" and not u["hidden"]]
        if needle:
            all_u = [
                u for u in all_u
                if needle in u["name"].lower() or needle in u["nick"].lower() or needle in u["status"].lower()
            ]
        all_u.sort(key=lambda u: (not u["favorite"], u["name"].lower()))
        return all_u[: max(1, min(limit, 1000))]

    def _load_room_messages(self, chat_id: str) -> list[dict] | None:
        if not _CHAT_ID_RE.match(chat_id):
            return None
        kid = f"chatLogs_{chat_id}.edb"
        p = self._db_path(kid)
        if not p or not os.path.exists(p):
            return None
        key = self.keys.get(kid)
        if not key:
            return None
        sig = file_signature(p)
        with self._lock:
            cached = self._chat_cache.get(chat_id)
            if cached and cached[0] == sig:
                self._chat_cache.move_to_end(chat_id)
                return cached[1]

        room = self.get_room(chat_id) or {"link_id": "0"}
        link_id = room.get("link_id", "0")
        with self._lock:
            wms = list(self._room_watermarks.get(chat_id, []))
            my_id = self._my_id
            names_for_room = {uid: u["name"] for uid, u in self._users.items()}
            for (uid, lid), u in self._open_users.items():
                if lid == link_id:
                    names_for_room[uid] = u["name"]

        conn = open_decrypted_connection(p, key)
        try:
            rows = list(conn.execute(
                "SELECT logId, authorId, type, sendAt, message, attachement, "
                "COALESCE(deleted, 0) AS deleted, COALESCE(threadId, 0) AS threadId "
                "FROM chatLogs ORDER BY sendAt ASC, logId ASC"
            ))
        finally:
            conn.close()

        by_log_id: dict[int, dict] = {}
        parsed: list[dict] = []

        for r in rows:
            lid = _safe_int(r["logId"])
            typ = _safe_int(r["type"], 1)
            if typ == 0 or typ >= 16384:
                continue
            uid = str(r["authorId"])
            mine = bool(my_id and uid == my_id)
            u = self._resolve_user(uid, link_id)
            sender = "나" if mine else (u["name"] if u else f"사용자 {uid[-4:]}")
            avatar = "" if mine else (u["avatar"] if u else "")
            deleted = bool(_safe_int(r["deleted"]))
            raw_msg = r["message"] or ""
            raw_att = r["attachement"] or ""
            att = _parse_json_obj(raw_att)

            item: dict = {
                "log_id": lid,
                "at": _safe_int(r["sendAt"]),
                "author_id": uid,
                "sender": sender,
                "avatar": avatar,
                "mine": mine,
                "type": typ,
                "deleted": deleted,
                "text": "",
                "kind": "text",
                "media": [],
                "file": None,
                "reply_to": None,
                "card": None,
                "unread": sum(1 for m_uid, wm in wms if m_uid != uid and wm < lid),
            }

            if deleted:
                item["kind"] = "deleted"
                item["text"] = "삭제된 메시지입니다."
            elif typ == 24:
                item["kind"] = "system"
                item["text"] = _format_feed_text(raw_msg, names_for_room)
            elif typ == 2 and isinstance(att, dict):
                item["kind"] = "image"
                full_u = proxy_url(att.get("url"))
                thumb_u = proxy_url(att.get("thumbnailUrl")) or full_u
                if full_u or thumb_u:
                    item["media"].append({
                        "url": full_u or thumb_u,
                        "thumb": thumb_u or full_u,
                        "w": _safe_int(att.get("w")),
                        "h": _safe_int(att.get("h")),
                    })
                item["text"] = raw_msg.strip() if raw_msg and raw_msg != "사진" else ""
            elif typ == 27 and isinstance(att, dict):
                item["kind"] = "album"
                urls = att.get("imageUrls") or []
                thumbs = att.get("thumbnailUrls") or []
                ws = att.get("wl") or []
                hs = att.get("hl") or []
                for idx in range(max(len(urls), len(thumbs))):
                    fu = proxy_url(urls[idx] if idx < len(urls) else "")
                    tu = proxy_url(thumbs[idx] if idx < len(thumbs) else "") or fu
                    if fu or tu:
                        item["media"].append({
                            "url": fu or tu,
                            "thumb": tu or fu,
                            "w": _safe_int(ws[idx]) if idx < len(ws) else 0,
                            "h": _safe_int(hs[idx]) if idx < len(hs) else 0,
                        })
                item["text"] = ""
            elif typ == 3 and isinstance(att, dict):
                item["kind"] = "video"
                tu = proxy_url(att.get("thumbnailUrl"))
                vu = proxy_url(att.get("url"))
                item["media"].append({"url": vu, "thumb": tu, "w": _safe_int(att.get("w")), "h": _safe_int(att.get("h"))})
                item["text"] = "[동영상]"
            elif typ == 18 and isinstance(att, dict):
                item["kind"] = "file"
                item["file"] = {
                    "name": str(att.get("name") or "첨부파일"),
                    "size": _safe_int(att.get("size")),
                    "url": proxy_url(att.get("url")),
                }
                item["text"] = item["file"]["name"]
            elif typ == 26:
                item["kind"] = "reply"
                item["text"] = raw_msg
                if isinstance(att, dict):
                    src_id = _safe_int(att.get("src_logId"))
                    src_msg = str(att.get("src_message") or "")
                    src_uid = str(att.get("src_userId") or "")
                    src_u = self._resolve_user(src_uid, link_id)
                    src_sender = "나" if (my_id and src_uid == my_id) else (src_u["name"] if src_u else "상대방")
                    if not src_msg and src_id in by_log_id:
                        src_msg = by_log_id[src_id]["text"]
                    item["reply_to"] = {
                        "log_id": src_id,
                        "sender": src_sender,
                        "text": src_msg or format_last_preview(_safe_int(att.get("src_type"), 1), ""),
                    }
            elif typ in (12, 20, 25):
                item["kind"] = "emoticon"
                item["text"] = raw_msg.strip() or "[이모티콘]"
                if isinstance(att, dict):
                    raw_path = att.get("path") or att.get("emoticonItemPath")
                    play_res = emoticon_play_filename_from_path(raw_path)
                    thumb_res = emoticon_png_filename_from_path(raw_path, prefer_static_full=False)
                    if play_res or thumb_res:
                        play_u = f"/api/emoticon-asset/{play_res[0]}/{play_res[1]}" if play_res else ""
                        thumb_u = f"/api/emoticon-asset/{thumb_res[0]}/{thumb_res[1]}" if thumb_res else ""
                        item["media"].append({"url": play_u or thumb_u, "thumb": thumb_u or play_u, "w": 140, "h": 140})
                    else:
                        tu = proxy_url(att.get("thumbnailUrl") or att.get("url"))
                        if tu:
                            item["media"].append({"url": tu, "thumb": tu, "w": 120, "h": 120})
            elif typ == 71:
                item["kind"] = "card"
                summary, card = _extract_rich_card(raw_msg, raw_att)
                item["text"] = summary
                item["card"] = card
            else:
                item["text"] = raw_msg or format_last_preview(typ, raw_msg, raw_att)
                if isinstance(att, dict) and isinstance(att.get("universalScrapData"), dict):
                    sd = att["universalScrapData"]
                    if sd.get("title") or sd.get("url"):
                        item["card"] = {
                            "title": str(sd.get("title") or sd.get("url") or ""),
                            "description": str(sd.get("description") or ""),
                            "url": str(sd.get("url") or ""),
                        }

            by_log_id[lid] = item
            parsed.append(item)

        with self._lock:
            self._chat_cache[chat_id] = (sig, parsed)
            self._chat_cache.move_to_end(chat_id)
            while len(self._chat_cache) > MAX_CACHED_ROOMS:
                self._chat_cache.popitem(last=False)
        return parsed

    def get_messages(self, chat_id: str, limit: int = 80, before_log_id: int = 0, q: str = "") -> dict:
        room = self.get_room(chat_id)
        if room is None:
            return {"found": False, "unlocked": False, "messages": [], "has_more": False}
        msgs = self._load_room_messages(chat_id)
        if msgs is None:
            return {"found": True, "room": room, "unlocked": False, "messages": [], "has_more": False}

        needle = q.strip().lower()
        filtered = msgs
        if needle:
            filtered = [m for m in filtered if needle in m["text"].lower() or needle in m["sender"].lower()]
        if before_log_id > 0:
            filtered = [m for m in filtered if m["log_id"] < before_log_id]

        lim = max(1, min(limit, 300))
        has_more = len(filtered) > lim
        sliced = filtered[-lim:]
        return {
            "found": True,
            "room": room,
            "unlocked": True,
            "messages": sliced,
            "has_more": has_more,
            "total": len(msgs),
        }

    def resolve_emoticon_asset_path(self, item_code: str, filename: str) -> Path | None:
        if not _EMOT_CODE_RE.match(item_code or "") or not _EMOT_ASSET_RE.match(filename or ""):
            return None
        if ".." in filename:
            return None
        acc = self._acc()
        if not acc:
            return None
        di_base = (Path(acc) / "DigitalItem").resolve()
        if not di_base.is_dir():
            return None
        di_root = (di_base / "x2.0").resolve()
        if filename.lower().endswith(".webp"):
            candidates: tuple[Path, ...] = (di_base / "e" / filename,)
        else:
            candidates = (
                di_base / "e" / filename,
                di_root / item_code / filename,
                di_root / item_code / "thum_pack" / filename,
                di_root / "t" / filename,
            )
        for cand_raw in candidates:
            try:
                cand = cand_raw.resolve()
                if cand.is_relative_to(di_base) and cand.is_file():
                    return cand
            except OSError:
                continue
        return None

    def get_emoticons(self) -> dict:
        p = self._db_path("emoticon.edb")
        if not p or not os.path.exists(p):
            return {"available": False, "recent": [], "packs": []}

        sig = file_signature(p)
        with self._lock:
            if sig == self._emoticon_sig and self._emoticon_cache is not None:
                return self._emoticon_cache

        self.keys.invalidate_if_stale("emoticon.edb", p)
        key = self.keys.get("emoticon.edb")
        if not key:
            self.keys.scan_memory({"emoticon.edb": p}, include_hex_fallback=False, fast_only=True)
            key = self.keys.get("emoticon.edb")
        if not key:
            return {"available": False, "recent": [], "packs": []}

        conn = open_decrypted_connection(p, key)
        try:
            kid_map: dict[int, list[str]] = {}
            try:
                for row in conn.execute("SELECT * FROM SuggestionDictionary"):
                    kid = _safe_int(row[0], -1)
                    if kid < 0:
                        continue
                    words: list[str] = []
                    main_kw = str(row[1] or "").strip() if len(row) > 1 else ""
                    if main_kw:
                        words.append(main_kw)
                    if len(row) > 2 and row[2]:
                        for syn in str(row[2]).split(","):
                            s = syn.strip()
                            if s and s not in words:
                                words.append(s)
                            if len(words) >= 9:
                                break
                    if words:
                        kid_map[kid] = words
            except sqlite3.Error:
                pass

            user_data: dict[str, str] = {}
            try:
                for row in conn.execute("SELECT * FROM UserData"):
                    if len(row) >= 2 and row[0]:
                        user_data[str(row[0])] = str(row[1] or "")
            except sqlite3.Error:
                pass

            sticker_order = [x.strip() for x in user_data.get("KeyboardOrder", "").split(",") if x.strip()]
            emoji_order = [x.strip() for x in user_data.get("EmojiKeyboardOrder", "").split(",") if x.strip()]

            packs_by_code: dict[str, dict] = {}
            pack_owned: dict[str, int] = {}
            item_lookup_by_thumb: dict[tuple[str, str], dict] = {}
            item_lookup_by_idx: dict[tuple[str, int], dict] = {}

            sql_packs = (
                "SELECT itemCode, itemType, itemSubType, title, iconOnImagePath, emotsJson, kids, ownedItem "
                "FROM KeyboardItems"
            )
            for row in conn.execute(sql_packs):
                code = str(row["itemCode"] or "").strip()
                if not _EMOT_CODE_RE.match(code):
                    continue
                item_type = str(row["itemType"] or "STICKER").strip()
                title = str(row["title"] or code).strip()
                owned = _safe_int(row["ownedItem"])
                pack_owned[code] = owned

                raw_emots = _parse_json_obj(row["emotsJson"])
                raw_kids = _parse_json_obj(row["kids"])
                emots_list = raw_emots if isinstance(raw_emots, list) else []
                kids_list = raw_kids if isinstance(raw_kids, list) else []

                items: list[dict] = []
                for idx_pos, e in enumerate(emots_list):
                    if not isinstance(e, dict):
                        continue
                    thumb_res = emoticon_png_filename_from_path(e.get("path"), prefer_static_full=False)
                    send_res = emoticon_png_filename_from_path(
                        e.get("playPath") or e.get("path"), prefer_static_full=True
                    ) or thumb_res
                    play_res = emoticon_play_filename_from_path(e.get("playPath") or e.get("path"))
                    if not thumb_res or not send_res:
                        continue

                    label = ""
                    text_obj = _parse_json_obj(e.get("text"))
                    if isinstance(text_obj, dict):
                        ko_val = str(text_obj.get("ko") or "").strip()
                        if ko_val and ko_val != "(이모티콘)":
                            label = ko_val

                    kws: list[str] = []
                    if idx_pos < len(kids_list) and isinstance(kids_list[idx_pos], list):
                        for kid_raw in kids_list[idx_pos]:
                            for kw in kid_map.get(_safe_int(kid_raw, -1), ()):
                                if kw not in kws:
                                    kws.append(kw)
                                if len(kws) >= 12:
                                    break
                            if len(kws) >= 12:
                                break
                    clean_label = label.strip("()")
                    if clean_label and clean_label not in kws:
                        kws.insert(0, clean_label)

                    emot_idx = _safe_int(e.get("emotIdx"), idx_pos + 1)
                    pack_kind = "emoji" if item_type.startswith("EMOJI") else "sticker"
                    item_obj = {
                        "item_code": code,
                        "idx": emot_idx,
                        "grid_idx": idx_pos,
                        "pack_pos": -1,
                        "kind": pack_kind,
                        "thumb_file": thumb_res[1],
                        "send_file": send_res[1],
                        "thumb_url": f"/api/emoticon-asset/{code}/{thumb_res[1]}",
                        "play_url": f"/api/emoticon-asset/{code}/{play_res[1]}" if play_res else f"/api/emoticon-asset/{code}/{thumb_res[1]}",
                        "label": label,
                        "keywords": kws[:12],
                    }
                    items.append(item_obj)
                    item_lookup_by_thumb[(code, thumb_res[1])] = item_obj
                    item_lookup_by_idx.setdefault((code, emot_idx), item_obj)

                icon_file = str(row["iconOnImagePath"] or f"{code}.icon_on.png").strip()
                if not _EMOT_PNG_RE.match(icon_file) or ".." in icon_file:
                    icon_file = f"{code}.icon_on.png"

                packs_by_code[code] = {
                    "code": code,
                    "title": title,
                    "type": item_type,
                    "kind": "emoji" if item_type.startswith("EMOJI") else "sticker",
                    "pack_pos": -1,
                    "icon_url": f"/api/emoticon-asset/{code}/{icon_file}",
                    "items": items,
                }

            ordered_packs: list[dict] = []
            seen_codes: set[str] = set()
            for code in sticker_order + emoji_order:
                if code in packs_by_code and code not in seen_codes:
                    ordered_packs.append(packs_by_code[code])
                    seen_codes.add(code)
            for code, pack in packs_by_code.items():
                if code not in seen_codes and pack_owned.get(code, 0) == 1:
                    ordered_packs.append(pack)
                    seen_codes.add(code)

            pos_by_kind = {"sticker": 0, "emoji": 0}
            for pack in ordered_packs:
                pk = pack["kind"]
                pos = pos_by_kind.get(pk, 0)
                pos_by_kind[pk] = pos + 1
                pack["pack_pos"] = pos
                for it in pack["items"]:
                    it["pack_pos"] = pos

            recent: list[dict] = []
            seen_recent: set[tuple[str, str]] = set()
            try:
                sql_recent = (
                    "SELECT itemCode, itemIndex, name, thumbnail, updatedAt "
                    "FROM RecentlyUsedItems ORDER BY updatedAt DESC LIMIT 36"
                )
                for row in conn.execute(sql_recent):
                    code = str(row["itemCode"] or "").strip()
                    if not _EMOT_CODE_RE.match(code):
                        continue
                    parsed = emoticon_png_filename_from_path(row["thumbnail"] or row["name"], prefer_static_full=False)
                    matched: dict | None = None
                    if parsed and (parsed[0], parsed[1]) in item_lookup_by_thumb:
                        matched = item_lookup_by_thumb[(parsed[0], parsed[1])]
                    elif (code, _safe_int(row["itemIndex"]) + 1) in item_lookup_by_idx:
                        matched = item_lookup_by_idx[(code, _safe_int(row["itemIndex"]) + 1)]
                    elif (code, _safe_int(row["itemIndex"])) in item_lookup_by_idx:
                        matched = item_lookup_by_idx[(code, _safe_int(row["itemIndex"]))]

                    if matched is not None:
                        rec_item = dict(matched)
                    elif parsed is not None:
                        send_parsed = emoticon_png_filename_from_path(row["name"] or row["thumbnail"], prefer_static_full=True) or parsed
                        rec_item = {
                            "item_code": parsed[0],
                            "idx": _safe_int(row["itemIndex"], 0),
                            "thumb_file": parsed[1],
                            "send_file": send_parsed[1],
                            "thumb_url": f"/api/emoticon-asset/{parsed[0]}/{parsed[1]}",
                            "label": "",
                            "keywords": [],
                        }
                    else:
                        continue

                    dedup_key = (rec_item["item_code"], rec_item["thumb_file"])
                    if dedup_key in seen_recent:
                        continue
                    seen_recent.add(dedup_key)
                    recent.append(rec_item)
            except sqlite3.Error:
                pass

            result = {"available": True, "recent": recent, "packs": ordered_packs}
            with self._lock:
                self._emoticon_sig = sig
                self._emoticon_cache = result
            return result
        finally:
            conn.close()

    def resolve_emoticon_target(self, item_code: str, filename: str) -> dict | None:
        data = self.get_emoticons()
        for pack in data.get("packs") or []:
            if pack.get("code") != item_code:
                continue
            for it in pack.get("items") or []:
                if it.get("thumb_file") == filename or it.get("send_file") == filename:
                    return {
                        "kind": it.get("kind") or pack.get("kind") or "sticker",
                        "pack_pos": _safe_int(it.get("pack_pos"), _safe_int(pack.get("pack_pos"), -1)),
                        "grid_idx": _safe_int(it.get("grid_idx"), -1),
                    }
        return None

    def _watch_loop(self) -> None:
        last_list_sig: tuple | None = None
        last_chat_sigs: dict[str, tuple] = {}
        while not self._stop_watch.is_set():
            try:
                acc = self._acc()
                if acc:
                    list_path = os.path.join(acc, "chat_data", "chatListInfo.edb")
                    lsig = file_signature(list_path)
                    if last_list_sig is not None and lsig != last_list_sig:
                        now = time.monotonic()
                        if now - self._last_rooms_reload >= 0.8:
                            self._last_rooms_reload = now
                            self._load_rooms_if_changed()
                            self._broadcast("rooms_updated", {"reason": "db_change"})
                    last_list_sig = lsig

                    chat_dir = os.path.join(acc, "chat_data")
                    # Watch cached rooms + any rooms with unlocked keys
                    with self._lock:
                        watch_ids = list(self._chat_cache.keys())
                    for cid in watch_ids:
                        p = os.path.join(chat_dir, f"chatLogs_{cid}.edb")
                        sig = file_signature(p)
                        prev = last_chat_sigs.get(cid)
                        if prev is not None and sig != prev:
                            self._broadcast("room_messages_updated", {"chat_id": cid})
                        last_chat_sigs[cid] = sig
            except Exception:
                pass
            self._stop_watch.wait(0.5)
