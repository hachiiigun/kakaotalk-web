#define _WIN32_WINNT 0x0601
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shlobj.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#define BRIDGE_PORT 14502
#define MAX_REQ_BYTES (1024 * 1024)
#define MAX_WINS 64
#define MAX_CHILDREN 512
#define MAX_EDITS 32

#ifndef SW_SHOWMINNOACTIVE
#define SW_SHOWMINNOACTIVE 7
#endif
#ifndef SMTO_ABORTIFHUNG
#define SMTO_ABORTIFHUNG 0x0002
#endif
#ifndef PROCESS_QUERY_LIMITED_INFORMATION
#define PROCESS_QUERY_LIMITED_INFORMATION 0x1000
#endif

#define ENTER_DOWN_LPARAM 0x001C0001L
#define ENTER_UP_LPARAM   ((LPARAM)0xC01C0001L)

typedef UINT (WINAPI *PFN_GetDpiForWindow)(HWND);
static PFN_GetDpiForWindow g_GetDpiForWindow = NULL;
static CRITICAL_SECTION g_action_lock;
static DWORD g_kakao_pid = 0;

/* ---------- String / Encoding Helpers ---------- */

static wchar_t *utf8_to_wide(const char *s) {
    if (!s) return _wcsdup(L"");
    int need = MultiByteToWideChar(CP_UTF8, 0, s, -1, NULL, 0);
    if (need <= 0) return _wcsdup(L"");
    wchar_t *w = (wchar_t *)calloc((size_t)need, sizeof(wchar_t));
    if (!w) return NULL;
    MultiByteToWideChar(CP_UTF8, 0, s, -1, w, need);
    return w;
}

static char *wide_to_utf8(const wchar_t *w) {
    if (!w) return _strdup("");
    int need = WideCharToMultiByte(CP_UTF8, 0, w, -1, NULL, 0, NULL, NULL);
    if (need <= 0) return _strdup("");
    char *s = (char *)calloc((size_t)need, 1);
    if (!s) return NULL;
    WideCharToMultiByte(CP_UTF8, 0, w, -1, s, need, NULL, NULL);
    return s;
}

static void trim_wide(wchar_t *s) {
    if (!s) return;
    size_t len = wcslen(s);
    while (len > 0 && (s[len - 1] == L' ' || s[len - 1] == L'\t' || s[len - 1] == L'\r' || s[len - 1] == L'\n')) {
        s[--len] = L'\0';
    }
    size_t start = 0;
    while (s[start] == L' ' || s[start] == L'\t' || s[start] == L'\r' || s[start] == L'\n') start++;
    if (start > 0) memmove(s, s + start, (len - start + 1) * sizeof(wchar_t));
}

static wchar_t *normalize_crlf_wide(const wchar_t *src) {
    size_t len = wcslen(src);
    wchar_t *out = (wchar_t *)calloc(len * 2 + 1, sizeof(wchar_t));
    if (!out) return NULL;
    size_t j = 0;
    for (size_t i = 0; i < len; i++) {
        if (src[i] == L'\r') {
            out[j++] = L'\r';
            if (i + 1 < len && src[i + 1] == L'\n') i++;
            out[j++] = L'\n';
        } else if (src[i] == L'\n') {
            out[j++] = L'\r';
            out[j++] = L'\n';
        } else {
            out[j++] = src[i];
        }
    }
    out[j] = L'\0';
    return out;
}

static int same_normalized_wide(const wchar_t *a, const wchar_t *b) {
    wchar_t *na = normalize_crlf_wide(a ? a : L"");
    wchar_t *nb = normalize_crlf_wide(b ? b : L"");
    int eq = (na && nb && wcscmp(na, nb) == 0);
    free(na);
    free(nb);
    return eq;
}

static int is_empty_input_text(const wchar_t *t) {
    if (!t) return 1;
    wchar_t *cp = _wcsdup(t);
    if (!cp) return 1;
    trim_wide(cp);
    int empty = (cp[0] == L'\0'
        || wcscmp(cp, L"Enter a message") == 0
        || wcscmp(cp, L"\uBA54\uC2DC\uC9C0 \uC785\uB825") == 0
        || wcscmp(cp, L"\uBA54\uC2DC\uC9C0\uB97C \uC785\uB825\uD558\uC138\uC694") == 0);
    free(cp);
    return empty;
}

/* ---------- Dynamic String Buffer & JSON Escaping ---------- */

typedef struct {
    char *buf;
    size_t len;
    size_t cap;
} StrBuf;

static void sb_append_n(StrBuf *sb, const char *s, size_t n) {
    if (sb->len + n + 1 > sb->cap) {
        size_t ncap = sb->cap ? sb->cap * 2 : 4096;
        while (ncap < sb->len + n + 1) ncap *= 2;
        char *nb = (char *)realloc(sb->buf, ncap);
        if (!nb) return;
        sb->buf = nb;
        sb->cap = ncap;
    }
    memcpy(sb->buf + sb->len, s, n);
    sb->len += n;
    sb->buf[sb->len] = '\0';
}

static void sb_append(StrBuf *sb, const char *s) {
    if (s) sb_append_n(sb, s, strlen(s));
}

static void sb_printf(StrBuf *sb, const char *fmt, ...) {
    char tmp[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
    va_end(ap);
    if (n > 0) sb_append_n(sb, tmp, (size_t)(n < (int)sizeof(tmp) ? n : (int)sizeof(tmp) - 1));
}

static void sb_append_json_str(StrBuf *sb, const char *s) {
    sb_append_n(sb, "\"", 1);
    if (s) {
        const unsigned char *p = (const unsigned char *)s;
        while (*p) {
            unsigned char c = *p++;
            if (c == '"') sb_append(sb, "\\\"");
            else if (c == '\\') sb_append(sb, "\\\\");
            else if (c == '\b') sb_append(sb, "\\b");
            else if (c == '\f') sb_append(sb, "\\f");
            else if (c == '\n') sb_append(sb, "\\n");
            else if (c == '\r') sb_append(sb, "\\r");
            else if (c == '\t') sb_append(sb, "\\t");
            else if (c < 0x20) sb_printf(sb, "\\u%04x", c);
            else sb_append_n(sb, (const char *)&c, 1);
        }
    }
    sb_append_n(sb, "\"", 1);
}

static void sb_append_json_wide(StrBuf *sb, const wchar_t *w) {
    char *u = wide_to_utf8(w);
    sb_append_json_str(sb, u);
    free(u);
}

static char *json_extract_string(const char *json, const char *key) {
    if (!json || !key) return NULL;
    size_t klen = strlen(key);
    const char *p = json;
    while ((p = strchr(p, '"')) != NULL) {
        p++;
        if (strncmp(p, key, klen) == 0 && p[klen] == '"') {
            const char *q = p + klen + 1;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (*q != ':') { p++; continue; }
            q++;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (*q != '"') return NULL;
            q++;
            StrBuf out = {0};
            while (*q && *q != '"') {
                if (*q == '\\' && q[1]) {
                    q++;
                    if (*q == '"' || *q == '\\' || *q == '/') sb_append_n(&out, q, 1);
                    else if (*q == 'b') sb_append_n(&out, "\b", 1);
                    else if (*q == 'f') sb_append_n(&out, "\f", 1);
                    else if (*q == 'n') sb_append_n(&out, "\n", 1);
                    else if (*q == 'r') sb_append_n(&out, "\r", 1);
                    else if (*q == 't') sb_append_n(&out, "\t", 1);
                    else if (*q == 'u' && isxdigit((unsigned char)q[1]) && isxdigit((unsigned char)q[2])
                             && isxdigit((unsigned char)q[3]) && isxdigit((unsigned char)q[4])) {
                        unsigned int cp = 0;
                        sscanf(q + 1, "%4x", &cp);
                        q += 4;
                        if (cp >= 0xD800 && cp <= 0xDBFF && q[1] == '\\' && q[2] == 'u'
                            && isxdigit((unsigned char)q[3]) && isxdigit((unsigned char)q[4])
                            && isxdigit((unsigned char)q[5]) && isxdigit((unsigned char)q[6])) {
                            unsigned int low = 0;
                            sscanf(q + 3, "%4x", &low);
                            if (low >= 0xDC00 && low <= 0xDFFF) {
                                cp = 0x10000 + (((cp - 0xD800) << 10) | (low - 0xDC00));
                                q += 6;
                            }
                        }
                        char utf8[4];
                        if (cp < 0x80) {
                            utf8[0] = (char)cp;
                            sb_append_n(&out, utf8, 1);
                        } else if (cp < 0x800) {
                            utf8[0] = (char)(0xC0 | (cp >> 6));
                            utf8[1] = (char)(0x80 | (cp & 0x3F));
                            sb_append_n(&out, utf8, 2);
                        } else if (cp < 0x10000) {
                            utf8[0] = (char)(0xE0 | (cp >> 12));
                            utf8[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
                            utf8[2] = (char)(0x80 | (cp & 0x3F));
                            sb_append_n(&out, utf8, 3);
                        } else {
                            utf8[0] = (char)(0xF0 | (cp >> 18));
                            utf8[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
                            utf8[2] = (char)(0x80 | ((cp >> 6) & 0x3F));
                            utf8[3] = (char)(0x80 | (cp & 0x3F));
                            sb_append_n(&out, utf8, 4);
                        }
                    }
                    q++;
                } else {
                    sb_append_n(&out, q, 1);
                    q++;
                }
            }
            return out.buf ? out.buf : _strdup("");
        }
        p++;
    }
    return NULL;
}

static int json_extract_bool(const char *json, const char *key) {
    if (!json || !key) return 0;
    size_t klen = strlen(key);
    const char *p = json;
    while ((p = strchr(p, '"')) != NULL) {
        p++;
        if (strncmp(p, key, klen) == 0 && p[klen] == '"') {
            const char *q = p + klen + 1;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (*q != ':') { p++; continue; }
            q++;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (strncmp(q, "true", 4) == 0) {
                char c = q[4];
                if (c == ',' || c == '}' || c == ' ' || c == '\t' || c == '\r' || c == '\n' || c == '\0') return 1;
            }
            return 0;
        }
        p++;
    }
    return 0;
}

static int json_extract_int(const char *json, const char *key, int def_val) {
    if (!json || !key) return def_val;
    size_t klen = strlen(key);
    const char *p = json;
    while ((p = strchr(p, '"')) != NULL) {
        p++;
        if (strncmp(p, key, klen) == 0 && p[klen] == '"') {
            const char *q = p + klen + 1;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            if (*q != ':') { p++; continue; }
            q++;
            while (*q == ' ' || *q == '\t' || *q == '\r' || *q == '\n') q++;
            char *endp = NULL;
            long val = strtol(q, &endp, 10);
            if (endp == q) return def_val;
            while (*endp == ' ' || *endp == '\t' || *endp == '\r' || *endp == '\n') endp++;
            if (*endp != ',' && *endp != '}' && *endp != '\0') return def_val;
            return (int)val;
        }
        p++;
    }
    return def_val;
}

/* ---------- KakaoTalk Window Inspection & Messaging ---------- */

static int smto_w_ms(HWND h, UINT msg, WPARAM wp, LPARAM lp, ULONG_PTR *out_res, UINT timeout_ms) {
    ULONG_PTR res = 0;
    LRESULT ok = SendMessageTimeoutW(h, msg, wp, lp, SMTO_ABORTIFHUNG, timeout_ms, &res);
    if (out_res) *out_res = res;
    return ok != 0;
}

static void get_cls(HWND h, wchar_t *buf, int cap) {
    buf[0] = L'\0';
    GetClassNameW(h, buf, cap);
}

/* Always use SendMessageTimeoutW instead of raw GetWindowTextW across processes so a hung window never stalls us. */
static void get_title(HWND h, wchar_t *buf, int cap) {
    buf[0] = L'\0';
    smto_w_ms(h, WM_GETTEXT, (WPARAM)cap, (LPARAM)buf, NULL, 1500);
}

static int is_kakaotalk_pid(DWORD pid) {
    if (!pid) return 0;
    if (g_kakao_pid && pid == g_kakao_pid) return 1;
    HANDLE hProc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!hProc) return 0;
    wchar_t path[MAX_PATH * 2];
    DWORD sz = MAX_PATH * 2;
    int match = 0;
    if (QueryFullProcessImageNameW(hProc, 0, path, &sz)) {
        const wchar_t *base = wcsrchr(path, L'\\');
        base = base ? base + 1 : path;
        if (_wcsicmp(base, L"KakaoTalk.exe") == 0) {
            g_kakao_pid = pid;
            match = 1;
        }
    }
    CloseHandle(hProc);
    return match;
}

static int is_kakaotalk_hwnd(HWND h) {
    DWORD pid = 0;
    GetWindowThreadProcessId(h, &pid);
    return is_kakaotalk_pid(pid);
}

static int is_main_title(const wchar_t *t) {
    return (wcscmp(t, L"\uCE74\uCE74\uC624\uD1A1") == 0 || wcscmp(t, L"KakaoTalk") == 0);
}

static int is_chat_win_class(const wchar_t *cls) {
    return (_wcsicmp(cls, L"EVA_Window_Dblclk") == 0
        || _wcsicmp(cls, L"EVA_Window") == 0
        || _wcsicmp(cls, L"#32770") == 0);
}

static int is_input_class(const wchar_t *cls) {
    return (_wcsicmp(cls, L"RICHEDIT50W") == 0
        || _wcsicmp(cls, L"RichEdit20W") == 0
        || _wcsicmp(cls, L"RICHEDIT60W") == 0
        || _wcsicmp(cls, L"RICHEDIT50A") == 0);
}

typedef struct {
    HWND items[MAX_WINS];
    int count;
} HwndList;

typedef struct {
    HWND items[MAX_CHILDREN];
    int count;
} ChildHwndList;

static BOOL CALLBACK enum_top_kakao_cb(HWND h, LPARAM lp) {
    HwndList *lst = (HwndList *)lp;
    if (lst->count < MAX_WINS && is_kakaotalk_hwnd(h)) {
        lst->items[lst->count++] = h;
    }
    return TRUE;
}

static BOOL CALLBACK enum_child_all_cb(HWND h, LPARAM lp) {
    HwndList *lst = (HwndList *)lp;
    if (lst->count < MAX_WINS) lst->items[lst->count++] = h;
    return TRUE;
}

static BOOL CALLBACK enum_child_many_cb(HWND h, LPARAM lp) {
    ChildHwndList *lst = (ChildHwndList *)lp;
    if (lst->count < MAX_CHILDREN) lst->items[lst->count++] = h;
    return TRUE;
}

static void collect_input_controls(HWND chat_hwnd, HwndList *out) {
    ChildHwndList all = {0};
    out->count = 0;
    EnumChildWindows(chat_hwnd, enum_child_many_cb, (LPARAM)&all);
    for (int i = 0; i < all.count; i++) {
        wchar_t cls[128];
        get_cls(all.items[i], cls, 128);
        if (is_input_class(cls) && IsWindowVisible(all.items[i])) {
            if (out->count < MAX_WINS) out->items[out->count++] = all.items[i];
        }
    }
}

typedef struct {
    HWND hwnd;
    HWND input_hwnd;
    int input_count;
    int minimized;
    wchar_t title[512];
} ChatWinInfo;

static HWND find_main_window(void) {
    HwndList tops = {0};
    EnumWindows(enum_top_kakao_cb, (LPARAM)&tops);
    for (int i = 0; i < tops.count; i++) {
        wchar_t cls[128], title[512];
        get_cls(tops.items[i], cls, 128);
        if (wcscmp(cls, L"EVA_Window_Dblclk") != 0) continue;
        get_title(tops.items[i], title, 512);
        if (is_main_title(title)) {
            return tops.items[i];
        }
    }
    return NULL;
}

static int list_chat_windows(ChatWinInfo *out, int max_out) {
    HwndList tops = {0};
    EnumWindows(enum_top_kakao_cb, (LPARAM)&tops);
    int n = 0;
    for (int i = 0; i < tops.count && n < max_out; i++) {
        HWND h = tops.items[i];
        wchar_t cls[128], title[512];
        if (!IsWindowVisible(h)) continue;
        get_cls(h, cls, 128);
        if (!is_chat_win_class(cls)) continue;
        get_title(h, title, 512);
        trim_wide(title);
        if (!title[0] || is_main_title(title)) continue;
        HwndList inputs = {0};
        collect_input_controls(h, &inputs);
        if (inputs.count > 0) {
            out[n].hwnd = h;
            out[n].input_hwnd = inputs.items[0];
            out[n].input_count = inputs.count;
            out[n].minimized = IsIconic(h) ? 1 : 0;
            wcsncpy(out[n].title, title, 511);
            out[n].title[511] = L'\0';
            n++;
        }
    }
    return n;
}

static int title_matches_fuzzy(const wchar_t *win_title, const wchar_t *query) {
    if (!win_title || !win_title[0] || !query || !query[0]) return 0;
    wchar_t needle[512];
    wcsncpy(needle, query, 511);
    needle[511] = L'\0';
    trim_wide(needle);
    wchar_t *suffix = wcsstr(needle, L" \uC678 ");
    if (suffix && wcsstr(suffix, L"\uBA85") != NULL) {
        *suffix = L'\0';
        trim_wide(needle);
    }
    if (wcslen(needle) < 2 || wcslen(win_title) < 2) return 0;
    return (wcsstr(win_title, needle) != NULL || wcsstr(needle, win_title) != NULL);
}

static HWND find_chat_window_any(const wchar_t *room_w, const wchar_t *alt_w) {
    wchar_t want1[512] = {0}, want2[512] = {0};
    if (room_w) { wcsncpy(want1, room_w, 511); trim_wide(want1); }
    if (alt_w) { wcsncpy(want2, alt_w, 511); trim_wide(want2); }
    if (!want1[0] && !want2[0]) return NULL;

    ChatWinInfo wins[MAX_WINS];
    int n = list_chat_windows(wins, MAX_WINS);
    for (int i = 0; i < n; i++) {
        if ((want1[0] && wcscmp(wins[i].title, want1) == 0) ||
            (want2[0] && wcscmp(wins[i].title, want2) == 0)) {
            return wins[i].hwnd;
        }
    }

    HWND fuzzy_hit = NULL;
    int fuzzy_matches = 0;
    for (int i = 0; i < n; i++) {
        if (title_matches_fuzzy(wins[i].title, want1) ||
            title_matches_fuzzy(wins[i].title, want2)) {
            fuzzy_hit = wins[i].hwnd;
            fuzzy_matches++;
        }
    }
    return (fuzzy_matches == 1) ? fuzzy_hit : NULL;
}

static int wcsistr_ascii(const wchar_t *hay, const wchar_t *needle) {
    if (!hay || !needle || !needle[0]) return 0;
    size_t nlen = wcslen(needle);
    for (const wchar_t *p = hay; *p; p++) {
        if (_wcsnicmp(p, needle, nlen) == 0) return 1;
    }
    return 0;
}

static HWND find_chats_pane(HWND main_h) {
    HWND child = FindWindowExW(main_h, NULL, L"EVA_ChildWindow", NULL);
    if (!child) return NULL;
    HWND panes[16] = {0};
    int np = 0;
    HWND cur = NULL;
    while (np < 16) {
        cur = FindWindowExW(child, cur, L"EVA_Window", NULL);
        if (!cur) break;
        panes[np++] = cur;
        wchar_t t[256] = {0};
        get_title(cur, t, 256);
        if (wcsistr_ascii(t, L"ChatRoomListView")) {
            return cur;
        }
    }
    return (np >= 2) ? panes[1] : (np == 1 ? panes[0] : NULL);
}

static int collect_search_edits(HWND main_hwnd, HWND *edits, int max_edits, int visible_only) {
    int count = 0;
    HWND child = FindWindowExW(main_hwnd, NULL, L"EVA_ChildWindow", NULL);
    if (child) {
        HWND first = FindWindowExW(child, NULL, L"EVA_Window", NULL);
        HWND second = first ? FindWindowExW(child, first, L"EVA_Window", NULL) : NULL;
        if (second) {
            HWND ed = FindWindowExW(second, NULL, L"Edit", NULL);
            if (ed && (!visible_only || IsWindowVisible(ed)) && count < max_edits) edits[count++] = ed;
        }
        if (first) {
            HWND ed1 = FindWindowExW(first, NULL, L"Edit", NULL);
            if (ed1 && (!visible_only || IsWindowVisible(ed1)) && count < max_edits) {
                int dup = 0;
                for (int j = 0; j < count; j++) if (edits[j] == ed1) dup = 1;
                if (!dup) edits[count++] = ed1;
            }
        }
    }
    HwndList all = {0};
    EnumChildWindows(main_hwnd, enum_child_all_cb, (LPARAM)&all);
    for (int i = 0; i < all.count && count < max_edits; i++) {
        wchar_t cls[128], pcls[128];
        get_cls(all.items[i], cls, 128);
        if (wcscmp(cls, L"Edit") != 0) continue;
        HWND par = GetParent(all.items[i]);
        if (!par) continue;
        get_cls(par, pcls, 128);
        if (wcscmp(pcls, L"EVA_Window") != 0) continue;
        if (visible_only && !IsWindowVisible(all.items[i])) continue;
        int dup = 0;
        for (int j = 0; j < count; j++) if (edits[j] == all.items[i]) dup = 1;
        if (!dup) edits[count++] = all.items[i];
    }
    return count;
}

static void send_ctrl_key(WORD vk) {
    INPUT inp[4];
    ZeroMemory(inp, sizeof(inp));
    inp[0].type = INPUT_KEYBOARD; inp[0].ki.wVk = VK_CONTROL;
    inp[1].type = INPUT_KEYBOARD; inp[1].ki.wVk = vk;
    inp[2].type = INPUT_KEYBOARD; inp[2].ki.wVk = vk; inp[2].ki.dwFlags = KEYEVENTF_KEYUP;
    inp[3].type = INPUT_KEYBOARD; inp[3].ki.wVk = VK_CONTROL; inp[3].ki.dwFlags = KEYEVENTF_KEYUP;
    SendInput(4, inp, sizeof(INPUT));
}

static void post_dblclick_client(HWND hwnd, int x, int y) {
    LPARAM lp = MAKELPARAM(x, y);
    PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp);
    Sleep(20);
    PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp);
    Sleep(25);
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp);
    Sleep(35);
    PostMessageW(hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, lp);
    Sleep(25);
    PostMessageW(hwnd, WM_LBUTTONUP, 0, lp);
}

static int count_visible_kakao_tops(void) {
    HwndList tops = {0};
    EnumWindows(enum_top_kakao_cb, (LPARAM)&tops);
    int vis = 0;
    for (int i = 0; i < tops.count; i++) {
        if (IsWindowVisible(tops.items[i])) vis++;
    }
    return vis;
}

static int is_chat_hwnd_valid(HWND h) {
    if (!IsWindowVisible(h)) return 0;
    wchar_t cls[128] = {0}, title[512] = {0};
    get_cls(h, cls, 128);
    if (!is_chat_win_class(cls)) return 0;
    get_title(h, title, 512);
    trim_wide(title);
    if (!title[0] || is_main_title(title)) return 0;
    HwndList inputs = {0};
    collect_input_controls(h, &inputs);
    return inputs.count > 0;
}

static void collect_child_classes_summary(HWND h, StrBuf *csb, int *out_count) {
    ChildHwndList ch = {0};
    EnumChildWindows(h, enum_child_many_cb, (LPARAM)&ch);
    if (out_count) *out_count = ch.count;
    char uniq[10][64];
    int nu = 0;
    for (int i = 0; i < ch.count && nu < 10; i++) {
        wchar_t cw[64] = {0};
        get_cls(ch.items[i], cw, 64);
        char *cu = wide_to_utf8(cw);
        if (cu && cu[0]) {
            int seen = 0;
            for (int j = 0; j < nu; j++) {
                if (strcmp(uniq[j], cu) == 0) { seen = 1; break; }
            }
            if (!seen) {
                snprintf(uniq[nu], sizeof(uniq[nu]), "%s", cu);
                if (nu > 0) sb_append(csb, ",");
                sb_append(csb, uniq[nu]);
                nu++;
            }
        }
        free(cu);
    }
}

static void log_popup_diag(HWND h, long w, long ht) {
    wchar_t wcls[128] = {0}, wttl[512] = {0};
    get_cls(h, wcls, 128);
    get_title(h, wttl, 512);
    char *cls_u = wide_to_utf8(wcls);
    int title_len = (int)wcslen(wttl);
    int n_ch = 0;
    StrBuf csb = {0};
    collect_child_classes_summary(h, &csb, &n_ch);
    fprintf(stderr, "[popup] hwnd=%llu cls=%s size=%ldx%ld title_len=%d children=%d child_cls=%s\n",
            (unsigned long long)(ULONG_PTR)h,
            cls_u ? cls_u : "",
            w, ht, title_len, n_ch,
            csb.buf ? csb.buf : "");
    fflush(stderr);
    free(cls_u);
    free(csb.buf);
}

static void log_sendfile_dialog(const char *via, HWND dlg, int dry_run) {
    wchar_t wcls[128] = {0};
    get_cls(dlg, wcls, 128);
    char *cls_u = wide_to_utf8(wcls);
    RECT rc = {0};
    GetWindowRect(dlg, &rc);
    int n_ch = 0;
    StrBuf csb = {0};
    collect_child_classes_summary(dlg, &csb, &n_ch);
    fprintf(stderr, "[send-file] via=%s dlg=%llu cls=%s size=%ldx%ld children=%d child_cls=%s dry_run=%d\n",
            via ? via : "",
            (unsigned long long)(ULONG_PTR)dlg,
            cls_u ? cls_u : "",
            (long)(rc.right - rc.left),
            (long)(rc.bottom - rc.top),
            n_ch,
            csb.buf ? csb.buf : "",
            dry_run ? 1 : 0);
    fflush(stderr);
    free(cls_u);
    free(csb.buf);
}

static int close_new_nonchat_popups(HWND main_h, const HwndList *before_tops) {
    HwndList now = {0};
    EnumWindows(enum_top_kakao_cb, (LPARAM)&now);
    int closed = 0;
    for (int i = 0; i < now.count; i++) {
        HWND h = now.items[i];
        if (h == main_h || !IsWindowVisible(h)) continue;
        int existed = 0;
        if (before_tops) {
            for (int j = 0; j < before_tops->count; j++) {
                if (before_tops->items[j] == h) { existed = 1; break; }
            }
        }
        if (existed || is_chat_hwnd_valid(h)) continue;
        RECT rc = {0};
        GetWindowRect(h, &rc);
        long w = rc.right - rc.left;
        long ht = rc.bottom - rc.top;
        if (w >= 100 && ht >= 100) {
            log_popup_diag(h, w, ht);
            PostMessageW(h, WM_CLOSE, 0, 0);
            closed++;
        }
    }
    if (closed > 0) Sleep(140);
    return closed;
}

static void close_stray_profile_popups(HWND main_h) {
    HwndList now = {0};
    EnumWindows(enum_top_kakao_cb, (LPARAM)&now);
    int closed = 0;
    for (int i = 0; i < now.count; i++) {
        HWND h = now.items[i];
        if (h == main_h || !IsWindowVisible(h) || is_chat_hwnd_valid(h)) continue;
        RECT rc = {0};
        GetWindowRect(h, &rc);
        long w = rc.right - rc.left;
        long ht = rc.bottom - rc.top;
        if (w >= 260 && w <= 360 && ht >= 400 && ht <= 540) {
            PostMessageW(h, WM_CLOSE, 0, 0);
            closed++;
        }
    }
    if (closed > 0) Sleep(120);
}

static void close_unexpected_chats(const ChatWinInfo *before, int n_before, HWND keep_hwnd) {
    ChatWinInfo cur[MAX_WINS];
    int nc = list_chat_windows(cur, MAX_WINS);
    int closed = 0;
    for (int k = 0; k < nc; k++) {
        if (cur[k].hwnd == keep_hwnd) continue;
        int was_open = 0;
        for (int j = 0; j < n_before; j++) {
            if (before[j].hwnd == cur[k].hwnd) { was_open = 1; break; }
        }
        if (!was_open) {
            PostMessageW(cur[k].hwnd, WM_CLOSE, 0, 0);
            closed++;
        }
    }
    if (closed > 0) Sleep(100);
}

static void send_vk_input(WORD vk) {
    INPUT inp[2];
    ZeroMemory(inp, sizeof(inp));
    inp[0].type = INPUT_KEYBOARD; inp[0].ki.wVk = vk;
    inp[1].type = INPUT_KEYBOARD; inp[1].ki.wVk = vk; inp[1].ki.dwFlags = KEYEVENTF_KEYUP;
    SendInput(2, inp, sizeof(INPUT));
}

static void real_mouse_click_screen(int sx, int sy, int dbl) {
    SetCursorPos(sx, sy);
    Sleep(20);
    int vx = GetSystemMetrics(SM_CXSCREEN);
    int vy = GetSystemMetrics(SM_CYSCREEN);
    LONG ax = (vx > 1) ? (LONG)(((long long)sx * 65535LL) / (vx - 1)) : 0;
    LONG ay = (vy > 1) ? (LONG)(((long long)sy * 65535LL) / (vy - 1)) : 0;
    INPUT inp[3];
    ZeroMemory(inp, sizeof(inp));
    inp[0].type = INPUT_MOUSE;
    inp[0].mi.dx = ax;
    inp[0].mi.dy = ay;
    inp[0].mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE;
    inp[1].type = INPUT_MOUSE;
    inp[1].mi.dx = ax;
    inp[1].mi.dy = ay;
    inp[1].mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTDOWN;
    inp[2].type = INPUT_MOUSE;
    inp[2].mi.dx = ax;
    inp[2].mi.dy = ay;
    inp[2].mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_LEFTUP;
    SendInput(2, &inp[0], sizeof(INPUT));
    Sleep(25);
    SendInput(1, &inp[2], sizeof(INPUT));
    if (dbl) {
        Sleep(45);
        SendInput(1, &inp[1], sizeof(INPUT));
        Sleep(25);
        SendInput(1, &inp[2], sizeof(INPUT));
    }
}

static int clipboard_paste_wide(HWND target, const wchar_t *txt) {
    if (!txt) return 0;
    size_t len = wcslen(txt);
    size_t bytes = (len + 1) * sizeof(wchar_t);
    HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, bytes);
    if (!hMem) return 0;
    wchar_t *dst = (wchar_t *)GlobalLock(hMem);
    if (!dst) {
        GlobalFree(hMem);
        return 0;
    }
    memcpy(dst, txt, bytes);
    GlobalUnlock(hMem);

    if (!OpenClipboard(NULL)) {
        if (!target || !OpenClipboard(target)) {
            GlobalFree(hMem);
            return 0;
        }
    }
    EmptyClipboard();
    if (!SetClipboardData(CF_UNICODETEXT, hMem)) {
        GlobalFree(hMem);
        CloseClipboard();
        return 0;
    }
    CloseClipboard();

    send_ctrl_key('A');
    Sleep(40);
    send_ctrl_key('V');
    Sleep(80);
    return 1;
}

static HWND find_expanded_search_edit_in(HWND chats_pane) {
    if (!chats_pane) return NULL;
    HWND ed = FindWindowExW(chats_pane, NULL, L"Edit", NULL);
    if (!ed || !IsWindowVisible(ed)) return NULL;
    RECT rc = {0};
    GetWindowRect(ed, &rc);
    return ((rc.right - rc.left) >= 40) ? ed : NULL;
}

static HWND find_visible_chats_list(HWND chats_pane) {
    if (!chats_pane) return NULL;
    HWND best = NULL;
    LONG best_h = 0;
    HWND cur = NULL;
    for (;;) {
        cur = FindWindowExW(chats_pane, cur, L"EVA_VH_ListControl_Dblclk", NULL);
        if (!cur) break;
        if (!IsWindowVisible(cur)) continue;
        RECT rc = {0};
        GetWindowRect(cur, &rc);
        LONG h = rc.bottom - rc.top;
        if (h >= 40 && h > best_h) {
            best = cur;
            best_h = h;
        }
    }
    return best;
}

static HWND find_search_list_in(HWND chats_pane, int *out_is_search) {
    if (out_is_search) *out_is_search = 0;
    if (!chats_pane) return NULL;
    HWND cur = NULL;
    for (;;) {
        cur = FindWindowExW(chats_pane, cur, L"EVA_VH_ListControl_Dblclk", NULL);
        if (!cur) break;
        if (!IsWindowVisible(cur)) continue;
        RECT rc = {0};
        GetWindowRect(cur, &rc);
        if ((rc.bottom - rc.top) < 40) continue;
        wchar_t t[256] = {0};
        get_title(cur, t, 256);
        if (wcsistr_ascii(t, L"SearchListCtrl")) {
            if (out_is_search) *out_is_search = 1;
            return cur;
        }
    }
    return find_visible_chats_list(chats_pane);
}

static void force_foreground(HWND hwnd) {
    DWORD fg_tid = GetWindowThreadProcessId(GetForegroundWindow(), NULL);
    DWORD cur_tid = GetCurrentThreadId();
    DWORD tgt_tid = GetWindowThreadProcessId(hwnd, NULL);
    if (IsIconic(hwnd)) ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    if (fg_tid && fg_tid != cur_tid) AttachThreadInput(cur_tid, fg_tid, TRUE);
    if (tgt_tid && tgt_tid != cur_tid) AttachThreadInput(cur_tid, tgt_tid, TRUE);
    SetForegroundWindow(hwnd);
    BringWindowToTop(hwnd);
    if (tgt_tid && tgt_tid != cur_tid) AttachThreadInput(cur_tid, tgt_tid, FALSE);
    if (fg_tid && fg_tid != cur_tid) AttachThreadInput(cur_tid, fg_tid, FALSE);
}

static HWND ensure_search_bar_ready(HWND main_h, HWND *out_chats_pane) {
    HWND child = FindWindowExW(main_h, NULL, L"EVA_ChildWindow", NULL);
    HWND chats_pane = find_chats_pane(main_h);
    if (out_chats_pane) *out_chats_pane = chats_pane;

    if (chats_pane && !IsWindowVisible(chats_pane) && child) {
        RECT crc = {0};
        GetWindowRect(child, &crc);
        const int ys[4] = {95, 82, 112, 65};
        for (int i = 0; i < 4 && !IsWindowVisible(chats_pane); i++) {
            force_foreground(main_h);
            Sleep(60);
            real_mouse_click_screen(crc.left + 33, crc.top + ys[i], 0);
            Sleep(220);
        }
        if (!IsWindowVisible(chats_pane)) {
            force_foreground(main_h);
            Sleep(60);
            send_ctrl_key('2');
            Sleep(250);
        }
    }

    HWND exp = find_expanded_search_edit_in(chats_pane);
    if (exp) return exp;

    if (chats_pane) {
        force_foreground(main_h);
        Sleep(80);
        send_ctrl_key('F');
        for (int i = 0; i < 12; i++) {
            Sleep(50);
            exp = find_expanded_search_edit_in(chats_pane);
            if (exp) return exp;
        }

        RECT prc = {0};
        GetWindowRect(chats_pane, &prc);
        const int rx_offsets[2] = {56, 88};
        for (int k = 0; k < 2 && !exp; k++) {
            force_foreground(main_h);
            Sleep(40);
            real_mouse_click_screen(prc.right - rx_offsets[k], prc.top + 22, 0);
            for (int i = 0; i < 8; i++) {
                Sleep(50);
                exp = find_expanded_search_edit_in(chats_pane);
                if (exp) return exp;
            }
        }
    }

    if (chats_pane) {
        HWND ed = FindWindowExW(chats_pane, NULL, L"Edit", NULL);
        if (ed) return ed;
    }
    HWND edits[MAX_EDITS];
    int n = collect_search_edits(main_h, edits, MAX_EDITS, 1);
    if (n > 0) return edits[0];
    n = collect_search_edits(main_h, edits, MAX_EDITS, 0);
    return n > 0 ? edits[0] : NULL;
}

static wchar_t *get_ctrl_text(HWND h) {
    ULONG_PTR len = 0;
    if (!smto_w_ms(h, WM_GETTEXTLENGTH, 0, 0, &len, 500)) return NULL;
    wchar_t *buf = (wchar_t *)calloc((size_t)len + 4, sizeof(wchar_t));
    if (!buf) return NULL;
    smto_w_ms(h, WM_GETTEXT, (WPARAM)(len + 2), (LPARAM)buf, NULL, 500);
    return buf;
}

static int set_ctrl_text(HWND h, const wchar_t *txt) {
    return smto_w_ms(h, WM_SETTEXT, 0, (LPARAM)(txt ? txt : L""), NULL, 500);
}

static void replace_ctrl_text(HWND h, const wchar_t *txt) {
    smto_w_ms(h, EM_SETSEL, 0, (LPARAM)-1, NULL, 500);
    smto_w_ms(h, EM_REPLACESEL, 1, (LPARAM)(txt ? txt : L""), NULL, 500);
}

static void press_enter_msg(HWND h) {
    PostMessageW(h, WM_KEYDOWN, VK_RETURN, ENTER_DOWN_LPARAM);
    PostMessageW(h, WM_KEYUP, VK_RETURN, ENTER_UP_LPARAM);
}

static int inner_open_room(const wchar_t *room_w, const wchar_t *alt_w, HWND *out_hwnd, int *out_already, char *err, size_t errlen) {
    HWND existing = find_chat_window_any(room_w, alt_w);
    if (existing) {
        *out_hwnd = existing;
        *out_already = 1;
        return 1;
    }

    HWND main_h = find_main_window();
    if (!main_h) {
        snprintf(err, errlen, "KakaoTalk main window not found");
        return 0;
    }
    if (!IsWindowVisible(main_h)) {
        ShowWindow(main_h, SW_SHOW);
        Sleep(150);
    }
    if (IsIconic(main_h)) {
        ShowWindow(main_h, SW_RESTORE);
        Sleep(150);
    }
    close_stray_profile_popups(main_h);

    HWND chats_pane = NULL;
    HWND search_edit = ensure_search_bar_ready(main_h, &chats_pane);
    if (!search_edit) {
        snprintf(err, errlen, "No search Edit control found in KakaoTalk main window");
        return 0;
    }

    RECT erc = {0};
    GetWindowRect(search_edit, &erc);
    long edit_w = (long)(erc.right - erc.left);
    long edit_h = (long)(erc.bottom - erc.top);
    int chats_vis = (chats_pane && IsWindowVisible(chats_pane)) ? 1 : 0;

    ChatWinInfo before[MAX_WINS];
    int n_before = list_chat_windows(before, MAX_WINS);

    const wchar_t *queries[2] = { room_w, (alt_w && alt_w[0] && wcscmp(alt_w, room_w) != 0) ? alt_w : NULL };

    for (int qi = 0; qi < 2 && queries[qi]; qi++) {
        force_foreground(main_h);
        GetWindowRect(search_edit, &erc);
        edit_w = (long)(erc.right - erc.left);
        edit_h = (long)(erc.bottom - erc.top);
        if (edit_w >= 40 && edit_h >= 10) {
            real_mouse_click_screen((erc.left + erc.right) / 2, (erc.top + erc.bottom) / 2, 0);
            Sleep(60);
        } else {
            SetFocus(search_edit);
        }

        clipboard_paste_wide(search_edit, queries[qi]);
        wchar_t *rb = get_ctrl_text(search_edit);
        int rb_len = rb ? (int)wcslen(rb) : -1;
        if (rb_len <= 0) {
            replace_ctrl_text(search_edit, queries[qi]);
            free(rb);
            rb = get_ctrl_text(search_edit);
            rb_len = rb ? (int)wcslen(rb) : -1;
            if (rb_len <= 0) {
                set_ctrl_text(search_edit, queries[qi]);
                free(rb);
                rb = get_ctrl_text(search_edit);
                rb_len = rb ? (int)wcslen(rb) : -1;
            }
            HWND par = GetParent(search_edit);
            if (par) {
                int cid = GetDlgCtrlID(search_edit);
                PostMessageW(par, WM_COMMAND, MAKEWPARAM(cid, EN_CHANGE), (LPARAM)search_edit);
            }
        }
        free(rb);

        Sleep(600);

        HWND found = NULL;
        int is_search_list = 0;
        HWND list_h = find_search_list_in(chats_pane, &is_search_list);
        RECT lrc = {0};
        long list_w = 0, list_h_px = 0;
        if (list_h) {
            GetWindowRect(list_h, &lrc);
            list_w = (long)(lrc.right - lrc.left);
            list_h_px = (long)(lrc.bottom - lrc.top);
        }

        HwndList before_tops = {0};
        EnumWindows(enum_top_kakao_cb, (LPARAM)&before_tops);
        int popups_closed = 0;

        /* Strategy 1: Clean Win32 double-click sequence directly on list_h across deeper row offsets */
        const int row_y[9] = {28, 64, 100, 140, 180, 220, 260, 300, 340};
        for (int ri = 0; ri < 9 && !found && list_h && list_w >= 80; ri++) {
            if (list_h_px < row_y[ri] + 16) break;
            post_dblclick_client(list_h, 120, row_y[ri]);
            for (int poll = 0; poll < 6; poll++) {
                Sleep(50);
                found = find_chat_window_any(room_w, alt_w);
                if (found) break;
            }
            if (found) break;
            int pc = close_new_nonchat_popups(main_h, &before_tops);
            popups_closed += pc;
            close_unexpected_chats(before, n_before, NULL);
        }

        /* Strategy 2: Keyboard Down + Enter on list_h */
        if (!found && list_h) {
            PostMessageW(list_h, WM_KEYDOWN, VK_DOWN, 0);
            PostMessageW(list_h, WM_KEYUP, VK_DOWN, 0);
            Sleep(80);
            press_enter_msg(list_h);
            for (int poll = 0; poll < 6; poll++) {
                Sleep(50);
                found = find_chat_window_any(room_w, alt_w);
                if (found) break;
            }
            if (!found) {
                popups_closed += close_new_nonchat_popups(main_h, &before_tops);
                close_unexpected_chats(before, n_before, NULL);
            }
        }

        /* Strategy 3: Real screen-coordinate double-click via SendInput (separate from PostMessage) */
        const int sy_off[4] = {32, 68, 108, 148};
        for (int ri = 0; ri < 4 && !found && list_h && list_w >= 80; ri++) {
            if (list_h_px < sy_off[ri] + 16) break;
            force_foreground(main_h);
            real_mouse_click_screen(lrc.left + 120, lrc.top + sy_off[ri], 1);
            for (int poll = 0; poll < 8; poll++) {
                Sleep(50);
                found = find_chat_window_any(room_w, alt_w);
                if (found) break;
            }
            if (found) break;
            popups_closed += close_new_nonchat_popups(main_h, &before_tops);
            close_unexpected_chats(before, n_before, NULL);
        }

        /* Strategy 4: Enter on search_edit last */
        if (!found) {
            force_foreground(main_h);
            SetFocus(search_edit);
            press_enter_msg(search_edit);
            send_vk_input(VK_RETURN);
            for (int poll = 0; poll < 6; poll++) {
                Sleep(50);
                found = find_chat_window_any(room_w, alt_w);
                if (found) break;
            }
        }

        if (!found) {
            popups_closed += close_new_nonchat_popups(main_h, &before_tops);
        }

        POINT cur = {0};
        GetCursorPos(&cur);
        int top_vis = count_visible_kakao_tops();
        ChatWinInfo mid[MAX_WINS];
        int n_mid = list_chat_windows(mid, MAX_WINS);
        fprintf(stderr, "[open] chats_vis=%d edit=%ldx%ld list=(%ld,%ld,%ldx%ld) is_search=%d qi=%d rb_len=%d found=%d n_before=%d n_mid=%d top_vis=%d popups=%d cur=(%ld,%ld)\n",
                chats_vis, edit_w, edit_h, (long)lrc.left, (long)lrc.top, list_w, list_h_px, is_search_list, qi, rb_len, found ? 1 : 0, n_before, n_mid, top_vis, popups_closed, (long)cur.x, (long)cur.y);
        fflush(stderr);

        replace_ctrl_text(search_edit, L"");
        set_ctrl_text(search_edit, L"");

        if (found) {
            PostMessageW(search_edit, WM_KEYDOWN, VK_ESCAPE, 0);
            PostMessageW(search_edit, WM_KEYUP, VK_ESCAPE, 0);
            close_stray_profile_popups(main_h);
            ShowWindow(found, SW_SHOWMINNOACTIVE);
            *out_hwnd = found;
            *out_already = 0;
            return 1;
        }

        /* Close any unintended chat window opened by mismatch before trying next query */
        for (int k = 0; k < n_mid; k++) {
            int was_open = 0;
            for (int j = 0; j < n_before; j++) {
                if (before[j].hwnd == mid[k].hwnd) { was_open = 1; break; }
            }
            if (!was_open) PostMessageW(mid[k].hwnd, WM_CLOSE, 0, 0);
        }
    }

    PostMessageW(search_edit, WM_KEYDOWN, VK_ESCAPE, 0);
    PostMessageW(search_edit, WM_KEYUP, VK_ESCAPE, 0);
    snprintf(err, errlen, "Could not open chat window matching room title");
    return 0;
}

static void click_send_btn(HWND chat_hwnd) {
    if (IsIconic(chat_hwnd)) {
        ShowWindow(chat_hwnd, SW_SHOWNOACTIVATE);
        Sleep(200);
    }
    RECT rc;
    if (!GetClientRect(chat_hwnd, &rc)) return;
    UINT dpi = g_GetDpiForWindow ? g_GetDpiForWindow(chat_hwnd) : 96;
    if (!dpi) dpi = 96;
    double scale = (double)dpi / 96.0;
    int x = (int)(rc.right - (LONG)(36.0 * scale + 0.5));
    int y = (int)(rc.bottom - (LONG)(23.0 * scale + 0.5));
    LPARAM lp = MAKELPARAM(x, y);
    PostMessageW(chat_hwnd, WM_MOUSEMOVE, 0, lp);
    Sleep(30);
    PostMessageW(chat_hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp);
    Sleep(30);
    PostMessageW(chat_hwnd, WM_LBUTTONUP, 0, lp);
}

static char *handle_probe(void) {
    StrBuf sb = {0};
    HWND main_h = find_main_window();
    if (!main_h) {
        g_kakao_pid = 0;
        main_h = find_main_window();
    }
    ChatWinInfo wins[MAX_WINS];
    int nw = list_chat_windows(wins, MAX_WINS);
    HWND edits[MAX_EDITS];
    int n_edits = main_h ? collect_search_edits(main_h, edits, MAX_EDITS, 0) : 0;

    RECT mrc = {0};
    if (main_h) GetWindowRect(main_h, &mrc);
    sb_printf(&sb, "{\"ok\":true,\"main_hwnd\":%llu,\"main_visible\":%s,\"main_w\":%ld,\"main_h\":%ld,\"search_edits\":%d,\"edits\":[",
              (unsigned long long)(ULONG_PTR)main_h,
              (main_h && IsWindowVisible(main_h)) ? "true" : "false",
              (long)(mrc.right - mrc.left),
              (long)(mrc.bottom - mrc.top),
              n_edits);
    for (int i = 0; i < n_edits; i++) {
        RECT rc = {0};
        GetWindowRect(edits[i], &rc);
        wchar_t pcls[128] = {0};
        HWND par = GetParent(edits[i]);
        if (par) get_cls(par, pcls, 128);
        if (i > 0) sb_append(&sb, ",");
        sb_printf(&sb, "{\"hwnd\":%llu,\"visible\":%s,\"enabled\":%s,\"x\":%ld,\"y\":%ld,\"w\":%ld,\"h\":%ld,\"parent\":",
                  (unsigned long long)(ULONG_PTR)edits[i],
                  IsWindowVisible(edits[i]) ? "true" : "false",
                  IsWindowEnabled(edits[i]) ? "true" : "false",
                  (long)(rc.left - mrc.left),
                  (long)(rc.top - mrc.top),
                  (long)(rc.right - rc.left),
                  (long)(rc.bottom - rc.top));
        sb_append_json_wide(&sb, pcls);
        sb_append(&sb, "}");
    }
    sb_append(&sb, "],\"main_children\":[");
    if (main_h) {
        HwndList mc = {0};
        EnumChildWindows(main_h, enum_child_all_cb, (LPARAM)&mc);
        for (int i = 0; i < mc.count && i < 32; i++) {
            RECT rc = {0};
            GetWindowRect(mc.items[i], &rc);
            wchar_t cls[128] = {0};
            get_cls(mc.items[i], cls, 128);
            wchar_t ttl[256] = {0};
            if (wcsncmp(cls, L"EVA_", 4) == 0) {
                get_title(mc.items[i], ttl, 256);
            }
            HWND par = GetParent(mc.items[i]);
            if (i > 0) sb_append(&sb, ",");
            sb_printf(&sb, "{\"hwnd\":%llu,\"parent\":%llu,\"vis\":%s,\"x\":%ld,\"y\":%ld,\"w\":%ld,\"h\":%ld,\"cls\":",
                      (unsigned long long)(ULONG_PTR)mc.items[i],
                      (unsigned long long)(ULONG_PTR)par,
                      IsWindowVisible(mc.items[i]) ? "true" : "false",
                      (long)(rc.left - mrc.left),
                      (long)(rc.top - mrc.top),
                      (long)(rc.right - rc.left),
                      (long)(rc.bottom - rc.top));
            sb_append_json_wide(&sb, cls);
            sb_append(&sb, ",\"title\":");
            sb_append_json_wide(&sb, ttl);
            sb_append(&sb, "}");
        }
    }
    sb_append(&sb, "],\"chat_windows\":[");
    for (int i = 0; i < nw; i++) {
        if (i > 0) sb_append(&sb, ",");
        sb_printf(&sb, "{\"hwnd\":%llu,\"input_hwnd\":%llu,\"inputs\":%d,\"minimized\":%s,\"title\":",
                  (unsigned long long)(ULONG_PTR)wins[i].hwnd,
                  (unsigned long long)(ULONG_PTR)wins[i].input_hwnd,
                  wins[i].input_count,
                  wins[i].minimized ? "true" : "false");
        sb_append_json_wide(&sb, wins[i].title);
        sb_append(&sb, "}");
    }
    sb_append(&sb, "]}");
    return sb.buf;
}

static char *handle_open(const char *body) {
    char *room_u = json_extract_string(body, "room");
    char *alt_u = json_extract_string(body, "alt_room");
    if (!room_u || !room_u[0]) {
        free(room_u); free(alt_u);
        return _strdup("{\"ok\":false,\"error\":\"missing room\"}");
    }
    wchar_t *room_w = utf8_to_wide(room_u);
    wchar_t *alt_w = alt_u ? utf8_to_wide(alt_u) : NULL;
    free(room_u); free(alt_u);

    EnterCriticalSection(&g_action_lock);
    HWND chat_hwnd = NULL;
    int already = 0;
    char err[256] = {0};
    int ok = inner_open_room(room_w, alt_w, &chat_hwnd, &already, err, sizeof(err));
    LeaveCriticalSection(&g_action_lock);
    free(room_w);
    free(alt_w);

    StrBuf sb = {0};
    if (ok) {
        sb_printf(&sb, "{\"ok\":true,\"already_open\":%s,\"hwnd\":%llu}",
                  already ? "true" : "false",
                  (unsigned long long)(ULONG_PTR)chat_hwnd);
    } else {
        sb_append(&sb, "{\"ok\":false,\"error\":");
        sb_append_json_str(&sb, err);
        sb_append(&sb, "}");
    }
    return sb.buf;
}

static char *handle_send(const char *body) {
    char *room_u = json_extract_string(body, "room");
    char *alt_u = json_extract_string(body, "alt_room");
    char *text_u = json_extract_string(body, "text");
    if (!room_u || !room_u[0] || !text_u || !text_u[0]) {
        free(room_u); free(alt_u); free(text_u);
        return _strdup("{\"ok\":false,\"confirmed\":false,\"error\":\"missing room or text\"}");
    }
    wchar_t *room_w = utf8_to_wide(room_u);
    wchar_t *alt_w = alt_u ? utf8_to_wide(alt_u) : NULL;
    wchar_t *text_w_raw = utf8_to_wide(text_u);
    wchar_t *text_w = normalize_crlf_wide(text_w_raw);
    free(room_u); free(alt_u); free(text_u); free(text_w_raw);

    EnterCriticalSection(&g_action_lock);
    HWND chat_hwnd = NULL;
    int already = 0;
    char err[256] = {0};
    int ok = inner_open_room(room_w, alt_w, &chat_hwnd, &already, err, sizeof(err));
    HWND inp = NULL;
    int confirmed = 0;

    if (ok) {
        if (IsIconic(chat_hwnd)) {
            ShowWindow(chat_hwnd, SW_SHOWNOACTIVATE);
            Sleep(80);
        }
        if (IsHungAppWindow(chat_hwnd)) {
            ok = 0;
            snprintf(err, sizeof(err), "KakaoTalk chat window is hung");
        } else {
            HwndList inputs = {0};
            collect_input_controls(chat_hwnd, &inputs);
            if (inputs.count != 1) {
                ok = 0;
                snprintf(err, sizeof(err), "Expected 1 input control, found %d", inputs.count);
            } else {
                inp = inputs.items[0];
                wchar_t *draft = get_ctrl_text(inp);
                if (draft && !is_empty_input_text(draft)) {
                    ok = 0;
                    snprintf(err, sizeof(err), "Chat input already has an unsent draft");
                } else {
                    replace_ctrl_text(inp, text_w);
                    wchar_t *readback = get_ctrl_text(inp);
                    if (!readback || !same_normalized_wide(readback, text_w)) {
                        set_ctrl_text(inp, draft ? draft : L"");
                        ok = 0;
                        snprintf(err, sizeof(err), "Input text readback mismatch");
                    } else {
                        press_enter_msg(inp);
                        for (int i = 0; i < 15; i++) {
                            Sleep(40);
                            wchar_t *cur = get_ctrl_text(inp);
                            int cleared = (cur && is_empty_input_text(cur));
                            free(cur);
                            if (cleared) { confirmed = 1; break; }
                        }
                        if (!confirmed) {
                            click_send_btn(chat_hwnd);
                            for (int i = 0; i < 20; i++) {
                                Sleep(40);
                                wchar_t *cur = get_ctrl_text(inp);
                                int cleared = (cur && is_empty_input_text(cur));
                                free(cur);
                                if (cleared) { confirmed = 1; break; }
                            }
                        }
                        if (!confirmed) {
                            ok = 0;
                            snprintf(err, sizeof(err), "Message entered but send confirmation timed out");
                        }
                    }
                    free(readback);
                }
                free(draft);
            }
        }
    }
    LeaveCriticalSection(&g_action_lock);
    free(room_w);
    free(alt_w);
    free(text_w);

    StrBuf sb = {0};
    if (ok) {
        sb_printf(&sb, "{\"ok\":true,\"confirmed\":%s,\"hwnd\":%llu,\"input_hwnd\":%llu}",
                  confirmed ? "true" : "false",
                  (unsigned long long)(ULONG_PTR)chat_hwnd,
                  (unsigned long long)(ULONG_PTR)inp);
    } else {
        sb_append(&sb, "{\"ok\":false,\"confirmed\":false,\"error\":");
        sb_append_json_str(&sb, err);
        sb_append(&sb, "}");
    }
    return sb.buf;
}

static HWND wait_new_kakao_dialog(const HwndList *before_all, HWND ignore_hwnd, int polls, int sleep_ms) {
    for (int t = 0; t < polls; t++) {
        Sleep((DWORD)sleep_ms);
        HwndList now = {0};
        EnumWindows(enum_top_kakao_cb, (LPARAM)&now);
        for (int i = 0; i < now.count; i++) {
            HWND h = now.items[i];
            if (h == ignore_hwnd || !IsWindowVisible(h)) continue;
            int known = 0;
            if (before_all) {
                for (int j = 0; j < before_all->count; j++) {
                    if (before_all->items[j] == h) { known = 1; break; }
                }
            }
            if (known) continue;
            RECT rc = {0};
            GetWindowRect(h, &rc);
            if ((rc.right - rc.left) >= 100 && (rc.bottom - rc.top) >= 100) {
                return h;
            }
        }
    }
    return NULL;
}

static int wait_dialog_closed(HWND dlg, int polls, int sleep_ms) {
    for (int i = 0; i < polls; i++) {
        if (!IsWindow(dlg) || !IsWindowVisible(dlg)) return 1;
        Sleep((DWORD)sleep_ms);
    }
    return (!IsWindow(dlg) || !IsWindowVisible(dlg)) ? 1 : 0;
}

static int submit_open_file_dialog(HWND file_dlg, const wchar_t *path_w) {
    ChildHwndList ch = {0};
    EnumChildWindows(file_dlg, enum_child_many_cb, (LPARAM)&ch);
    HWND ed_1148 = NULL;
    HWND ed_last = NULL;
    for (int i = 0; i < ch.count; i++) {
        if (!IsWindowVisible(ch.items[i])) continue;
        wchar_t cls[64] = {0};
        get_cls(ch.items[i], cls, 64);
        if (_wcsicmp(cls, L"Edit") == 0) {
            if (GetDlgCtrlID(ch.items[i]) == 1148) ed_1148 = ch.items[i];
            ed_last = ch.items[i];
        }
    }
    HWND ed = ed_1148 ? ed_1148 : ed_last;
    if (ed) {
        force_foreground(file_dlg);
        SetFocus(ed);
        replace_ctrl_text(ed, path_w);
        set_ctrl_text(ed, path_w);
        HWND par = GetParent(ed);
        if (par) {
            int cid = GetDlgCtrlID(ed);
            PostMessageW(par, WM_COMMAND, MAKEWPARAM(cid, EN_CHANGE), (LPARAM)ed);
        }
        Sleep(100);
    }
    PostMessageW(file_dlg, WM_COMMAND, MAKEWPARAM(IDOK, BN_CLICKED), 0);
    Sleep(80);
    if (IsWindow(file_dlg) && IsWindowVisible(file_dlg)) {
        force_foreground(file_dlg);
        if (ed) press_enter_msg(ed);
        else press_enter_msg(file_dlg);
        send_vk_input(VK_RETURN);
    }
    return wait_dialog_closed(file_dlg, 24, 50);
}

static int confirm_send_dialog(HWND dialog) {
    force_foreground(dialog);
    Sleep(150);

    press_enter_msg(dialog);
    Sleep(60);
    send_vk_input(VK_RETURN);
    if (wait_dialog_closed(dialog, 16, 50)) return 1;

    ChildHwndList ch = {0};
    EnumChildWindows(dialog, enum_child_many_cb, (LPARAM)&ch);
    for (int i = 0; i < ch.count; i++) {
        HWND btn = ch.items[i];
        if (!IsWindowVisible(btn)) continue;
        wchar_t cls[64] = {0}, ttl[128] = {0};
        get_cls(btn, cls, 64);
        if (_wcsicmp(cls, L"Button") != 0) continue;
        get_title(btn, ttl, 128);
        int is_ok = (GetDlgCtrlID(btn) == IDOK)
            || wcsstr(ttl, L"\uC804\uC1A1") != NULL
            || wcsstr(ttl, L"\uBCF4\uB0B4\uAE30") != NULL
            || wcsstr(ttl, L"\uD655\uC778") != NULL
            || wcsistr_ascii(ttl, L"Send");
        if (!is_ok) continue;
        PostMessageW(btn, BM_CLICK, 0, 0);
        RECT brc = {0};
        if (GetWindowRect(btn, &brc)) {
            force_foreground(dialog);
            real_mouse_click_screen((brc.left + brc.right) / 2, (brc.top + brc.bottom) / 2, 0);
        }
        if (wait_dialog_closed(dialog, 16, 50)) return 1;
    }

    RECT drc = {0};
    if (GetWindowRect(dialog, &drc)) {
        long dw = drc.right - drc.left;
        const int xs[2] = { (int)(drc.left + dw / 2 - 42), (int)(drc.right - 70) };
        for (int k = 0; k < 2; k++) {
            force_foreground(dialog);
            real_mouse_click_screen(xs[k], drc.bottom - 28, 0);
            if (wait_dialog_closed(dialog, 12, 50)) return 1;
        }
    }
    return (!IsWindow(dialog) || !IsWindowVisible(dialog)) ? 1 : 0;
}

static char *handle_send_file(const char *body) {
    char *room_u = json_extract_string(body, "room");
    char *alt_u = json_extract_string(body, "alt_room");
    char *path_u = json_extract_string(body, "win_path");
    int dry_run = json_extract_bool(body, "dry_run");
    if (!room_u || !room_u[0] || !path_u || !path_u[0]) {
        free(room_u); free(alt_u); free(path_u);
        return _strdup("{\"ok\":false,\"confirmed\":false,\"error\":\"missing room or win_path\"}");
    }
    wchar_t *room_w = utf8_to_wide(room_u);
    wchar_t *alt_w = alt_u ? utf8_to_wide(alt_u) : NULL;
    wchar_t *path_w = utf8_to_wide(path_u);
    free(room_u); free(alt_u); free(path_u);

    if (GetFileAttributesW(path_w) == INVALID_FILE_ATTRIBUTES) {
        free(room_w); free(alt_w); free(path_w);
        return _strdup("{\"ok\":false,\"confirmed\":false,\"error\":\"file not found on Wine Z: path\"}");
    }

    EnterCriticalSection(&g_action_lock);
    HWND chat_hwnd = NULL;
    int already = 0;
    int confirmed = 0;
    const char *via = "";
    char err[256] = {0};
    int ok = inner_open_room(room_w, alt_w, &chat_hwnd, &already, err, sizeof(err));
    if (ok) {
        size_t plen = wcslen(path_w);
        size_t bytes = sizeof(DROPFILES) + (plen + 2) * sizeof(wchar_t);
        HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, bytes);
        if (!hMem) {
            ok = 0;
            snprintf(err, sizeof(err), "GlobalAlloc failed");
        } else {
            DROPFILES *df = (DROPFILES *)GlobalLock(hMem);
            df->pFiles = sizeof(DROPFILES);
            df->fWide = TRUE;
            memcpy((char *)df + sizeof(DROPFILES), path_w, (plen + 1) * sizeof(wchar_t));
            GlobalUnlock(hMem);

            if (!OpenClipboard(NULL) && !OpenClipboard(GetDesktopWindow())) {
                GlobalFree(hMem);
                ok = 0;
                snprintf(err, sizeof(err), "OpenClipboard failed");
            } else {
                EmptyClipboard();
                if (!SetClipboardData(CF_HDROP, hMem)) {
                    GlobalFree(hMem);
                    CloseClipboard();
                    ok = 0;
                    snprintf(err, sizeof(err), "SetClipboardData failed");
                } else {
                    CloseClipboard();

                    if (IsIconic(chat_hwnd)) ShowWindow(chat_hwnd, SW_RESTORE);
                    force_foreground(chat_hwnd);
                    Sleep(120);

                    HwndList before_all = {0};
                    EnumWindows(enum_top_kakao_cb, (LPARAM)&before_all);

                    HwndList inputs = {0};
                    collect_input_controls(chat_hwnd, &inputs);
                    if (inputs.count > 0) {
                        RECT irc = {0};
                        GetWindowRect(inputs.items[0], &irc);
                        real_mouse_click_screen((irc.left + irc.right) / 2, (irc.top + irc.bottom) / 2, 0);
                        Sleep(60);
                        PostMessageW(inputs.items[0], WM_PASTE, 0, 0);
                    }

                    HWND dialog = wait_new_kakao_dialog(&before_all, chat_hwnd, 6, 50);
                    if (dialog) {
                        via = dry_run ? "cf_hdrop_paste_dry" : "cf_hdrop_paste";
                    } else {
                        force_foreground(chat_hwnd);
                        if (inputs.count > 0) SetFocus(inputs.items[0]);
                        send_ctrl_key('V');
                        dialog = wait_new_kakao_dialog(&before_all, chat_hwnd, 24, 50);
                        if (dialog) {
                            via = dry_run ? "cf_hdrop_ctrlv_dry" : "cf_hdrop_ctrlv";
                        }
                    }

                    if (!dialog) {
                        EnumWindows(enum_top_kakao_cb, (LPARAM)&before_all);
                        force_foreground(chat_hwnd);
                        if (inputs.count > 0) {
                            RECT irc = {0};
                            GetWindowRect(inputs.items[0], &irc);
                            real_mouse_click_screen((irc.left + irc.right) / 2, (irc.top + irc.bottom) / 2, 0);
                            Sleep(60);
                        }
                        send_ctrl_key('T');
                        HWND file_dlg = wait_new_kakao_dialog(&before_all, chat_hwnd, 30, 50);
                        if (file_dlg) {
                            log_sendfile_dialog("ctrl_t_open", file_dlg, dry_run);
                            if (dry_run) {
                                PostMessageW(file_dlg, WM_CLOSE, 0, 0);
                                Sleep(150);
                                confirmed = 1;
                                via = "ctrl_t_dry";
                            } else {
                                wchar_t fcls[64] = {0};
                                get_cls(file_dlg, fcls, 64);
                                if (_wcsicmp(fcls, L"#32770") == 0) {
                                    int submitted = submit_open_file_dialog(file_dlg, path_w);
                                    if (submitted) {
                                        HWND secondary = wait_new_kakao_dialog(&before_all, chat_hwnd, 20, 50);
                                        if (secondary) {
                                            dialog = secondary;
                                            via = "ctrl_t_confirm";
                                        } else {
                                            confirmed = 1;
                                            via = "ctrl_t";
                                        }
                                    }
                                } else {
                                    dialog = file_dlg;
                                    via = "ctrl_t_dialog";
                                }
                            }
                        }
                    }

                    if (dialog) {
                        log_sendfile_dialog(via, dialog, dry_run);
                        if (dry_run) {
                            PostMessageW(dialog, WM_CLOSE, 0, 0);
                            Sleep(150);
                            confirmed = 1;
                        } else {
                            confirmed = confirm_send_dialog(dialog);
                        }
                    }

                    if (!confirmed) {
                        ok = 0;
                        snprintf(err, sizeof(err), "KakaoTalk file send dialog did not open or confirm");
                        fprintf(stderr, "[send-file] FAILED via=%s dry_run=%d\n", via, dry_run);
                        fflush(stderr);
                    }
                    if (!already) ShowWindow(chat_hwnd, SW_SHOWMINNOACTIVE);
                }
            }
        }
    }
    LeaveCriticalSection(&g_action_lock);
    free(room_w);
    free(alt_w);
    free(path_w);

    StrBuf sb = {0};
    if (ok) {
        sb_printf(&sb, "{\"ok\":true,\"confirmed\":%s,\"dry_run\":%s,\"via\":",
                  confirmed ? "true" : "false",
                  dry_run ? "true" : "false");
        sb_append_json_str(&sb, via);
        sb_append(&sb, "}");
    } else {
        sb_append(&sb, "{\"ok\":false,\"confirmed\":false,\"error\":");
        sb_append_json_str(&sb, err);
        sb_append(&sb, "}");
    }
    return sb.buf;
}

/* Close the Ctrl+E emoticon popup cleanly without ever sending global VK_ESCAPE. */
static int close_emoticon_popup(HWND popup, const RECT *prc) {
    if (!IsWindow(popup) || !IsWindowVisible(popup)) return 1;
    PostMessageW(popup, WM_CLOSE, 0, 0);
    if (wait_dialog_closed(popup, 10, 40)) return 1;
    if (prc) {
        force_foreground(popup);
        Sleep(30);
        real_mouse_click_screen(prc->right - 18, prc->top + 16, 0);
        if (wait_dialog_closed(popup, 12, 40)) return 1;
    }
    return (!IsWindow(popup) || !IsWindowVisible(popup)) ? 1 : 0;
}

static char *handle_send_emoticon(const char *body) {
    char *room_u = json_extract_string(body, "room");
    char *alt_u = json_extract_string(body, "alt_room");
    char *kind_u = json_extract_string(body, "kind");
    int pack_pos = json_extract_int(body, "pack_pos", -1);
    int grid_idx = json_extract_int(body, "grid_idx", -1);
    int dry_run = json_extract_bool(body, "dry_run");

    if (!room_u || !room_u[0] || pack_pos < 0 || pack_pos >= 64 || grid_idx < 0 || grid_idx >= 256) {
        free(room_u); free(alt_u); free(kind_u);
        return _strdup("{\"ok\":false,\"confirmed\":false,\"error\":\"invalid emoticon target\"}");
    }

    int is_emoji = (kind_u && strcmp(kind_u, "emoji") == 0) ? 1 : 0;
    wchar_t *room_w = utf8_to_wide(room_u);
    wchar_t *alt_w = alt_u ? utf8_to_wide(alt_u) : NULL;
    free(room_u); free(alt_u); free(kind_u);

    EnterCriticalSection(&g_action_lock);
    HWND chat_hwnd = NULL;
    int already = 0;
    int confirmed = 0;
    const char *via = "";
    char err[256] = {0};
    int ok = inner_open_room(room_w, alt_w, &chat_hwnd, &already, err, sizeof(err));

    if (ok) {
        if (IsIconic(chat_hwnd)) ShowWindow(chat_hwnd, SW_RESTORE);
        force_foreground(chat_hwnd);
        Sleep(120);

        HwndList inputs = {0};
        collect_input_controls(chat_hwnd, &inputs);
        if (inputs.count < 1) {
            ok = 0;
            snprintf(err, sizeof(err), "No chat input control found");
        } else {
            HWND inp = inputs.items[0];
            RECT irc = {0};
            GetWindowRect(inp, &irc);
            real_mouse_click_screen((irc.left + irc.right) / 2, (irc.top + irc.bottom) / 2, 0);
            Sleep(80);

            HwndList before_all = {0};
            EnumWindows(enum_top_kakao_cb, (LPARAM)&before_all);
            send_ctrl_key('E');
            HWND popup = wait_new_kakao_dialog(&before_all, chat_hwnd, 30, 50);

            if (!popup) {
                ok = 0;
                snprintf(err, sizeof(err), "KakaoTalk emoticon popup did not open");
            } else {
                Sleep(220);
                RECT prc = {0};
                GetWindowRect(popup, &prc);
                long pw = prc.right - prc.left;
                long ph = prc.bottom - prc.top;

                if (pw < 300 || ph < 400) {
                    PostMessageW(popup, WM_CLOSE, 0, 0);
                    wait_dialog_closed(popup, 10, 40);
                    ok = 0;
                    snprintf(err, sizeof(err), "Unexpected emoticon popup size: %ldx%ld", pw, ph);
                } else {
                    /* Select category tab: [이모티콘] at (50, 50) or [미니 이모티콘] at (148, 50) */
                    real_mouse_click_screen(prc.left + (is_emoji ? 148 : 50), prc.top + 50, 0);
                    Sleep(180);

                    /* Select pack slot in id=104 strip (slot 0 is Recent at x=30; owned packs start at slot 1) */
                    int slot = pack_pos + 1;
                    if (slot < 8) {
                        real_mouse_click_screen(prc.left + 30 + slot * 39, prc.top + 95, 0);
                        Sleep(220);
                    } else {
                        real_mouse_click_screen(prc.left + 30, prc.top + 95, 0);
                        Sleep(80);
                        for (int s = 0; s < slot; s++) {
                            send_vk_input(VK_RIGHT);
                            Sleep(25);
                        }
                        send_vk_input(VK_RETURN);
                        Sleep(220);
                    }

                    /* Move selection inside grid id=105 via VK_TAB + arrow keys */
                    int cols = is_emoji ? 6 : 4;
                    int row = grid_idx / cols;
                    int col = grid_idx % cols;
                    send_vk_input(VK_TAB);
                    Sleep(100);
                    if (row == 0 && col == 0) {
                        send_vk_input(VK_RIGHT);
                        Sleep(40);
                        send_vk_input(VK_LEFT);
                        Sleep(50);
                    } else {
                        for (int r = 0; r < row; r++) {
                            send_vk_input(VK_DOWN);
                            Sleep(35);
                        }
                        for (int c = 0; c < col; c++) {
                            send_vk_input(VK_RIGHT);
                            Sleep(35);
                        }
                        Sleep(80);
                    }

                    if (dry_run) {
                        confirmed = close_emoticon_popup(popup, &prc);
                        if (!confirmed) {
                            ok = 0;
                            snprintf(err, sizeof(err), "Emoticon popup did not close during dry_run");
                        } else {
                            via = is_emoji ? "native_ctrl_e_emoji_dry" : "native_ctrl_e_sticker_dry";
                        }
                    } else if (!is_emoji) {
                        /* First Enter attaches the sticker to the chat preview bar; close popup then press Enter in chat window to send */
                        send_vk_input(VK_RETURN);
                        Sleep(200);
                        close_emoticon_popup(popup, &prc);
                        force_foreground(chat_hwnd);
                        SetFocus(inp);
                        Sleep(80);
                        send_vk_input(VK_RETURN);
                        Sleep(200);
                        confirmed = 1;
                        via = "native_ctrl_e_sticker";
                    } else {
                        /* Mini emoji inserts into chat input; close popup and submit input */
                        send_vk_input(VK_RETURN);
                        Sleep(180);
                        close_emoticon_popup(popup, &prc);
                        force_foreground(chat_hwnd);
                        SetFocus(inp);
                        Sleep(80);
                        press_enter_msg(inp);
                        for (int i = 0; i < 15; i++) {
                            Sleep(40);
                            wchar_t *cur = get_ctrl_text(inp);
                            int cleared = (cur && is_empty_input_text(cur));
                            free(cur);
                            if (cleared) { confirmed = 1; break; }
                        }
                        if (!confirmed) {
                            send_vk_input(VK_RETURN);
                            for (int i = 0; i < 15; i++) {
                                Sleep(40);
                                wchar_t *cur = get_ctrl_text(inp);
                                int cleared = (cur && is_empty_input_text(cur));
                                free(cur);
                                if (cleared) { confirmed = 1; break; }
                            }
                        }
                        if (!confirmed) {
                            click_send_btn(chat_hwnd);
                            for (int i = 0; i < 20; i++) {
                                Sleep(40);
                                wchar_t *cur = get_ctrl_text(inp);
                                int cleared = (cur && is_empty_input_text(cur));
                                free(cur);
                                if (cleared) { confirmed = 1; break; }
                            }
                        }
                        if (!confirmed) {
                            ok = 0;
                            snprintf(err, sizeof(err), "Mini emoji inserted but send confirmation timed out");
                        } else {
                            via = "native_ctrl_e_emoji";
                        }
                    }
                }
            }
        }
        if (!already) ShowWindow(chat_hwnd, SW_SHOWMINNOACTIVE);
    }

    LeaveCriticalSection(&g_action_lock);
    free(room_w);
    free(alt_w);

    StrBuf sb = {0};
    if (ok) {
        sb_printf(&sb, "{\"ok\":true,\"confirmed\":%s,\"dry_run\":%s,\"via\":",
                  confirmed ? "true" : "false",
                  dry_run ? "true" : "false");
        sb_append_json_str(&sb, via);
        sb_append(&sb, "}");
    } else {
        sb_append(&sb, "{\"ok\":false,\"confirmed\":false,\"error\":");
        sb_append_json_str(&sb, err);
        sb_append(&sb, "}");
    }
    return sb.buf;
}

/* ---------- Minimal Multi-Threaded HTTP/1.1 Loopback Server ---------- */

static void send_http_json(SOCKET cli, int status, const char *json) {
    const char *msg = (status == 200) ? "OK" : (status == 400 ? "Bad Request" : "Not Found");
    int blen = json ? (int)strlen(json) : 2;
    char hdr[256];
    int hlen = snprintf(hdr, sizeof(hdr),
        "HTTP/1.1 %d %s\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "Content-Length: %d\r\n"
        "Connection: close\r\n\r\n",
        status, msg, blen);
    send(cli, hdr, hlen, 0);
    if (json) send(cli, json, blen, 0);
}

static void handle_client(SOCKET cli) {
    char *buf = (char *)malloc(MAX_REQ_BYTES + 1);
    if (!buf) { closesocket(cli); return; }
    int total = 0;
    int hdr_end = -1;
    int content_len = 0;

    DWORD tv = 5000;
    setsockopt(cli, SOL_SOCKET, SO_RCVTIMEO, (const char *)&tv, sizeof(tv));

    while (total < MAX_REQ_BYTES) {
        int r = recv(cli, buf + total, MAX_REQ_BYTES - total, 0);
        if (r <= 0) break;
        total += r;
        buf[total] = '\0';
        if (hdr_end < 0) {
            char *p = strstr(buf, "\r\n\r\n");
            if (p) {
                hdr_end = (int)(p - buf) + 4;
                const char *cl = strstr(buf, "Content-Length:");
                if (!cl) cl = strstr(buf, "content-length:");
                if (cl && cl < p) content_len = atoi(cl + 15);
                if (content_len < 0) content_len = 0;
                if (total >= hdr_end + content_len) break;
            }
        } else if (total >= hdr_end + content_len) {
            break;
        }
    }

    if (hdr_end < 0) {
        send_http_json(cli, 400, "{\"ok\":false,\"error\":\"bad request\"}");
        free(buf);
        closesocket(cli);
        return;
    }

    char method[16] = {0}, path[128] = {0};
    sscanf(buf, "%15s %127s", method, path);
    const char *body = buf + hdr_end;

    char *resp = NULL;
    int status = 200;
    if (strcmp(method, "GET") == 0 && strcmp(path, "/probe") == 0) {
        resp = handle_probe();
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/open") == 0) {
        resp = handle_open(body);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/send") == 0) {
        resp = handle_send(body);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/send-file") == 0) {
        resp = handle_send_file(body);
    } else if (strcmp(method, "POST") == 0 && strcmp(path, "/send-emoticon") == 0) {
        resp = handle_send_emoticon(body);
    } else {
        status = 404;
        resp = _strdup("{\"ok\":false,\"error\":\"not found\"}");
    }

    send_http_json(cli, status, resp);
    free(resp);
    free(buf);
    closesocket(cli);
}

static DWORD WINAPI client_thread(LPVOID param) {
    SOCKET cli = (SOCKET)(ULONG_PTR)param;
    handle_client(cli);
    return 0;
}

int wmain(void) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        fprintf(stderr, "WSAStartup failed\n");
        return 1;
    }
    InitializeCriticalSection(&g_action_lock);
    HMODULE u32 = GetModuleHandleW(L"user32.dll");
    if (u32) g_GetDpiForWindow = (PFN_GetDpiForWindow)GetProcAddress(u32, "GetDpiForWindow");

    SOCKET srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == INVALID_SOCKET) {
        fprintf(stderr, "socket() failed: %d\n", WSAGetLastError());
        return 1;
    }
    int yes = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, (const char *)&yes, sizeof(yes));

    struct sockaddr_in addr;
    ZeroMemory(&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(BRIDGE_PORT);

    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) == SOCKET_ERROR) {
        fprintf(stderr, "bind(127.0.0.1:%d) failed: %d\n", BRIDGE_PORT, WSAGetLastError());
        closesocket(srv);
        return 1;
    }
    if (listen(srv, 16) == SOCKET_ERROR) {
        fprintf(stderr, "listen() failed: %d\n", WSAGetLastError());
        closesocket(srv);
        return 1;
    }

    fprintf(stderr, "kakao-bridge listening on 127.0.0.1:%d\n", BRIDGE_PORT);
    fflush(stderr);

    for (;;) {
        SOCKET cli = accept(srv, NULL, NULL);
        if (cli == INVALID_SOCKET) {
            Sleep(50);
            continue;
        }
        HANDLE th = CreateThread(NULL, 0, client_thread, (LPVOID)(ULONG_PTR)cli, 0, NULL);
        if (th) CloseHandle(th);
        else handle_client(cli);
    }
    return 0;
}
