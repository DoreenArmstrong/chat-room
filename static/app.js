'use strict';

/* =========================================================
   聊天室 v3 前端（微信风）
   - 多房间：每个房间独立维护 lastRev / 消息缓存 / 已读水位
   - 图片消息：客户端压缩后上传，服务端单独端点读取
   - 头像：按设备保存，靠版本号刷新
   ========================================================= */

/* ---------------- 常量 ---------------- */
const POLL_NORMAL_MS = 1000;
const POLL_MAX_MS = 5000;
const NEAR_BOTTOM_PX = 80;
const READ_REPORT_MIN_MS = 800;
const TOP_LOAD_PX = 60;
const PHOTO_MAX_EDGE = 1600;
const PHOTO_QUALITY = 0.82;
const AVATAR_SIZE = 128;

/* ---------------- 身份 ---------------- */
function getOrCreate(storage, key, prefix) {
  let id = storage.getItem(key);
  if (!id) {
    id = prefix + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
    storage.setItem(key, id);
  }
  return id;
}

const store = {
  clientId: getOrCreate(sessionStorage, 'chat-client-id', 'c'),
  deviceId: getOrCreate(localStorage, 'chat-device-id', 'd'),
  name: localStorage.getItem('chat-name') || '',
  started: false,
  conn: 'connecting',
  failCount: 0,
  pollTimer: null,
  roomId: null,
  rooms: [],
  roomState: new Map(),
  avatars: {},
  limits: {
    msg_max: 2000, name_max: 20, recall: 120, edit: 600,
    max_keep: 500, history_page: 30, online_timeout: 20,
    image_max: 1500000, avatar_max: 200000, room_name_max: 20,
  },
  serverOffsetMs: 0,
  seq: Number(sessionStorage.getItem('chat-seq') || 0),
  lastReadReport: 0,
  lastScrollLoad: 0,
};

/* ---------------- DOM ---------------- */
const $ = (id) => document.getElementById(id);
const ui = {
  app: $('app'),
  roomList: $('room-list'), newRoomBtn: $('new-room-btn'),
  myAvatar: $('my-avatar'), myName: $('my-name'),
  roomName: $('room-name'), roomSub: $('room-sub'), backBtn: $('back-btn'),
  membersBtn: $('members-btn'), banner: $('banner'),
  chat: $('chat'), messages: $('messages'), loadMore: $('load-more'), jumpBtn: $('jump-btn'),
  input: $('text-input'), counter: $('counter'), sendBtn: $('send-btn'), photoBtn: $('photo-btn'),
  photoInput: $('photo-input'), avatarInput: $('avatar-input'),
  membersPanel: $('members-panel'), memberList: $('member-list'), panelCount: $('panel-count'),
  panelClose: $('panel-close'),
  lightbox: $('lightbox'), lightboxImg: $('lightbox-img'),
  login: $('login'), nameInput: $('name-input'), nameBtn: $('name-btn'), loginError: $('login-error'),
  roomDialog: $('room-dialog'), roomInput: $('room-input'), roomOk: $('room-ok'),
  roomCancel: $('room-cancel'), roomError: $('room-error'),
  toast: $('toast'),
};

/* ---------------- 小工具 ---------------- */
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

const AVATAR_COLORS = ['#5B8FF9', '#F6903D', '#3FB27F', '#9B7DF0', '#E8684A', '#3AA0C9', '#D9A322', '#5AC8D8'];
function avatarColor(name) {
  let hash = 0;
  for (const ch of String(name || '')) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function fmtWindow(seconds) {
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} 分钟`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  return `${seconds} 秒`;
}

function pad2(n) { return String(n).padStart(2, '0'); }
function fmtClock(epochSeconds) {
  const d = new Date(epochSeconds * 1000);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function fmtListTime(epochSeconds) {
  if (!epochSeconds) return '';
  const d = new Date(epochSeconds * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return fmtClock(epochSeconds);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

async function api(method, path, body) {
  const options = { method, cache: 'no-store' };
  if (body !== undefined && body !== null) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  return { res, data };
}

let toastTimer = null;
function showToast(text) {
  ui.toast.textContent = text;
  ui.toast.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ui.toast.classList.add('hidden'), 3200);
}

/* ---------------- 房间状态 ---------------- */
function newRoomState(id) {
  return {
    id,
    messages: new Map(),     // id -> {data, el}
    pending: new Map(),      // uuid -> {kind, text, dataUrl, uuid, error, el}
    lastRev: 0,
    lastMaxId: 0,
    latestId: 0,
    oldestId: 0,
    loadedOldest: 0,
    readId: 0,
    members: [],
    readPositions: {},
    loaded: false,
    loadingHistory: false,
    hasMore: true,
    dividerId: null,
  };
}

function ensureRoom(id) {
  if (!store.roomState.has(id)) store.roomState.set(id, newRoomState(id));
  return store.roomState.get(id);
}

function currentRoom() {
  return store.roomId ? ensureRoom(store.roomId) : null;
}

function roomName(id) {
  const r = store.rooms.find((x) => x.id === id);
  return r ? r.name : '房间';
}

/* ---------------- 头像 ---------------- */
function avatarInner(deviceId, name) {
  const v = store.avatars[deviceId];
  if (v) {
    return `<img src="/avatar?device_id=${encodeURIComponent(deviceId)}&v=${v}" alt="">`;
  }
  return `<span class="initial" style="background:${avatarColor(name)}">${escapeHtml(String(name || '?').slice(0, 1))}</span>`;
}
function avatarHtml(deviceId, name) {
  return `<div class="avatar">${avatarInner(deviceId, name)}</div>`;
}
function updateMyAvatar() {
  ui.myAvatar.innerHTML = avatarInner(store.deviceId, store.name);
  ui.myName.textContent = store.name || '';
}

/* ---------------- 连接状态 ---------------- */
function setConn(state) {
  if (store.conn === state) return;
  const prev = store.conn;
  store.conn = state;
  if (state === 'reconnecting') {
    ui.banner.className = 'banner warn';
    ui.banner.textContent = '连接中断，正在重连…（草稿和未发送的消息会保留）';
  } else if (state === 'offline') {
    ui.banner.className = 'banner danger';
    ui.banner.textContent = '已断开，正在重试连接…消息同步暂停，恢复后会自动补齐';
  } else {
    ui.banner.className = 'banner hidden';
    if (state === 'online' && (prev === 'reconnecting' || prev === 'offline')) showToast('已重新连接');
  }
  renderHeader();
}

function renderHeader() {
  const rs = currentRoom();
  ui.roomName.textContent = store.roomId ? roomName(store.roomId) : '聊天室';
  const online = store.conn === 'online' && rs ? rs.members.length : null;
  ui.roomSub.textContent = online === null ? '在线 —' : `在线 ${online}`;
}

/* ---------------- 会话列表 ---------------- */
function renderRooms() {
  if (!store.rooms.length) {
    ui.roomList.innerHTML = '<div class="r-empty">还没有房间</div>';
    return;
  }
  ui.roomList.innerHTML = store.rooms.map((r) => {
    const active = r.id === store.roomId ? ' active' : '';
    const badge = r.unread > 0 ? `<span class="badge">${r.unread > 99 ? '99+' : r.unread}</span>` : '';
    return `<div class="room${active}" data-room="${r.id}">
      <div class="r-avatar">${escapeHtml(r.name.slice(0, 1))}</div>
      <div class="r-main">
        <div class="r-top">
          <span class="r-name">${escapeHtml(r.name)}</span>
          <span class="r-time">${fmtListTime(r.last_ts)}</span>
        </div>
        <div class="r-bottom">
          <span class="r-preview">${escapeHtml(r.last_text || '还没有消息')}</span>
          ${badge}
        </div>
      </div>
    </div>`;
  }).join('');

  for (const el of ui.roomList.querySelectorAll('.room')) {
    el.addEventListener('click', () => openRoom(Number(el.dataset.room)));
  }
}

/* ---------------- 消息渲染 ---------------- */
function updateActions(el, m) {
  const btnEdit = el.querySelector('.btn-edit');
  const btnRevoke = el.querySelector('.btn-revoke');
  if (!btnEdit || !btnRevoke) return;
  const serverNow = (Date.now() + store.serverOffsetMs) / 1000;
  const age = serverNow - (m.ts || 0);
  btnEdit.disabled = age > (store.limits.edit || 600);
  if (btnEdit.disabled) btnEdit.title = `超过 ${fmtWindow(store.limits.edit)}，不能编辑了`;
  else btnEdit.title = '';
  btnRevoke.disabled = age > (store.limits.recall || 120);
  if (btnRevoke.disabled) btnRevoke.title = `超过 ${fmtWindow(store.limits.recall)}，不能撤回了`;
  else btnRevoke.title = '';
}

function updateReceipt(el, m) {
  const box = el.querySelector('.receipt');
  if (!box) return;
  const rs = currentRoom();
  const audience = Array.isArray(m.audience) ? m.audience : [];
  if (!audience.length) {
    box.textContent = '暂无其他在线成员';
    box.title = '';
    box.classList.remove('read');
    return;
  }
  const readers = [];
  for (const dev of audience) {
    const rp = rs && rs.readPositions[dev];
    if (rp && (rp.read_id || 0) >= m.id) readers.push(rp.name || dev.slice(0, 4));
  }
  box.classList.toggle('read', readers.length > 0);
  if (readers.length === audience.length) box.textContent = '全部已读';
  else if (readers.length === 0) box.textContent = '未读';
  else box.textContent = `已读 ${readers.length}/${audience.length}`;
  box.title = readers.length ? '已读：' + readers.join('、') : '';
}

function paintMessage(el, m) {
  el.dataset.id = String(m.id);
  el.dataset.cid = m.device_id;

  if (m.system) {
    el.className = 'msg system-msg';
    el.textContent = m.text;
    return;
  }
  const mine = m.device_id === store.deviceId;
  if (m.revoked) {
    el.className = 'msg system-msg';
    el.textContent = mine ? '你撤回了一条消息' : `${m.name} 撤回了一条消息`;
    return;
  }

  el.className = 'msg ' + (mine ? 'mine' : 'other');
  const edited = m.edited_at ? '<span class="edited">(已编辑)</span>' : '';
  const nameLine = mine
    ? `<div class="name"><span class="time">${escapeHtml(m.time)}</span>${edited}</div>`
    : `<div class="name">${escapeHtml(m.name)}<span class="time">${escapeHtml(m.time)}</span>${edited}</div>`;
  const body = m.kind === 'image'
    ? `<div class="bubble image"><img class="msg-image" loading="lazy" alt="图片"
         src="/image?message_id=${m.id}&v=${m.rev}"></div>`
    : `<div class="bubble">${escapeHtml(m.text)}</div>`;
  el.innerHTML = avatarHtml(m.device_id, m.name) +
    '<div class="body">' +
      nameLine +
      body +
      (mine
        ? '<div class="receipt"></div><div class="actions">' +
          '<button type="button" class="btn-edit">编辑</button>' +
          '<button type="button" class="btn-revoke">撤回</button></div>'
        : '') +
    '</div>';

  const img = el.querySelector('.msg-image');
  if (img) img.addEventListener('click', () => openLightbox(img.src));

  if (!mine) return;
  const editBtn = el.querySelector('.btn-edit');
  const revokeBtn = el.querySelector('.btn-revoke');
  if (m.kind !== 'text') editBtn.style.display = 'none';
  editBtn.addEventListener('click', () => startEdit(el, m));
  revokeBtn.addEventListener('click', () => revokeMessage(m));
  el.querySelector('.bubble').addEventListener('click', () => {
    if (m.kind === 'text') el.classList.toggle('show-actions');
  });
  updateActions(el, m);
  updateReceipt(el, m);
}

/* 按 id 顺序插入（分隔线也参与排序） */
function insertMessageDom(m) {
  const rs = currentRoom();
  const item = rs.messages.get(m.id);
  if (!item) return;
  const el = item.el || document.createElement('div');
  item.el = el;
  paintMessage(el, m);

  let ref = null;
  for (const child of ui.messages.children) {
    if (child.classList.contains('gap-hint')) continue;
    if (child.dataset.divider === '1') {
      if (m.id <= Number(child.dataset.id)) { ref = child; break; }
      continue;
    }
    if (Number(child.dataset.id) > m.id) { ref = child; break; }
  }
  ui.messages.insertBefore(el, ref);
}

function insertDivider(dividerId) {
  const div = document.createElement('div');
  div.className = 'read-divider';
  div.dataset.divider = '1';
  div.dataset.id = String(dividerId);
  div.textContent = '以下为新消息';
  let ref = null;
  for (const child of ui.messages.children) {
    if (child.classList.contains('gap-hint')) continue;
    if (child.dataset.divider === '1') continue;
    if (Number(child.dataset.id) > dividerId) { ref = child; break; }
  }
  ui.messages.insertBefore(div, ref);
}

function renderPending(p) {
  const el = document.createElement('div');
  el.className = 'msg mine pending';
  el.dataset.uuid = p.uuid;
  el.dataset.id = '999999999';
  const body = p.kind === 'image'
    ? `<div class="bubble image"><img class="msg-image" src="${p.dataUrl}" alt="图片"></div>`
    : `<div class="bubble">${escapeHtml(p.text)}</div>`;
  el.innerHTML = avatarHtml(store.deviceId, store.name) +
    `<div class="body">
       <div class="name">${escapeHtml(store.name)}<span class="time">刚刚</span></div>
       ${body}
       <div class="meta"><span class="status">发送中…</span></div>
     </div>`;
  if (p.error) markPendingFailed(p, el);
  p.el = el;
  ui.messages.appendChild(el);
  return el;
}

function markPendingFailed(p, el) {
  const box = el || p.el;
  if (!box) return;
  box.classList.remove('pending');
  box.classList.add('failed');
  const meta = box.querySelector('.meta');
  meta.innerHTML = '<span class="fail-tip">发送失败 · <button type="button" class="btn-retry">点击重试</button></span>';
  meta.querySelector('.btn-retry').addEventListener('click', (ev) => {
    ev.stopPropagation();
    retryPending(p);
  });
}

function rebuildMessages() {
  const rs = currentRoom();
  if (!rs) return;
  ui.messages.innerHTML = '';
  const ids = [...rs.messages.keys()].sort((a, b) => a - b);
  for (const id of ids) {
    const item = rs.messages.get(id);
    const el = document.createElement('div');
    item.el = el;
    paintMessage(el, item.data);
    ui.messages.appendChild(el);
  }
  if (rs.dividerId) insertDivider(rs.dividerId);
  for (const p of rs.pending.values()) renderPending(p);
  renderReceipts();
}

function renderReceipts() {
  const rs = currentRoom();
  if (!rs) return;
  for (const { data: m, el } of rs.messages.values()) {
    if (!m.system && m.device_id === store.deviceId && !m.revoked && el) updateReceipt(el, m);
  }
}

/* ---------------- 图片查看 ---------------- */
function openLightbox(src) {
  ui.lightboxImg.src = src;
  ui.lightbox.classList.remove('hidden');
}
function closeLightbox() {
  ui.lightbox.classList.add('hidden');
  ui.lightboxImg.removeAttribute('src');
}

/* ---------------- 滚动 / 未读 ---------------- */
function isNearBottom() {
  return ui.chat.scrollHeight - ui.chat.scrollTop - ui.chat.clientHeight < NEAR_BOTTOM_PX;
}
function scrollToBottom() {
  ui.chat.scrollTop = ui.chat.scrollHeight;
}
function scrollDividerIntoView() {
  const divider = ui.messages.querySelector('.read-divider');
  if (!divider) return false;
  const chatRect = ui.chat.getBoundingClientRect();
  const divRect = divider.getBoundingClientRect();
  ui.chat.scrollTop += (divRect.top - chatRect.top) - 8;
  return true;
}

function updateUnread() {
  const rs = currentRoom();
  let unread = 0;
  if (rs) {
    for (const { data: m } of rs.messages.values()) {
      if (m.system || m.revoked || m.device_id === store.deviceId) continue;
      if (m.id > rs.readId) unread += 1;
    }
  }
  document.title = unread > 0 ? `(${unread}) 聊天室` : '聊天室';
  if (isNearBottom()) {
    ui.jumpBtn.classList.add('hidden');
  } else {
    ui.jumpBtn.classList.remove('hidden');
    ui.jumpBtn.textContent = unread > 0 ? `↓ ${unread} 条新消息` : '回到底部';
  }
}

function maybeReportRead() {
  const rs = currentRoom();
  if (!rs || !store.started) return;
  if (document.visibilityState !== 'visible') return;
  if (!isNearBottom()) return;
  const target = rs.latestId;
  if (target <= rs.readId) return;
  const now = Date.now();
  if (now - store.lastReadReport < READ_REPORT_MIN_MS) return;
  store.lastReadReport = now;
  rs.readId = target;
  updateUnread();
  renderReceipts();
  api('POST', '/read', { client_id: store.clientId, read_id: target, room_id: rs.id }).catch(() => {});
}

/* ---------------- 历史记录 ---------------- */
function setTopHint(state) {
  if (state === 'loading') {
    ui.loadMore.textContent = '加载中…';
    ui.loadMore.disabled = true;
  } else if (state === 'end') {
    ui.loadMore.textContent = `没有更多了 · 每房保留最近 ${store.limits.max_keep} 条`;
    ui.loadMore.disabled = true;
  } else {
    ui.loadMore.textContent = '加载更早消息';
    ui.loadMore.disabled = false;
  }
}

function showGapHint(count, what) {
  if (ui.messages.querySelector('.gap-hint')) return;
  const el = document.createElement('div');
  el.className = 'gap-hint';
  el.textContent = `${what}：有 ${count} 条消息已不在服务器上（每房只保留最近 ${store.limits.max_keep} 条），无法补齐。`;
  ui.messages.insertBefore(el, ui.messages.firstChild);
}

async function loadInitialHistory(roomId) {
  const rs = ensureRoom(roomId);
  if (rs.loadingHistory || rs.loaded) return;
  rs.loadingHistory = true;
  setTopHint('loading');
  try {
    const { res, data } = await api('GET', `/history?room_id=${roomId}&limit=${store.limits.history_page}`);
    if (!res.ok || !data || !data.ok) throw new Error('history failed');
    for (const m of data.messages) {
      if (!rs.messages.has(m.id)) rs.messages.set(m.id, { data: m, el: null });
    }
    rs.loaded = true;
    rs.hasMore = !!data.has_more;
    rs.oldestId = data.oldest_id || 0;
    rs.latestId = Math.max(rs.latestId, data.latest_id || 0);

    let maxRev = 0, maxId = 0, minId = 0;
    for (const [id, item] of rs.messages) {
      maxRev = Math.max(maxRev, item.data.rev || 0);
      maxId = Math.max(maxId, id);
      minId = minId ? Math.min(minId, id) : id;
    }
    rs.lastRev = Math.max(rs.lastRev, maxRev);
    rs.lastMaxId = Math.max(rs.lastMaxId, maxId);
    rs.loadedOldest = minId;
    if (!rs.dividerId && rs.readId && rs.latestId > rs.readId) rs.dividerId = rs.readId;
    setTopHint(rs.hasMore ? 'more' : 'end');
  } catch (err) {
    setTopHint('more');
    showToast('加载历史失败，请稍后重试');
  } finally {
    rs.loadingHistory = false;
  }
}

async function loadOlder() {
  const rs = currentRoom();
  if (!rs || !rs.loaded || rs.loadingHistory || !rs.hasMore) return;
  rs.loadingHistory = true;
  setTopHint('loading');
  const prevHeight = ui.chat.scrollHeight;
  const prevTop = ui.chat.scrollTop;
  try {
    const { res, data } = await api(
      'GET', `/history?room_id=${rs.id}&before=${rs.loadedOldest}&limit=${store.limits.history_page}`);
    if (!res.ok || !data || !data.ok) throw new Error('history failed');
    for (const m of data.messages) {
      if (!rs.messages.has(m.id)) rs.messages.set(m.id, { data: m, el: null });
      if (!rs.loadedOldest || m.id < rs.loadedOldest) rs.loadedOldest = m.id;
    }
    rs.hasMore = !!data.has_more;
    setTopHint(rs.hasMore ? 'more' : 'end');
    rebuildMessages();
    ui.chat.scrollTop = prevTop + (ui.chat.scrollHeight - prevHeight);
  } catch (err) {
    setTopHint(rs.hasMore ? 'more' : 'end');
    showToast('加载更早消息失败');
  } finally {
    rs.loadingHistory = false;
  }
}

/* ---------------- 编辑 / 撤回 ---------------- */
function startEdit(el, m) {
  if (el.classList.contains('editing')) return;
  const bubble = el.querySelector('.bubble');
  if (!bubble) return;
  el.classList.add('editing');

  const editor = document.createElement('div');
  editor.className = 'editor';
  editor.innerHTML =
    `<textarea>${escapeHtml(m.text)}</textarea>` +
    '<div class="editor-actions">' +
    '<button type="button" class="btn-cancel">取消</button>' +
    '<button type="button" class="btn-save">保存</button></div>';
  bubble.replaceWith(editor);

  const textarea = editor.querySelector('textarea');
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  const cancel = () => {
    el.classList.remove('editing');
    paintMessage(el, m);
  };
  editor.querySelector('.btn-cancel').addEventListener('click', cancel);

  editor.querySelector('.btn-save').addEventListener('click', async () => {
    const text = textarea.value.trim();
    if (!text) { showToast('消息不能为空'); return; }
    if (text.length > store.limits.msg_max) {
      showToast(`消息过长：最多 ${store.limits.msg_max} 字，当前 ${text.length} 字`);
      return;
    }
    const { res, data } = await api('POST', '/edit', {
      client_id: store.clientId, id: m.id, text,
    });
    if (!res.ok || !data || !data.ok) {
      showToast((data && data.error) || '编辑失败，请稍后重试');
      return;
    }
    el.classList.remove('editing');
    applyMessage(m.room_id, data.msg);
  });
}

async function revokeMessage(m) {
  if (!window.confirm('确定撤回这条消息？撤回后原文不会保留。')) return;
  const { res, data } = await api('POST', '/revoke', {
    client_id: store.clientId, id: m.id,
  });
  if (!res.ok || !data || !data.ok) {
    showToast((data && data.error) || '撤回失败，请稍后重试');
    return;
  }
  applyMessage(m.room_id, data.msg);
}

/* ---------------- 发送 ---------------- */
function updateCounter() {
  const max = store.limits.msg_max || 2000;
  const len = ui.input.value.length;
  ui.counter.textContent = `${len}/${max}`;
  ui.counter.classList.toggle('over', len > max);
}

let draftTimer = null;
function draftKey(hint) {
  const id = hint === undefined ? store.roomId : hint;
  return 'chat-draft-' + (id || '0');
}
function saveDraftSoon() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    if (store.roomId) sessionStorage.setItem(draftKey(), ui.input.value);
  }, 250);
}
function saveDraftNow() {
  if (store.roomId) sessionStorage.setItem(draftKey(), ui.input.value);
}
function restoreDraft() {
  ui.input.value = store.roomId ? (sessionStorage.getItem(draftKey()) || '') : '';
  updateCounter();
}

function removePending(p) {
  const rs = ensureRoom(p.roomId);
  rs.pending.delete(p.uuid);
  if (p.el && p.el.parentNode) p.el.remove();
  p.el = null;
}

async function sendCurrentInput() {
  const rs = currentRoom();
  if (!rs) return;
  const text = ui.input.value.trim();
  if (!text) { ui.input.focus(); return; }
  if (text.length > store.limits.msg_max) {
    showToast(`消息过长：最多 ${store.limits.msg_max} 字，当前 ${text.length} 字`);
    return;
  }
  store.seq += 1;
  sessionStorage.setItem('chat-seq', String(store.seq));
  const p = {
    kind: 'text', text, uuid: `${store.clientId}-${store.seq}`,
    roomId: rs.id, error: false, el: null,
  };
  rs.pending.set(p.uuid, p);
  renderPending(p);
  ui.input.value = '';
  updateCounter();
  saveDraftNow();
  scrollToBottom();
  updateUnread();
  await postText(p);
}

async function postText(p) {
  try {
    const { res, data } = await api('POST', '/send', {
      client_id: store.clientId, device_id: store.deviceId,
      name: store.name, room_id: p.roomId, text: p.text, msg_uuid: p.uuid,
    });
    if (!res.ok || !data || !data.ok) {
      removePending(p);
      if (store.roomId === p.roomId) {
        ui.input.value = p.text;
        updateCounter();
        saveDraftNow();
      }
      showToast((data && data.error) || `发送失败（HTTP ${res.status}）`);
      return;
    }
    removePending(p);
    applyMessage(p.roomId, data.msg);
    if (data.duplicate) showToast('这条消息之前已经发送成功，已自动去重');
    maybeReportRead();
    updateUnread();
  } catch (err) {
    p.error = true;
    if (p.el) markPendingFailed(p, p.el);
    showToast('网络异常，消息没有发出去（点气泡里的“点击重试”）');
  }
}

async function postImage(p) {
  try {
    const { res, data } = await api('POST', '/send_image', {
      client_id: store.clientId, device_id: store.deviceId, name: store.name,
      room_id: p.roomId, msg_uuid: p.uuid, data_url: p.dataUrl,
      width: p.width, height: p.height,
    });
    if (!res.ok || !data || !data.ok) {
      removePending(p);
      showToast((data && data.error) || `发送失败（HTTP ${res.status}）`);
      return;
    }
    removePending(p);
    applyMessage(p.roomId, data.msg);
    scrollToBottom();
    updateUnread();
  } catch (err) {
    p.error = true;
    if (p.el) markPendingFailed(p, p.el);
    showToast('网络异常，图片没有发出去（点“点击重试”）');
  }
}

async function retryPending(p) {
  if (!p.el) return;
  p.error = false;
  p.el.classList.remove('failed');
  p.el.classList.add('pending');
  p.el.querySelector('.meta').innerHTML = '<span class="status">发送中…</span>';
  if (p.kind === 'image') await postImage(p);
  else await postText(p);
}

/* ---------------- 应用一条消息 ---------------- */
function applyMessage(roomId, m) {
  const rs = ensureRoom(roomId || 1);
  if (m.uuid && rs.pending.has(m.uuid)) removePending(rs.pending.get(m.uuid));

  const existing = rs.messages.get(m.id);
  if (existing) {
    if ((m.rev || 0) < (existing.data.rev || 0)) return;
    existing.data = m;
    if (store.roomId === rs.id) {
      const el = existing.el || document.createElement('div');
      existing.el = el;
      paintMessage(el, m);
    }
  } else {
    rs.messages.set(m.id, { data: m, el: null });
    if (store.roomId === rs.id) insertMessageDom(m);
  }
  rs.lastRev = Math.max(rs.lastRev, m.rev || 0);
  rs.lastMaxId = Math.max(rs.lastMaxId, m.id);
  rs.latestId = Math.max(rs.latestId, m.id);
}

/* ---------------- 轮询 ---------------- */
function schedulePoll(delay) {
  clearTimeout(store.pollTimer);
  store.pollTimer = setTimeout(pollOnce, delay);
}

async function pollOnce() {
  if (!store.started || !store.roomId) return;
  const roomId = store.roomId;
  const rs = ensureRoom(roomId);
  const prevConn = store.conn;
  let delay = POLL_NORMAL_MS;
  try {
    const { res, data } = await api(
      'GET', `/state?client_id=${encodeURIComponent(store.clientId)}&rev=${rs.lastRev}&room_id=${roomId}`);
    if (!res.ok || !data || !data.ok) throw new Error(`state ${res.status}`);
    if (data.reset) {
      showToast('服务器数据已重置，正在重新加载…');
      location.reload();
      return;
    }
    if (store.roomId !== roomId) return;   // 已切房，丢弃这次结果
    store.failCount = 0;
    setConn('online');
    handleState(roomId, data, prevConn);
    if (data.known === false || prevConn === 'reconnecting' || prevConn === 'offline') {
      rejoinSilently();
    }
  } catch (err) {
    store.failCount += 1;
    setConn(store.failCount <= 2 ? 'reconnecting' : 'offline');
    delay = Math.min(POLL_MAX_MS, 1000 * store.failCount);
  } finally {
    schedulePoll(delay);
  }
}

let lastRejoinAt = 0;
function rejoinSilently() {
  const now = Date.now();
  if (now - lastRejoinAt < 3000) return;
  lastRejoinAt = now;
  api('POST', '/join', {
    client_id: store.clientId, device_id: store.deviceId,
    name: store.name, room_id: store.roomId || 1, silent: true,
  }).then(({ res, data }) => {
    if (res.ok && data && data.ok && data.limits) store.limits = data.limits;
  }).catch(() => {});
}

function handleState(roomId, data, prevConn) {
  store.serverOffsetMs = data.server_time * 1000 - Date.now();
  if (data.limits) store.limits = data.limits;
  const rs = ensureRoom(roomId);

  for (const m of data.messages || []) applyMessage(roomId, m);

  const oldAvatars = JSON.stringify(store.avatars);
  store.avatars = data.avatars || {};
  store.rooms = data.rooms || [];
  renderRooms();

  rs.members = data.members || [];
  rs.readPositions = data.read_positions || {};
  rs.oldestId = data.oldest_id || 0;
  rs.latestId = Math.max(rs.latestId, data.latest_id || 0);
  if ((data.my_read_id || 0) > rs.readId) rs.readId = data.my_read_id;

  if (store.roomId === roomId) {
    renderHeader();
    renderMembers();
    renderReceipts();
    if (oldAvatars !== JSON.stringify(store.avatars)) {
      rebuildMessages();      // 有人的头像变了，重画一遍
      updateMyAvatar();
    }
    if (prevConn !== 'online' && rs.lastMaxId > 0 && data.oldest_id > rs.lastMaxId + 1) {
      showGapHint(data.oldest_id - rs.lastMaxId - 1, '断线期间有部分消息已超过保留范围');
    }
    if (data.messages && data.messages.length) {
      if (isNearBottom()) scrollToBottom();
      updateUnread();
    }
  }
  maybeReportRead();
}

/* ---------------- 成员面板 ---------------- */
function renderMembers() {
  const rs = currentRoom();
  if (!rs) return;
  const nameCount = {};
  for (const mb of rs.members) nameCount[mb.name] = (nameCount[mb.name] || 0) + 1;
  const html = rs.members.map((mb) => {
    const tags = [];
    if (nameCount[mb.name] > 1) {
      tags.push(`<span class="tag" title="同名的人用短码区分">#${escapeHtml(String(mb.device_id || '').slice(-3))}</span>`);
    }
    if (mb.device_id === store.deviceId) tags.push('<span class="tag me">我</span>');
    if ((mb.count || 1) > 1) tags.push(`<span class="tag" title="这个浏览器开着多个窗口">${mb.count} 个窗口</span>`);
    return `<div class="member"><span class="dot"></span>` +
      `<span class="mname">${escapeHtml(mb.name)}</span>${tags.join('')}` +
      `<span class="mtime">${mb.joined_at ? fmtClock(mb.joined_at) + ' 加入' : ''}</span></div>`;
  }).join('');
  ui.memberList.innerHTML = html || '<div class="empty">这个房间还没有其他人</div>';
  ui.panelCount.textContent = `(${rs.members.length})`;
  renderHeader();
}

function toggleMembers(show) {
  const shouldShow = show === undefined ? ui.membersPanel.classList.contains('hidden') : show;
  ui.membersPanel.classList.toggle('hidden', !shouldShow);
  if (shouldShow) renderMembers();
}

/* ---------------- 打开房间 ---------------- */
async function openRoom(roomId) {
  if (!store.started) return;
  if (store.roomId && store.roomId !== roomId) saveDraftNow();
  store.roomId = roomId;
  const rs = ensureRoom(roomId);
  document.body.classList.add('in-room');
  renderRooms();
  renderHeader();
  rebuildMessages();
  restoreDraft();
  updateUnread();

  if (!rs.loaded) {
    await loadInitialHistory(roomId);
    rebuildMessages();
    if (!scrollDividerIntoView()) scrollToBottom();
    updateUnread();
  } else {
    scrollToBottom();
  }
  schedulePoll(0);
  if (window.innerWidth > 820) ui.input.focus();
}

/* ---------------- 头像上传 ---------------- */
async function uploadAvatar(file) {
  try {
    const dataUrl = await compressAvatar(file);
    const { res, data } = await api('POST', '/avatar', {
      client_id: store.clientId, data_url: dataUrl,
    });
    if (!res.ok || !data || !data.ok) {
      showToast((data && data.error) || '头像上传失败');
      return;
    }
    store.avatars = data.avatars || store.avatars;
    updateMyAvatar();
    rebuildMessages();
    showToast('头像已更新');
  } catch (err) {
    showToast(err.message || '头像处理失败');
  }
}

/* ---------------- 图片压缩 ---------------- */
function readFileAsImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('无法读取这张图片')); };
    img.src = url;
  });
}

async function compressPhoto(file) {
  const img = await readFileAsImage(file);
  const scale = Math.min(1, PHOTO_MAX_EDGE / Math.max(img.width, img.height));
  const w = Math.max(1, Math.round(img.width * scale));
  const h = Math.max(1, Math.round(img.height * scale));
  const canvas = document.createElement('canvas');
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, w, h);
  ctx.drawImage(img, 0, 0, w, h);

  let q = PHOTO_QUALITY;
  let dataUrl = canvas.toDataURL('image/jpeg', q);
  while (dataUrl.length * 0.75 > store.limits.image_max && q > 0.4) {
    q -= 0.1;
    dataUrl = canvas.toDataURL('image/jpeg', q);
  }
  if (dataUrl.length * 0.75 > store.limits.image_max) {
    throw new Error('图片太大，换一张小一点的吧');
  }
  return { dataUrl, width: w, height: h };
}

async function compressAvatar(file) {
  const img = await readFileAsImage(file);
  const side = Math.min(img.width, img.height);
  const sx = Math.round((img.width - side) / 2);
  const sy = Math.round((img.height - side) / 2);
  const canvas = document.createElement('canvas');
  canvas.width = AVATAR_SIZE;
  canvas.height = AVATAR_SIZE;
  canvas.getContext('2d').drawImage(img, sx, sy, side, side, 0, 0, AVATAR_SIZE, AVATAR_SIZE);

  let q = 0.85;
  let dataUrl = canvas.toDataURL('image/jpeg', q);
  while (dataUrl.length * 0.75 > store.limits.avatar_max && q > 0.5) {
    q -= 0.1;
    dataUrl = canvas.toDataURL('image/jpeg', q);
  }
  if (dataUrl.length * 0.75 > store.limits.avatar_max) throw new Error('头像图片太大');
  return dataUrl;
}

/* ---------------- 进入 ---------------- */
async function startEnter(rawName) {
  const name = String(rawName || '').trim();
  ui.loginError.textContent = '';
  if (!name) { ui.loginError.textContent = '请输入昵称'; ui.nameInput.focus(); return; }
  if (name.length > store.limits.name_max) {
    ui.loginError.textContent = `昵称最多 ${store.limits.name_max} 字`;
    return;
  }
  ui.nameBtn.disabled = true;
  try {
    const { res, data } = await api('POST', '/join', {
      client_id: store.clientId, device_id: store.deviceId, name, room_id: 1,
    });
    if (!res.ok || !data || !data.ok) {
      ui.loginError.textContent = (data && data.error) || `进入失败（HTTP ${res.status}）`;
      ui.login.classList.remove('hidden');
      return;
    }
    enterSuccess(name, data);
  } catch (err) {
    ui.loginError.textContent = '连不上服务器：确认黑窗口还开着、地址端口正确';
    ui.login.classList.remove('hidden');
  } finally {
    ui.nameBtn.disabled = false;
  }
}

function enterSuccess(name, data) {
  store.name = name;
  localStorage.setItem('chat-name', name);
  if (data.limits) store.limits = data.limits;
  store.avatars = data.avatars || {};
  store.rooms = data.rooms || [];
  store.started = true;

  ui.login.classList.add('hidden');
  ui.input.disabled = false;
  ui.sendBtn.disabled = false;
  updateMyAvatar();

  const roomId = data.room_id || 1;
  store.roomId = roomId;
  const rs = ensureRoom(roomId);
  rs.readId = data.read_id || 0;
  rs.members = data.members || [];
  rs.latestId = data.latest_id || 0;

  document.body.classList.add('in-room');
  renderRooms();
  renderHeader();

  openRoom(roomId);
}

/* ---------------- 事件 ---------------- */
function bindEvents() {
  ui.nameBtn.addEventListener('click', () => startEnter(ui.nameInput.value));
  ui.nameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') startEnter(ui.nameInput.value);
  });

  ui.sendBtn.addEventListener('click', sendCurrentInput);
  ui.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendCurrentInput(); }
  });
  ui.input.addEventListener('input', () => { updateCounter(); saveDraftSoon(); });

  ui.loadMore.addEventListener('click', loadOlder);
  ui.chat.addEventListener('scroll', () => {
    if (!store.started) return;
    updateUnread();
    const now = Date.now();
    if (ui.chat.scrollTop < TOP_LOAD_PX && now - store.lastScrollLoad > 400) {
      store.lastScrollLoad = now;
      loadOlder();
    }
    if (isNearBottom()) maybeReportRead();
  }, { passive: true });

  ui.jumpBtn.addEventListener('click', () => scrollToBottom());

  ui.photoBtn.addEventListener('click', () => ui.photoInput.click());
  ui.photoInput.addEventListener('change', async () => {
    const file = ui.photoInput.files && ui.photoInput.files[0];
    ui.photoInput.value = '';
    if (!file || !currentRoom()) return;
    try {
      const { dataUrl, width, height } = await compressPhoto(file);
      store.seq += 1;
      sessionStorage.setItem('chat-seq', String(store.seq));
      const p = {
        kind: 'image', dataUrl, width, height,
        uuid: `${store.clientId}-${store.seq}`,
        roomId: store.roomId, error: false, el: null,
      };
      currentRoom().pending.set(p.uuid, p);
      renderPending(p);
      scrollToBottom();
      await postImage(p);
    } catch (err) {
      showToast(err.message || '图片处理失败');
    }
  });

  ui.myAvatar.addEventListener('click', () => ui.avatarInput.click());
  ui.avatarInput.addEventListener('change', () => {
    const file = ui.avatarInput.files && ui.avatarInput.files[0];
    ui.avatarInput.value = '';
    if (file) uploadAvatar(file);
  });

  ui.membersBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleMembers(); });
  ui.panelClose.addEventListener('click', () => toggleMembers(false));
  document.addEventListener('click', (e) => {
    if (ui.membersPanel.classList.contains('hidden')) return;
    if (ui.membersPanel.contains(e.target) || ui.membersBtn.contains(e.target)) return;
    toggleMembers(false);
  });

  ui.newRoomBtn.addEventListener('click', () => {
    ui.roomInput.value = '';
    ui.roomError.textContent = '';
    ui.roomDialog.classList.remove('hidden');
    ui.roomInput.focus();
  });
  ui.roomCancel.addEventListener('click', () => ui.roomDialog.classList.add('hidden'));
  ui.roomOk.addEventListener('click', createRoom);
  ui.roomInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') createRoom();
  });

  ui.backBtn.addEventListener('click', () => document.body.classList.remove('in-room'));

  ui.lightbox.addEventListener('click', closeLightbox);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && store.started) {
      schedulePoll(0);
      maybeReportRead();
    }
  });

  window.addEventListener('pagehide', () => {
    if (!store.started) return;
    try {
      const blob = new Blob([JSON.stringify({ client_id: store.clientId })], { type: 'application/json' });
      navigator.sendBeacon('/leave', blob);
    } catch (e) { /* 忽略 */ }
  });
}

async function createRoom() {
  const name = ui.roomInput.value.trim();
  if (!name) { ui.roomError.textContent = '请输入房间名'; return; }
  if (name.length > store.limits.room_name_max) {
    ui.roomError.textContent = `房间名最多 ${store.limits.room_name_max} 字`;
    return;
  }
  ui.roomOk.disabled = true;
  try {
    const { res, data } = await api('POST', '/rooms', {
      client_id: store.clientId, name,
    });
    if (!res.ok || !data || !data.ok) {
      ui.roomError.textContent = (data && data.error) || '创建失败';
      return;
    }
    store.rooms = data.rooms || store.rooms;
    ui.roomDialog.classList.add('hidden');
    renderRooms();
    openRoom(data.room.id);
    showToast(`已创建房间「${data.room.name}」`);
  } catch (err) {
    ui.roomError.textContent = '网络异常，请重试';
  } finally {
    ui.roomOk.disabled = false;
  }
}

/* ---------------- 启动 ---------------- */
function boot() {
  bindEvents();
  updateCounter();
  updateMyAvatar();
  if (store.name) {
    ui.nameInput.value = store.name;
    startEnter(store.name);
  } else {
    ui.nameInput.focus();
  }
}

boot();
