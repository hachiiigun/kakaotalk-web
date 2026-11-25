'use strict';

const state = {
  nav: 'chats',
  search: '',
  rooms: [],
  friends: [],
  tabCache: {},
  activeRoomId: null,
  activeRoom: null,
  messages: [],
  hasMore: false,
  inRoomSearch: '',
  sending: false,
  theme: localStorage.getItem('kakao_webui_theme') || 'light',
  emoticons: {
    loaded: false,
    loading: false,
    open: false,
    available: false,
    packs: [],
    recent: [],
    activeTab: 'recent',
    search: '',
    selected: null,
    sending: false,
  },
};
let sidebarReqSeq = 0;
let sidebarAbort = null;
let msgReqSeq = 0;
let msgAbort = null;

const el = (id) => document.getElementById(id);
const appEl = el('app');
const sidebarTitle = el('sidebar-title');
const sidebarList = el('sidebar-list');
const sidebarSearch = el('sidebar-search');
const statusPill = el('status-pill');
const emptyStage = el('empty-stage');
const emptySummary = el('empty-summary');
const chatActive = el('chat-active');
const roomTitle = el('room-title');
const roomMembers = el('room-members');
const roomSub = el('room-sub');
const messagesWrap = el('messages-wrap');
const messagesList = el('messages-list');
const btnLoadMore = el('btn-load-more');
const lockedOverlay = el('locked-overlay');
const unlockSpinner = el('unlock-spinner');
const lockedTitle = el('locked-title');
const lockedDesc = el('locked-desc');
const btnUnlockRoom = el('btn-unlock-room');
const btnJumpBottom = el('btn-jump-bottom');
const composer = el('composer');
const readonlyBanner = el('readonly-banner');
const msgInput = el('msg-input');
const btnSend = el('btn-send');
const fileInput = el('file-input');
const btnEmoticon = el('btn-emoticon');
const emoticonPicker = el('emoticon-picker');
const emoticonSearch = el('emoticon-search');
const emoticonTabs = el('emoticon-tabs');
const emoticonGrid = el('emoticon-grid');
const emoticonPreview = el('emoticon-preview');
const emoticonPreviewImg = el('emoticon-preview-img');
const emoticonPreviewTitle = el('emoticon-preview-title');
const btnEmoticonSend = el('btn-emoticon-send');
const btnEmoticonClose = el('btn-emoticon-close');

const RECENT_EMOTICON_STORAGE_KEY = 'kakao_webui_recent_emoticons';
const UNICODE_EMOJIS = [
  { ch: '😀', kw: '웃음 미소 스마일' },
  { ch: '😄', kw: '웃음 하하 기쁨' },
  { ch: '😁', kw: '웃음 이모지 치즈' },
  { ch: '😂', kw: '웃음 ㅋㅋ 눈물 빵터짐' },
  { ch: '🤣', kw: 'ㅋㅋㅋ 웃음 폭소' },
  { ch: '😊', kw: '미소 웃음 부끄' },
  { ch: '😍', kw: '사랑 하트 좋아요 반함' },
  { ch: '🥰', kw: '사랑 하트 행복' },
  { ch: '😘', kw: '뽀뽀 키스 사랑 하트' },
  { ch: '😎', kw: '선글라스 멋짐 쿨' },
  { ch: '🤩', kw: '감탄 별 최고' },
  { ch: '🥳', kw: '축하 파티 생일' },
  { ch: '😌', kw: '안도 편안 휴식' },
  { ch: '😉', kw: '윙크 장난' },
  { ch: '😋', kw: '맛있음 냠냠 메롱' },
  { ch: '😜', kw: '메롱 장난 윙크' },
  { ch: '🤔', kw: '생각 고민 물음' },
  { ch: '🤨', kw: '의심 흠 글쎄' },
  { ch: '😐', kw: '무표정 멍' },
  { ch: '😑', kw: '무표정 황당' },
  { ch: '🙄', kw: '한심 절레절레' },
  { ch: '😏', kw: '씨익 능글' },
  { ch: '😅', kw: '땀 머쓱 민망' },
  { ch: '😓', kw: '땀 힘듦 피곤' },
  { ch: '😥', kw: '아쉬움 땀 슬픔' },
  { ch: '😢', kw: '울음 슬픔 눈물' },
  { ch: '😭', kw: '오열 울음 ㅠㅠ 눈물' },
  { ch: '😱', kw: '놀람 충격 비명' },
  { ch: '😲', kw: '깜짝 놀람' },
  { ch: '😳', kw: '당황 부끄 놀람' },
  { ch: '🥺', kw: '부탁 애원 ㅠㅠ' },
  { ch: '😤', kw: '화남 씩씩 짜증' },
  { ch: '😡', kw: '화남 분노 열받음' },
  { ch: '🤯', kw: '멘붕 충격 폭발' },
  { ch: '😴', kw: '졸림 잠 쿨쿨 피곤' },
  { ch: '🥱', kw: '하품 졸림 피곤' },
  { ch: '🤒', kw: '아픔 감기 병' },
  { ch: '🤧', kw: '재채기 감기 코' },
  { ch: '😇', kw: '천사 착함 감사' },
  { ch: '🫡', kw: '경례 충성 알겠습니다 확인' },
  { ch: '🫶', kw: '손하트 사랑 감사' },
  { ch: '👍', kw: '좋아요 최고 엄지 확인 굿' },
  { ch: '👎', kw: '싫어요 별로' },
  { ch: '👏', kw: '박수 축하 짝짝짝' },
  { ch: '🙌', kw: '만세 환영 축하' },
  { ch: '🙏', kw: '감사 부탁 죄송 기도 제발' },
  { ch: '🤝', kw: '악수 합의 인사' },
  { ch: '👋', kw: '안녕 인사 하이 바이' },
  { ch: '💪', kw: '파이팅 힘 근육 응원' },
  { ch: '👌', kw: 'OK 오케이 확인' },
  { ch: '✌️', kw: '브이 평화 사진' },
  { ch: '🤞', kw: '행운 빌어요' },
  { ch: '❤️', kw: '하트 사랑 빨간하트' },
  { ch: '💛', kw: '하트 노란하트 카톡' },
  { ch: '💙', kw: '하트 파란하트' },
  { ch: '🔥', kw: '불 열정 파이팅 대박' },
  { ch: '✨', kw: '반짝 별 최고 축하' },
  { ch: '⭐', kw: '별 최고 중요' },
  { ch: '💯', kw: '백점 완벽 최고' },
  { ch: '✅', kw: '체크 확인 완료' },
  { ch: '❌', kw: '엑스 취소 아님' },
  { ch: '❗', kw: '느낌표 중요' },
  { ch: '❓', kw: '물음표 질문' },
  { ch: '🎉', kw: '축하 파티 폭죽' },
  { ch: '🎁', kw: '선물 축하' },
  { ch: '🎂', kw: '케이크 생일 축하' },
  { ch: '☕', kw: '커피 카페 휴식' },
  { ch: '🍺', kw: '맥주 술 회식 건배' },
  { ch: '🍚', kw: '밥 식사 점심 저녁' },
];

const NAV_TITLES = {
  chats: '채팅',
  open: '오픈채팅',
  channels: '채널',
  friends: '친구',
};

function applyTheme(t) {
  state.theme = t;
  document.documentElement.setAttribute('data-theme', t === 'dark' ? 'dark' : 'light');
  localStorage.setItem('kakao_webui_theme', state.theme);
}
applyTheme(state.theme);

function toast(msg) {
  const host = el('toast-host');
  const d = document.createElement('div');
  d.className = 'toast';
  d.textContent = msg;
  host.appendChild(d);
  setTimeout(() => d.remove(), 3500);
}

async function apiJson(url, opts = {}) {
  const { timeoutMs = 15000, signal: extSignal, ...rest } = opts;
  const ctrl = new AbortController();
  let onExtAbort = null;
  if (extSignal) {
    if (extSignal.aborted) {
      ctrl.abort();
    } else {
      onExtAbort = () => ctrl.abort();
      extSignal.addEventListener('abort', onExtAbort, { once: true });
    }
  }
  const timer = timeoutMs > 0 ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  try {
    const r = await fetch(url, { ...rest, signal: ctrl.signal });
    if (r.status === 401) {
      location.reload();
      throw new Error('unauthorized');
    }
    return await r.json();
  } finally {
    if (timer) clearTimeout(timer);
    if (extSignal && onExtAbort) extSignal.removeEventListener('abort', onExtAbort);
  }
}

function colorFor(seed) {
  const palette = ['#5c7cfa', '#20c997', '#ff922b', '#845ef7', '#339af0', '#f06595', '#51cf66'];
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
  return palette[h % palette.length];
}

function buildAvatar(avatars, name, small = false) {
  if (avatars && avatars.length > 1) {
    const g = document.createElement('div');
    g.className = 'avatar-grid';
    avatars.slice(0, 4).forEach((src) => {
      const img = document.createElement('img');
      img.src = src;
      img.alt = '';
      img.loading = 'lazy';
      g.appendChild(img);
    });
    return g;
  }
  const d = document.createElement('div');
  d.className = small ? 'avatar sm' : 'avatar';
  d.style.background = colorFor(name || '?');
  if (avatars && avatars.length === 1) {
    const img = document.createElement('img');
    img.src = avatars[0];
    img.alt = '';
    img.loading = 'lazy';
    img.onerror = () => {
      img.remove();
      d.textContent = (name || '?').slice(0, 1);
    };
    d.appendChild(img);
  } else {
    d.textContent = (name || '?').slice(0, 1);
  }
  return d;
}

function fmtShortTime(epochSec) {
  if (!epochSec) return '';
  const d = new Date(epochSec * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString('ko-KR', { hour: 'numeric', minute: '2-digit' });
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  if (d.toDateString() === yest.toDateString()) return '어제';
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}월 ${d.getDate()}일`;
  return `${d.getFullYear()}.${d.getMonth() + 1}.${d.getDate()}`;
}

function fmtMsgTime(epochSec) {
  if (!epochSec) return '';
  return new Date(epochSec * 1000).toLocaleTimeString('ko-KR', { hour: 'numeric', minute: '2-digit' });
}

function fmtDateDivider(epochSec) {
  return new Date(epochSec * 1000).toLocaleDateString('ko-KR', {
    year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
  });
}

function fmtBytes(n) {
  if (!n) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function appendLinkifiedText(parent, text) {
  const re = /(https?:\/\/[^\s<>'"]+)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    const a = document.createElement('a');
    a.href = m[1];
    a.textContent = m[1];
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    parent.appendChild(a);
    last = m.index + m[1].length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

function renderSidebar() {
  sidebarTitle.textContent = NAV_TITLES[state.nav] || '채팅';
  sidebarList.replaceChildren();

  if (state.nav === 'friends') {
    sidebarSearch.placeholder = '친구 이름, 상태메시지 검색';
    state.friends.forEach((f) => {
      const row = document.createElement('div');
      row.className = 'friend-item';
      row.appendChild(buildAvatar(f.avatar ? [f.avatar] : [], f.name));
      const meta = document.createElement('div');
      meta.className = 'room-meta';
      const top = document.createElement('div');
      top.className = 'room-top';
      const nm = document.createElement('span');
      nm.className = 'room-name';
      nm.textContent = f.name;
      top.appendChild(nm);
      meta.appendChild(top);
      if (f.status) {
        const sub = document.createElement('div');
        sub.className = 'room-preview';
        sub.textContent = f.status;
        meta.appendChild(sub);
      }
      row.appendChild(meta);
      row.addEventListener('click', () => {
        if (f.direct_chat_id) {
          state.nav = 'chats';
          updateNavButtons();
          selectRoom(f.direct_chat_id);
        } else {
          toast('1:1 채팅방이 아직 열려 있지 않습니다.');
        }
      });
      sidebarList.appendChild(row);
    });
    return;
  }

  sidebarSearch.placeholder = '채팅방 이름, 메시지 검색';
  if (!state.rooms.length) {
    const empty = document.createElement('div');
    empty.style.cssText = 'padding:24px;text-align:center;font-size:13px;color:var(--muted)';
    empty.textContent = '표시할 채팅방이 없습니다.';
    sidebarList.appendChild(empty);
    return;
  }

  state.rooms.forEach((r) => {
    const row = document.createElement('div');
    row.className = 'room-item' + (r.id === state.activeRoomId ? ' active' : '');
    row.appendChild(buildAvatar(r.avatars, r.name));

    const meta = document.createElement('div');
    meta.className = 'room-meta';

    const top = document.createElement('div');
    top.className = 'room-top';
    const nw = document.createElement('div');
    nw.className = 'room-name-wrap';
    const nm = document.createElement('span');
    nm.className = 'room-name';
    nm.textContent = (r.favorite ? '★ ' : '') + r.name;
    nw.appendChild(nm);
    if (r.member_count > 2) {
      const cnt = document.createElement('span');
      cnt.className = 'room-cnt';
      cnt.textContent = String(r.member_count);
      nw.appendChild(cnt);
    }
    if (!r.unlocked) {
      const lk = document.createElement('span');
      lk.className = 'room-cnt';
      lk.title = '잠긴 방(클릭 시 키 탐색)';
      lk.textContent = '🔒';
      nw.appendChild(lk);
    }
    const tm = document.createElement('span');
    tm.className = 'room-time';
    tm.textContent = fmtShortTime(r.last_at);
    top.append(nw, tm);

    const bot = document.createElement('div');
    bot.className = 'room-bottom';
    const pv = document.createElement('span');
    pv.className = 'room-preview';
    pv.textContent = r.last_message || '';
    bot.appendChild(pv);
    if (r.unread_count > 0) {
      const ub = document.createElement('span');
      ub.className = 'unread-badge';
      ub.textContent = r.unread_count > 99 ? '99+' : String(r.unread_count);
      bot.appendChild(ub);
    }

    meta.append(top, bot);
    row.appendChild(meta);
    row.addEventListener('click', () => selectRoom(r.id));
    sidebarList.appendChild(row);
  });
}

function renderMessages(preserveScrollFromBottom = false) {
  const prevBottom = messagesWrap.scrollHeight - messagesWrap.scrollTop;
  messagesList.replaceChildren();
  btnLoadMore.classList.toggle('hidden', !state.hasMore);

  let lastDate = '';
  let prevAuthor = null;
  let prevMinute = 0;

  state.messages.forEach((m) => {
    const dayStr = fmtDateDivider(m.at);
    if (dayStr !== lastDate) {
      lastDate = dayStr;
      prevAuthor = null;
      const div = document.createElement('div');
      div.className = 'date-divider';
      div.textContent = dayStr;
      messagesList.appendChild(div);
    }

    if (m.kind === 'system') {
      prevAuthor = null;
      const sys = document.createElement('div');
      sys.className = 'sys-msg';
      sys.textContent = m.text;
      messagesList.appendChild(sys);
      return;
    }

    const minuteBucket = Math.floor(m.at / 60);
    const firstInGroup = m.author_id !== prevAuthor || minuteBucket !== prevMinute;
    prevAuthor = m.author_id;
    prevMinute = minuteBucket;

    const row = document.createElement('div');
    row.className = 'msg-row'
      + (m.mine ? ' mine' : '')
      + (firstInGroup ? ' first-in-group' : '')
      + (m.deleted ? ' deleted' : '');

    if (!m.mine) {
      const avSlot = document.createElement('div');
      avSlot.className = 'msg-avatar-slot';
      if (firstInGroup) {
        avSlot.appendChild(buildAvatar(m.avatar ? [m.avatar] : [], m.sender, true));
      }
      row.appendChild(avSlot);
    }

    const col = document.createElement('div');
    col.className = 'msg-body-col';
    if (!m.mine && firstInGroup) {
      const sn = document.createElement('div');
      sn.className = 'msg-sender';
      sn.textContent = m.sender;
      col.appendChild(sn);
    }

    const line = document.createElement('div');
    line.className = 'bubble-line';

    const isEmoticonBubble = m.kind === 'emoticon' && m.media && m.media.length > 0;
    if (isEmoticonBubble) {
      row.classList.add('emoticon-only');
    }

    const bubble = document.createElement('div');
    bubble.className = 'bubble';

    if (m.reply_to) {
      const rq = document.createElement('div');
      rq.className = 'reply-quote';
      const rqs = document.createElement('strong');
      rqs.textContent = `${m.reply_to.sender}에게 답장`;
      const rqt = document.createElement('span');
      rqt.textContent = m.reply_to.text;
      rq.append(rqs, rqt);
      bubble.appendChild(rq);
    }

    if (isEmoticonBubble) {
      const item = m.media[0];
      const img = document.createElement('img');
      img.className = 'emoticon-bubble-img';
      img.src = item.url || item.thumb;
      img.alt = m.text || '이모티콘';
      img.loading = 'lazy';
      img.addEventListener('click', () => openLightbox(item.url || item.thumb));
      img.onerror = () => {
        if (item.thumb && item.thumb !== img.src && img.dataset.fbTried !== '1') {
          img.dataset.fbTried = '1';
          img.src = item.thumb;
          return;
        }
        img.remove();
        row.classList.remove('emoticon-only');
        const fallback = document.createElement('span');
        fallback.textContent = m.text || '[이모티콘]';
        bubble.appendChild(fallback);
      };
      bubble.appendChild(img);
    } else if (m.media && m.media.length > 0) {
      const grid = document.createElement('div');
      grid.className = 'media-grid' + (m.media.length === 1 ? ' single' : '');
      m.media.forEach((item) => {
        if (!item.thumb && !item.url) return;
        const img = document.createElement('img');
        img.className = 'media-thumb';
        img.src = item.thumb || item.url;
        img.alt = '사진';
        img.loading = 'lazy';
        img.addEventListener('click', () => openLightbox(item.url || item.thumb));
        grid.appendChild(img);
      });
      bubble.appendChild(grid);
    }

    if (m.file) {
      const fc = document.createElement('a');
      fc.className = 'file-card';
      fc.href = m.file.url || '#';
      if (!m.file.url) fc.addEventListener('click', (e) => { e.preventDefault(); toast('만료되었거나 URL이 없는 파일입니다.'); });
      else { fc.target = '_blank'; fc.rel = 'noopener noreferrer'; }
      const icon = document.createElement('span');
      icon.textContent = '📎';
      const info = document.createElement('div');
      const fn = document.createElement('div');
      fn.className = 'file-card-name';
      fn.textContent = m.file.name;
      const fs = document.createElement('div');
      fs.className = 'file-card-size';
      fs.textContent = fmtBytes(m.file.size);
      info.append(fn, fs);
      fc.append(icon, info);
      bubble.appendChild(fc);
    } else if (m.text && !(isEmoticonBubble && (m.text === '(이모티콘)' || m.text === '[이모티콘]'))) {
      const txtEl = document.createElement('span');
      appendLinkifiedText(txtEl, m.text);
      bubble.appendChild(txtEl);
    }

    if (m.card && m.card.url) {
      const rc = document.createElement('div');
      rc.className = 'rich-card';
      if (m.card.title) {
        const rt = document.createElement('div');
        rt.className = 'rich-card-title';
        rt.textContent = m.card.title;
        rc.appendChild(rt);
      }
      if (m.card.description) {
        const rd = document.createElement('div');
        rd.textContent = m.card.description;
        rc.appendChild(rd);
      }
      const ra = document.createElement('a');
      ra.href = m.card.url;
      ra.target = '_blank';
      ra.rel = 'noopener noreferrer';
      ra.textContent = '링크 열기';
      rc.appendChild(ra);
      bubble.appendChild(rc);
    }

    const meta = document.createElement('div');
    meta.className = 'msg-meta';
    if (m.unread > 0) {
      const u = document.createElement('span');
      u.className = 'msg-unread';
      u.textContent = String(m.unread);
      meta.appendChild(u);
    }
    const tm = document.createElement('span');
    tm.className = 'msg-time';
    tm.textContent = fmtMsgTime(m.at);
    meta.appendChild(tm);

    line.append(bubble, meta);
    col.appendChild(line);
    row.appendChild(col);
    messagesList.appendChild(row);
  });

  if (preserveScrollFromBottom) {
    messagesWrap.scrollTop = Math.max(0, messagesWrap.scrollHeight - prevBottom);
  } else {
    messagesWrap.scrollTop = messagesWrap.scrollHeight;
  }
}

function openLightbox(src) {
  el('lightbox-img').src = src;
  el('lightbox-dl').href = src;
  el('lightbox').classList.remove('hidden');
}

async function loadStatus() {
  try {
    const st = await apiJson('/api/status');
    const voxlUrl = `${location.protocol}//${location.hostname}:${st.voxl_port || 14500}/`;
    el('btn-voxl').href = voxlUrl;
    el('btn-open-voxl').href = voxlUrl;
    if (st.kakaotalk_running) {
      statusPill.textContent = `연결됨 (${st.rooms_unlocked}/${st.rooms_total})`;
      statusPill.className = 'status-pill live';
    } else {
      statusPill.textContent = '카톡 미실행';
      statusPill.className = 'status-pill err';
    }
    emptySummary.textContent = `전체 채팅방 ${st.rooms_total}개 · 읽기 가능 ${st.rooms_unlocked}개 · 친구 ${st.friends_count}명`;
  } catch (e) {
    statusPill.textContent = '오류';
    statusPill.className = 'status-pill err';
  }
}

async function loadSidebar() {
  if (sidebarAbort) {
    try { sidebarAbort.abort(); } catch (_) {}
  }
  const ctrl = new AbortController();
  sidebarAbort = ctrl;
  const seq = ++sidebarReqSeq;
  const targetNav = state.nav;
  const targetSearch = state.search;
  try {
    if (targetNav === 'friends') {
      const data = await apiJson(`/api/friends?q=${encodeURIComponent(targetSearch)}`, { signal: ctrl.signal });
      if (seq !== sidebarReqSeq || targetNav !== state.nav) return;
      state.friends = data.friends || [];
      if (!targetSearch) state.tabCache[targetNav] = state.friends;
    } else {
      const data = await apiJson(`/api/rooms?tab=${encodeURIComponent(targetNav)}&q=${encodeURIComponent(targetSearch)}`, { signal: ctrl.signal });
      if (seq !== sidebarReqSeq || targetNav !== state.nav) return;
      state.rooms = data.rooms || [];
      if (!targetSearch) state.tabCache[targetNav] = state.rooms;
    }
    renderSidebar();
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (seq !== sidebarReqSeq) return;
    sidebarList.replaceChildren();
    const errBox = document.createElement('div');
    errBox.style.cssText = 'padding:24px;text-align:center;font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:10px;align-items:center;';
    const msg = document.createElement('div');
    msg.textContent = '목록을 불러오지 못했습니다.';
    const retry = document.createElement('button');
    retry.className = 'mini-btn';
    retry.textContent = '다시 시도';
    retry.addEventListener('click', () => loadSidebar());
    errBox.append(msg, retry);
    sidebarList.appendChild(errBox);
  }
}

async function selectRoom(chatId, autoUnlock = true) {
  closeEmoticonPicker(false);
  state.activeRoomId = chatId;
  state.inRoomSearch = '';
  el('inroom-search-input').value = '';
  el('inroom-search-bar').classList.add('hidden');
  appEl.classList.add('mobile-chat-open');
  emptyStage.classList.add('hidden');
  chatActive.classList.remove('hidden');
  btnJumpBottom.classList.add('hidden');
  renderSidebar();

  await fetchRoomMessages(chatId, { scrollBottom: true, autoUnlock });
}

async function fetchRoomMessages(chatId, { scrollBottom = false, prepend = false, autoUnlock = false } = {}) {
  if (!prepend && msgAbort) {
    try { msgAbort.abort(); } catch (_) {}
  }
  const ctrl = new AbortController();
  if (!prepend) msgAbort = ctrl;
  const seq = ++msgReqSeq;

  const before = prepend && state.messages.length ? state.messages[0].log_id : 0;
  const url = `/api/rooms/${chatId}/messages?limit=80&before_log_id=${before}&q=${encodeURIComponent(state.inRoomSearch)}`;
  let data;
  try {
    data = await apiJson(url, { signal: ctrl.signal });
  } catch (e) {
    if (e && e.name === 'AbortError') return;
    if (seq !== msgReqSeq || state.activeRoomId !== chatId) return;
    toast('대화 내용을 불러오지 못했습니다.');
    return;
  }
  if ((!prepend && seq !== msgReqSeq) || state.activeRoomId !== chatId) return;

  const room = data.room;
  state.activeRoom = room;
  roomTitle.textContent = room.name;
  roomMembers.textContent = room.member_count > 1 ? `(${room.member_count})` : '';
  roomSub.textContent = room.type_label + (data.unlocked ? ` · 메시지 ${data.total || 0}개` : ' · 잠금 상태');

  composer.classList.toggle('hidden', !room.writable);
  readonlyBanner.classList.toggle('hidden', room.writable);

  if (!data.unlocked) {
    state.messages = [];
    state.hasMore = false;
    renderMessages();
    lockedOverlay.classList.remove('hidden');
    if (autoUnlock) {
      await unlockRoom(chatId);
    }
    return;
  }

  lockedOverlay.classList.add('hidden');
  const nearBottom = messagesWrap.scrollHeight - messagesWrap.scrollTop - messagesWrap.clientHeight < 100;

  if (prepend) {
    state.messages = [...(data.messages || []), ...state.messages];
    state.hasMore = Boolean(data.has_more);
    renderMessages(true);
  } else {
    state.messages = data.messages || [];
    state.hasMore = Boolean(data.has_more);
    renderMessages(!(scrollBottom || nearBottom));
    if (!scrollBottom && !nearBottom) {
      btnJumpBottom.classList.remove('hidden');
    }
  }
}

async function unlockRoom(chatId) {
  unlockSpinner.classList.remove('hidden');
  btnUnlockRoom.disabled = true;
  lockedTitle.textContent = '채팅방 DB 여는 중...';
  lockedDesc.textContent = 'PC 카카오톡에서 해당 채팅방을 열어 키를 가져오고 있습니다.';
  try {
    const res = await apiJson(`/api/rooms/${chatId}/unlock`, { method: 'POST' });
    if (state.activeRoomId !== chatId) return;
    if (res.unlocked) {
      lockedOverlay.classList.add('hidden');
      await fetchRoomMessages(chatId, { scrollBottom: true, autoUnlock: false });
      await Promise.all([loadSidebar(), loadStatus()]);
    } else {
      lockedTitle.textContent = '대화 DB 키 확인 필요';
      lockedDesc.textContent = res.error || '원격 화면(:14500)에서 이 채팅방을 한 번 연 뒤 다시 시도해 주세요.';
    }
  } catch (e) {
    lockedTitle.textContent = '잠금 해제 실패';
    lockedDesc.textContent = '네트워크 또는 브리지 오류가 발생했습니다.';
  } finally {
    unlockSpinner.classList.add('hidden');
    btnUnlockRoom.disabled = false;
  }
}

async function sendMessage() {
  if (state.sending || !state.activeRoomId) return;
  const text = msgInput.value;
  if (!text.trim()) return;
  state.sending = true;
  btnSend.disabled = true;

  try {
    const res = await apiJson(`/api/rooms/${state.activeRoomId}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      toast(res.error || '메시지 전송에 실패했습니다.');
      return;
    }
    msgInput.value = '';
    msgInput.style.height = 'auto';
    setTimeout(() => {
      if (state.activeRoomId) fetchRoomMessages(state.activeRoomId, { scrollBottom: true });
      loadSidebar();
    }, 350);
  } catch (e) {
    toast('전송 요청 중 오류가 발생했습니다.');
  } finally {
    state.sending = false;
    btnSend.disabled = !msgInput.value.trim();
  }
}

async function uploadFile(file) {
  if (!file || !state.activeRoomId) return;
  if (file.size > 25 * 1024 * 1024) {
    toast('파일 크기는 25 MB 이하만 전송할 수 있습니다.');
    return;
  }
  toast(`전송 중: ${file.name}`);
  try {
    const res = await apiJson(`/api/rooms/${state.activeRoomId}/upload`, {
      method: 'POST',
      headers: { 'X-Filename': encodeURIComponent(file.name) },
      body: file,
    });
    if (res.ok) {
      toast(`파일 전송 완료: ${file.name}`);
      setTimeout(() => {
        if (state.activeRoomId) fetchRoomMessages(state.activeRoomId, { scrollBottom: true });
      }, 600);
    } else {
      toast(res.error || '파일 전송에 실패했습니다.');
    }
  } catch (e) {
    toast('파일 업로드 중 오류가 발생했습니다.');
  }
}

function updateNavButtons() {
  document.querySelectorAll('.rail-btn[data-nav]').forEach((b) => {
    b.classList.toggle('active', b.dataset.nav === state.nav);
  });
}

/* Event wiring */
document.querySelectorAll('.rail-btn[data-nav]').forEach((btn) => {
  btn.addEventListener('click', () => {
    state.nav = btn.dataset.nav;
    updateNavButtons();
    sidebarTitle.textContent = NAV_TITLES[state.nav] || '채팅';
    if (!state.search && state.tabCache[state.nav]) {
      if (state.nav === 'friends') state.friends = state.tabCache[state.nav];
      else state.rooms = state.tabCache[state.nav];
      renderSidebar();
    } else {
      sidebarList.replaceChildren();
      const loading = document.createElement('div');
      loading.style.cssText = 'padding:24px;text-align:center;font-size:13px;color:var(--muted)';
      loading.textContent = '불러오는 중...';
      sidebarList.appendChild(loading);
    }
    loadSidebar();
  });
});

let searchTimer = null;
sidebarSearch.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.search = sidebarSearch.value;
    loadSidebar();
  }, 150);
});

el('btn-scan').addEventListener('click', async () => {
  toast('메모리 키 스캔 중...');
  const res = await apiJson('/api/scan', { method: 'POST' });
  toast(`새로 열린 채팅방 ${res.newly_unlocked || 0}개 (총 ${res.rooms_unlocked}/${res.rooms_total})`);
  await Promise.all([loadSidebar(), loadStatus()]);
  if (state.activeRoomId) await fetchRoomMessages(state.activeRoomId);
});

el('btn-theme').addEventListener('click', () => applyTheme(state.theme === 'dark' ? 'light' : 'dark'));
el('btn-logout').addEventListener('click', async () => {
  await apiJson('/api/logout', { method: 'POST' });
  location.reload();
});
el('btn-back').addEventListener('click', () => appEl.classList.remove('mobile-chat-open'));
el('btn-refresh-room').addEventListener('click', () => {
  if (state.activeRoomId) fetchRoomMessages(state.activeRoomId);
});
el('btn-inroom-search').addEventListener('click', () => {
  el('inroom-search-bar').classList.toggle('hidden');
  el('inroom-search-input').focus();
});
el('inroom-search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && state.activeRoomId) {
    state.inRoomSearch = e.target.value;
    fetchRoomMessages(state.activeRoomId, { scrollBottom: true });
  }
});
el('inroom-search-clear').addEventListener('click', () => {
  state.inRoomSearch = '';
  el('inroom-search-input').value = '';
  el('inroom-search-bar').classList.add('hidden');
  if (state.activeRoomId) fetchRoomMessages(state.activeRoomId, { scrollBottom: true });
});

btnLoadMore.addEventListener('click', () => {
  if (state.activeRoomId) fetchRoomMessages(state.activeRoomId, { prepend: true });
});
btnUnlockRoom.addEventListener('click', () => {
  if (state.activeRoomId) unlockRoom(state.activeRoomId);
});
btnJumpBottom.addEventListener('click', () => {
  messagesWrap.scrollTop = messagesWrap.scrollHeight;
  btnJumpBottom.classList.add('hidden');
});
messagesWrap.addEventListener('scroll', () => {
  const nearBottom = messagesWrap.scrollHeight - messagesWrap.scrollTop - messagesWrap.clientHeight < 60;
  if (nearBottom) btnJumpBottom.classList.add('hidden');
});

msgInput.addEventListener('input', () => {
  btnSend.disabled = state.sending || !msgInput.value.trim();
  msgInput.style.height = 'auto';
  msgInput.style.height = Math.min(msgInput.scrollHeight, 140) + 'px';
});
msgInput.addEventListener('keydown', (e) => {
  if (e.isComposing || e.keyCode === 229) return;
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
btnSend.addEventListener('click', sendMessage);

el('btn-attach').addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  if (fileInput.files && fileInput.files[0]) {
    uploadFile(fileInput.files[0]);
    fileInput.value = '';
  }
});

/* Emoticon Picker (Ctrl+E) */
function readLocalRecentEmoticons() {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_EMOTICON_STORAGE_KEY) || '[]');
    return Array.isArray(raw) ? raw : [];
  } catch (_) {
    return [];
  }
}

function saveLocalRecentEmoticon(item) {
  if (!item || !item.item_code || !item.send_file) return;
  const cur = readLocalRecentEmoticons().filter(
    (x) => !(x.item_code === item.item_code && x.thumb_file === item.thumb_file)
  );
  cur.unshift(item);
  const sliced = cur.slice(0, 24);
  try {
    localStorage.setItem(RECENT_EMOTICON_STORAGE_KEY, JSON.stringify(sliced));
  } catch (_) {}
  mergeRecentEmoticons(state.emoticons.recent);
}

function mergeRecentEmoticons(serverRecent) {
  const local = readLocalRecentEmoticons();
  const seen = new Set();
  const merged = [];
  for (const it of [...local, ...(serverRecent || [])]) {
    if (!it || !it.item_code || !it.thumb_file) continue;
    const key = `${it.item_code}:${it.thumb_file}`;
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(it);
    if (merged.length >= 36) break;
  }
  state.emoticons.recent = merged;
}

function selectEmoticonItem(item) {
  state.emoticons.selected = item || null;
  if (!item) {
    emoticonPreviewImg.onerror = null;
    emoticonPreview.classList.add('hidden');
  } else {
    const playSrc = item.play_url || item.thumb_url;
    emoticonPreviewImg.onerror = () => {
      if (item.thumb_url && emoticonPreviewImg.src !== item.thumb_url) {
        emoticonPreviewImg.onerror = null;
        emoticonPreviewImg.src = item.thumb_url;
      }
    };
    emoticonPreviewImg.src = playSrc;
    const kwText = (item.keywords || []).slice(0, 4).join(', ');
    emoticonPreviewTitle.textContent = item.label || kwText || '카카오 이모티콘';
    emoticonPreview.classList.remove('hidden');
  }
  emoticonGrid.querySelectorAll('.emoticon-cell[data-key]').forEach((btn) => {
    const selectedKey = item ? `${item.item_code}:${item.thumb_file}` : '';
    btn.classList.toggle('selected', btn.dataset.key === selectedKey);
  });
}

function insertUnicodeEmoji(ch) {
  const start = typeof msgInput.selectionStart === 'number' ? msgInput.selectionStart : msgInput.value.length;
  const end = typeof msgInput.selectionEnd === 'number' ? msgInput.selectionEnd : msgInput.value.length;
  const val = msgInput.value;
  msgInput.value = val.slice(0, start) + ch + val.slice(end);
  const nextPos = start + ch.length;
  msgInput.setSelectionRange(nextPos, nextPos);
  msgInput.dispatchEvent(new Event('input'));
  msgInput.focus();
}

async function sendSelectedEmoticon(item) {
  if (!item || !state.activeRoomId || state.emoticons.sending) return;
  state.emoticons.sending = true;
  btnEmoticonSend.disabled = true;
  try {
    const res = await apiJson(`/api/rooms/${state.activeRoomId}/send-emoticon`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        item_code: item.item_code,
        filename: item.send_file || item.thumb_file,
        kind: item.kind,
        pack_pos: item.pack_pos,
        grid_idx: item.grid_idx,
      }),
    });
    if (!res.ok) {
      toast(res.error || '이모티콘 전송에 실패했습니다.');
      return;
    }
    saveLocalRecentEmoticon(item);
    closeEmoticonPicker();
    toast('이모티콘을 전송했습니다.');
    setTimeout(() => {
      if (state.activeRoomId) fetchRoomMessages(state.activeRoomId, { scrollBottom: true });
      loadSidebar();
    }, 500);
  } catch (e) {
    toast('이모티콘 전송 중 오류가 발생했습니다.');
  } finally {
    state.emoticons.sending = false;
    btnEmoticonSend.disabled = false;
  }
}

function renderEmoticonTabs() {
  emoticonTabs.replaceChildren();
  const makeTab = (id, label, iconUrl) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'emoticon-tab-btn' + (state.emoticons.activeTab === id ? ' active' : '');
    btn.title = label;
    if (iconUrl) {
      const img = document.createElement('img');
      img.src = iconUrl;
      img.alt = label;
      img.onerror = () => {
        img.remove();
        btn.textContent = label.slice(0, 2);
      };
      btn.appendChild(img);
    } else {
      btn.textContent = label;
    }
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      state.emoticons.activeTab = id;
      state.emoticons.search = '';
      emoticonSearch.value = '';
      selectEmoticonItem(null);
      renderEmoticonTabs();
      renderEmoticonGrid();
    });
    return btn;
  };

  emoticonTabs.appendChild(makeTab('recent', '🕒', ''));
  state.emoticons.packs.forEach((p) => {
    emoticonTabs.appendChild(makeTab(p.code, p.title || p.code, p.icon_url));
  });
  emoticonTabs.appendChild(makeTab('unicode', '😀', ''));
}

function renderEmoticonGrid() {
  emoticonGrid.replaceChildren();
  const q = state.emoticons.search.trim().toLowerCase();
  const appendKakaoCell = (item) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'emoticon-cell';
    btn.dataset.key = `${item.item_code}:${item.thumb_file}`;
    if (state.emoticons.selected && state.emoticons.selected.item_code === item.item_code
        && state.emoticons.selected.thumb_file === item.thumb_file) {
      btn.classList.add('selected');
    }
    const kws = (item.keywords || []).slice(0, 4).join(', ');
    btn.title = item.label || kws || '이모티콘';
    const img = document.createElement('img');
    img.src = item.thumb_url;
    img.alt = item.label || '이모티콘';
    img.loading = 'lazy';
    btn.appendChild(img);
    btn.addEventListener('click', () => selectEmoticonItem(item));
    btn.addEventListener('dblclick', () => sendSelectedEmoticon(item));
    emoticonGrid.appendChild(btn);
  };
  const appendEmojiCell = (em) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'emoticon-cell emoji-cell';
    btn.title = em.kw;
    btn.textContent = em.ch;
    btn.addEventListener('click', () => insertUnicodeEmoji(em.ch));
    emoticonGrid.appendChild(btn);
  };

  if (state.emoticons.loading) {
    emoticonGrid.classList.remove('emoji-mode');
    const msg = document.createElement('div');
    msg.className = 'emoticon-empty';
    msg.textContent = '이모티콘 목록을 불러오는 중입니다...';
    emoticonGrid.appendChild(msg);
    return;
  }

  if (q) {
    emoticonGrid.classList.remove('emoji-mode');
    let count = 0;
    const seen = new Set();
    state.emoticons.packs.forEach((pack) => {
      const packHit = (pack.title || '').toLowerCase().includes(q);
      (pack.items || []).forEach((it) => {
        const key = `${it.item_code}:${it.thumb_file}`;
        if (seen.has(key)) return;
        const labelHit = (it.label || '').toLowerCase().includes(q);
        const kwHit = (it.keywords || []).some((k) => k.toLowerCase().includes(q));
        if (packHit || labelHit || kwHit) {
          seen.add(key);
          appendKakaoCell(it);
          count++;
        }
      });
    });
    UNICODE_EMOJIS.filter((em) => em.kw.toLowerCase().includes(q) || em.ch.includes(q))
      .forEach((em) => {
        appendEmojiCell(em);
        count++;
      });
    if (!count) {
      const empty = document.createElement('div');
      empty.className = 'emoticon-empty';
      empty.textContent = `'${q}' 검색 결과가 없습니다.`;
      emoticonGrid.appendChild(empty);
    }
    return;
  }

  if (state.emoticons.activeTab === 'unicode') {
    emoticonGrid.classList.add('emoji-mode');
    UNICODE_EMOJIS.forEach(appendEmojiCell);
    return;
  }

  emoticonGrid.classList.remove('emoji-mode');
  if (state.emoticons.activeTab === 'recent') {
    if (!state.emoticons.recent.length) {
      const empty = document.createElement('div');
      empty.className = 'emoticon-empty';
      empty.textContent = '최근 사용한 이모티콘이 없습니다. 위 팩 아이콘이나 😀 탭을 눌러 보세요.';
      emoticonGrid.appendChild(empty);
      return;
    }
    state.emoticons.recent.forEach(appendKakaoCell);
    return;
  }

  const pack = state.emoticons.packs.find((p) => p.code === state.emoticons.activeTab);
  if (!pack || !pack.items || !pack.items.length) {
    const empty = document.createElement('div');
    empty.className = 'emoticon-empty';
    empty.textContent = '표시할 이모티콘이 없습니다.';
    emoticonGrid.appendChild(empty);
    return;
  }
  pack.items.forEach(appendKakaoCell);
}

async function loadEmoticons(force = false) {
  if (state.emoticons.loading) return;
  if (state.emoticons.loaded && !force) return;
  state.emoticons.loading = true;
  renderEmoticonGrid();
  try {
    const data = await apiJson('/api/emoticons');
    state.emoticons.available = Boolean(data.available);
    state.emoticons.packs = data.packs || [];
    mergeRecentEmoticons(data.recent || []);
    state.emoticons.loaded = true;
    if (!state.emoticons.recent.length && state.emoticons.packs.length) {
      state.emoticons.activeTab = state.emoticons.packs[0].code;
    } else if (!state.emoticons.recent.length && !state.emoticons.packs.length) {
      state.emoticons.activeTab = 'unicode';
    }
  } catch (e) {
    toast('이모티콘 목록을 불러오지 못했습니다.');
    state.emoticons.activeTab = 'unicode';
  } finally {
    state.emoticons.loading = false;
    renderEmoticonTabs();
    renderEmoticonGrid();
  }
}

function openEmoticonPicker() {
  if (!state.activeRoomId || !state.activeRoom || !state.activeRoom.writable) return;
  state.emoticons.open = true;
  emoticonPicker.classList.remove('hidden');
  btnEmoticon.classList.add('active');
  selectEmoticonItem(null);
  renderEmoticonTabs();
  renderEmoticonGrid();
  if (!state.emoticons.loaded) {
    loadEmoticons();
  }
  emoticonSearch.focus();
}

function closeEmoticonPicker(restoreFocus = true) {
  if (!state.emoticons.open) return;
  state.emoticons.open = false;
  emoticonPicker.classList.add('hidden');
  btnEmoticon.classList.remove('active');
  selectEmoticonItem(null);
  if (restoreFocus && state.activeRoom && state.activeRoom.writable) {
    msgInput.focus();
  }
}

function toggleEmoticonPicker() {
  if (state.emoticons.open) closeEmoticonPicker();
  else openEmoticonPicker();
}

btnEmoticon.addEventListener('click', (e) => {
  e.stopPropagation();
  toggleEmoticonPicker();
});
emoticonPicker.addEventListener('click', (e) => {
  e.stopPropagation();
});
btnEmoticonClose.addEventListener('click', () => closeEmoticonPicker());
btnEmoticonSend.addEventListener('click', () => {
  if (state.emoticons.selected) sendSelectedEmoticon(state.emoticons.selected);
});
emoticonSearch.addEventListener('input', () => {
  state.emoticons.search = emoticonSearch.value;
  renderEmoticonGrid();
});
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.code === 'KeyE' || e.key.toLowerCase() === 'e')) {
    if (state.activeRoomId && state.activeRoom && state.activeRoom.writable) {
      e.preventDefault();
      toggleEmoticonPicker();
    }
    return;
  }
  if (e.key === 'Escape' && state.emoticons.open) {
    e.preventDefault();
    closeEmoticonPicker();
    return;
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && state.emoticons.open && state.emoticons.selected) {
    if (document.activeElement !== msgInput || !msgInput.value.trim()) {
      e.preventDefault();
      sendSelectedEmoticon(state.emoticons.selected);
    }
  }
});
document.addEventListener('click', (e) => {
  if (!state.emoticons.open) return;
  if (!emoticonPicker.contains(e.target) && !btnEmoticon.contains(e.target)) {
    closeEmoticonPicker(false);
  }
});
document.addEventListener('paste', (e) => {
  if (!state.activeRoomId || !state.activeRoom || !state.activeRoom.writable) return;
  const cd = e.clipboardData;
  if (!cd) return;
  const pickWithValidName = (rawFile) => {
    if (!rawFile) return null;
    if (rawFile.name && rawFile.name.trim()) return rawFile;
    const ext = ((rawFile.type || 'image/png').split('/')[1] || 'png').split(';')[0];
    return new File([rawFile], `paste_${Date.now()}.${ext}`, { type: rawFile.type || 'image/png' });
  };
  if (cd.files && cd.files.length > 0) {
    const f = pickWithValidName(cd.files[0]);
    if (f) { e.preventDefault(); uploadFile(f); return; }
  }
  const items = cd.items;
  if (!items) return;
  for (const it of items) {
    if (it.kind === 'file' || (it.type && it.type.startsWith('image/'))) {
      const f = pickWithValidName(it.getAsFile());
      if (f) { e.preventDefault(); uploadFile(f); return; }
    }
  }
});
el('chat-pane').addEventListener('dragover', (e) => e.preventDefault());
el('chat-pane').addEventListener('drop', (e) => {
  e.preventDefault();
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]) {
    uploadFile(e.dataTransfer.files[0]);
  }
});

el('lightbox-close').addEventListener('click', () => el('lightbox').classList.add('hidden'));
el('lightbox').addEventListener('click', (e) => {
  if (e.target === el('lightbox')) el('lightbox').classList.add('hidden');
});

let activeES = null;
function closeSSE() {
  if (activeES) {
    activeES.close();
    activeES = null;
  }
}
window.addEventListener('beforeunload', closeSSE);
window.addEventListener('pagehide', closeSSE);

let sseRoomsTimer = null;
let sseRoomMsgTimer = null;
function connectSSE() {
  closeSSE();
  const es = new EventSource('/api/events');
  activeES = es;
  es.addEventListener('rooms_updated', () => {
    clearTimeout(sseRoomsTimer);
    sseRoomsTimer = setTimeout(() => {
      if (state.nav !== 'friends') loadSidebar();
      loadStatus();
    }, 450);
  });
  es.addEventListener('room_messages_updated', (ev) => {
    try {
      const d = JSON.parse(ev.data);
      if (d.chat_id && d.chat_id === state.activeRoomId) {
        const targetId = d.chat_id;
        clearTimeout(sseRoomMsgTimer);
        sseRoomMsgTimer = setTimeout(() => {
          if (state.activeRoomId === targetId) fetchRoomMessages(targetId);
        }, 350);
      }
    } catch (_) {}
  });
  es.onerror = () => {
    statusPill.textContent = '재연결 중';
  };
}

(async () => {
  await Promise.all([loadStatus(), loadSidebar()]);
  if (!state.activeRoomId && state.rooms.length > 0) {
    const firstUnlocked = state.rooms.find((r) => r.unlocked);
    if (firstUnlocked) {
      await selectRoom(firstUnlocked.id, false);
    }
  }
  connectSSE();
})();
