'use strict';
// ── 共用 ──
const $ = s => document.querySelector(s);
const esc = t => String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmtN = (n, d) => (n === null || n === undefined || isNaN(Number(n))) ? '—' : Number(n).toLocaleString('zh-TW', {minimumFractionDigits: d || 0, maximumFractionDigits: d || 0});
const isNA = d => d && d.configured === false;
let FEAT = null;

async function api(url, opt) {
  let r;
  try { r = await fetch(url, opt); } catch (e) { throw new Error('連不到伺服器（網路斷了或伺服器已停止）'); }
  if (r.status === 401) { location.href = '/login'; throw new Error('需要登入'); }
  const b = await r.json().catch(() => null);
  if (!r.ok) throw new Error((b && b.error) || ('HTTP ' + r.status));
  if (b === null) throw new Error('回應不是 JSON');
  return b;
}
const post = (u, payload) => api(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload || {})});
function showNA(id, d) { const n = $('#' + id); if (!n) return; n.style.display = d ? '' : 'none'; if (d) n.textContent = '⚠ ' + (d.reason || '') + '（設定方式見 README）'; }
function errBox(el, e) { el.innerHTML = '<div class="err">讀取失敗：' + esc(e.message || e) + '</div>'; }
function naBox(el, d) { el.innerHTML = '<div class="notice">⚠ ' + esc(d.reason) + '（設定方式見 README）</div>'; }
setInterval(() => { $('#clock').textContent = new Date().toLocaleTimeString('zh-TW', {hour12: false}); }, 1000);

// ── 分頁 ──
const loaded = {};
function openTab(t) {
  document.querySelectorAll('#nav button').forEach(b => b.classList.toggle('on', b.dataset.t === t));
  document.querySelectorAll('.tab').forEach(x => x.classList.toggle('on', x.id === t));
  if (t === 'commute') { initMap(); setTimeout(() => map && map.invalidateSize(), 50); if (!loaded.commute) { loaded.commute = 1; loadTrack(); } }
  if (t === 'dice' && !loaded.dice) { loaded.dice = 1; fillFoodSelects(); }
  if (t === 'db' && !loaded.db) { loaded.db = 1; fillFoodSelects(); loadDb(); }
  if (t === 'metro') { if (!loaded.metro) { loaded.metro = 1; setInterval(() => { if (!document.hidden && $('#metro').classList.contains('on')) loadMetro(); }, 30000); } loadMetro(); }
  if (t === 'custom') { if (!loaded.custom) { loaded.custom = 1; setInterval(() => { if (!document.hidden && $('#custom').classList.contains('on')) loadCustom(); }, 60000); } loadCustom(); }
}
document.querySelectorAll('#nav button').forEach(b => { b.onclick = () => openTab(b.dataset.t); });

async function init() {
  try { FEAT = await api('/api/features'); } catch (e) { $('#setup').style.display = ''; $('#setup').textContent = '讀不到伺服器設定：' + e.message; return; }
  document.title = FEAT.title; $('#title').textContent = FEAT.title;
  const f = FEAT.features;
  let first = null;
  document.querySelectorAll('#nav button').forEach(b => {
    const on = f[b.dataset.f].enabled;
    b.style.display = on ? '' : 'none';
    if (on && !first) first = b.dataset.t;
  });
  $('#nav-custom').textContent = FEAT.custom_title || '自訂';
  for (const [feat, id] of [['commute', 'commute-na'], ['food', 'dice-na'], ['food', 'near-na'], ['food', 'db-na'], ['metro', 'metro-na'], ['custom', 'custom-na']]) {
    const s = f[feat]; if (s.enabled && !s.ready) showNA(id, {reason: s.reason});
  }
  if (!first) { $('#setup').style.display = ''; $('#setup').textContent = '所有功能都已在設定檔關閉（features）。'; return; }
  $('#date').value = FEAT.today;
  openTab(first);
}

// ── 地圖與軌跡 ──
let map = null, layer = null, baseCtl = null, OSM = null;
const MODE_COLOR = {walk: '#1f9d55', vehicle: '#1e63d6', slow: '#8a94a6'};
const MODE_ICON = {walk: '🚶', vehicle: '🚗', slow: '🐢'};
function initMap() {
  if (map || !FEAT) return;
  map = L.map('map').setView(FEAT.map.center, 13);
  OSM = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution: '© OpenStreetMap', maxZoom: 19}).addTo(map);
  baseCtl = L.control.layers({'OpenStreetMap': OSM}, null, {position: 'topright'}).addTo(map);
  layer = L.layerGroup().addTo(map);
  if (FEAT.map.google) initGoogleBase().catch(e => console.warn('Google 底圖載入失敗，使用 OpenStreetMap：', e));
}
function loadScript(src, integrity) { return new Promise((ok, no) => { const s = document.createElement('script'); s.src = src; s.async = true; if (integrity) { s.integrity = integrity; s.crossOrigin = 'anonymous'; } s.onload = ok; s.onerror = () => no(new Error('載入失敗：' + src)); document.head.appendChild(s); }); }
async function initGoogleBase() {
  const k = (await api('/api/mapkey')).key;
  await loadScript('https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(k) + '&v=weekly&loading=async&language=zh-TW');
  await loadScript('https://unpkg.com/leaflet.gridlayer.googlemutant@0.14.1/dist/Leaflet.GoogleMutant.js', 'sha384-KjtN2k5BmItwm/9RNbodjINlsn1Z6ylCyF1JfHLtZIwhfNmZuxCn8pGJOXGReEJr');
  for (let i = 0; i < 60 && !(window.google && google.maps && google.maps.Map); i++) await new Promise(f => setTimeout(f, 100));
  if (!(window.google && google.maps && google.maps.Map && L.gridLayer.googleMutant)) throw new Error('google.maps 沒就緒');
  const road = L.gridLayer.googleMutant({type: 'roadmap', maxZoom: 20}), sat = L.gridLayer.googleMutant({type: 'hybrid', maxZoom: 20});
  baseCtl.addBaseLayer(road, 'Google 街道'); baseCtl.addBaseLayer(sat, 'Google 衛星');
  map.removeLayer(OSM); road.addTo(map);
}
window.gm_authFailure = () => { $('#tinfo').textContent = 'Google 底圖授權失敗（請檢查金鑰的網站限制），已改用 OpenStreetMap'; if (map && OSM && !map.hasLayer(OSM)) OSM.addTo(map); };

let trackReq = 0;
async function loadTrack() {
  const req = ++trackReq, d = $('#date').value;
  $('#tinfo').textContent = '載入中…';
  let t;
  try { t = await api('/api/track?date=' + encodeURIComponent(d)); } catch (e) { if (req === trackReq) { $('#tinfo').textContent = ''; errBox($('#segs'), e); } return; }
  if (req !== trackReq) return;
  layer.clearLayers();
  if (isNA(t)) { showNA('commute-na', t); $('#tinfo').textContent = ''; $('#segs').textContent = '（尚無資料）'; return; }
  showNA('commute-na', null);
  const segs = t.segs || [];
  const moves = segs.filter(s => s.kind === 'move').map(s => [s.a + ':00', s.b + ':59', MODE_COLOR[s.mode] || '#5b6b8c']);
  const colorAt = tt => { const m = moves.find(([x, y]) => tt >= x && tt <= y); return m ? m[2] : '#5b6b8c'; };
  const main = t.points.filter(p => p.src === 'main');
  let cur = [], col = null; const chunks = [];
  main.forEach(p => {
    const c = colorAt(p.t);
    if (col !== null && c !== col) { if (cur.length > 1) chunks.push([cur, col]); cur = [cur[cur.length - 1]]; }
    col = c; cur.push([p.lat, p.lon]);
  });
  if (cur.length > 1) chunks.push([cur, col]);
  chunks.forEach(([c, k]) => { L.polyline(c, {color: '#fff', weight: 8, opacity: .95}).addTo(layer); L.polyline(c, {color: k, weight: 4}).addTo(layer); });
  const sub = t.points.filter(p => p.src === 'sub').map(p => [p.lat, p.lon]);
  if (sub.length) L.polyline(sub, {color: '#c0392b', weight: 1.5, opacity: .35, dashArray: '3 7'}).addTo(layer);
  segs.filter(s => s.kind === 'stay').forEach(s => {
    L.marker([s.lat, s.lon], {icon: L.divIcon({className: 'stayic', html: '<span>' + esc(s.icon || '📍') + '</span>', iconSize: [28, 28], iconAnchor: [14, 14]})})
      .bindTooltip(esc(s.a + '–' + s.b + ' ' + s.place + '（' + s.mins + ' 分）')).addTo(layer);
  });
  t.points.forEach((p, i) => { if (i % 6 === 0 || i === t.points.length - 1) L.circleMarker([p.lat, p.lon], {radius: 2, color: p.src === 'main' ? '#3b4a6b' : '#c0392b', opacity: .5, fillOpacity: .5}).bindTooltip(esc(p.t + ' ±' + Math.round(p.acc) + 'm')).addTo(layer); });
  const all = t.points.map(p => [p.lat, p.lon]);
  if (all.length) map.fitBounds(L.latLngBounds(all).pad(0.1));
  if (t.now) L.marker([t.now.lat, t.now.lon], {icon: L.divIcon({className: 'stayic', html: '<span>📍</span>', iconSize: [28, 28], iconAnchor: [14, 26]})}).bindTooltip('現在 ' + esc(t.now.t.slice(0, 5))).addTo(layer);
  $('#tinfo').textContent = t.points.length ? t.points.length + ' 個定位點（實線＝主要裝置、虛線＝其他裝置）' : '這天沒有定位點';
  renderSegs(segs);
}
function renderSegs(segs) {
  const el = $('#segs'); el.innerHTML = '';
  if (!segs.length) { el.textContent = '（這天沒有軌跡）'; return; }
  segs.forEach(s => {
    const d = document.createElement('div');
    if (s.kind === 'stay') {
      d.className = 'ev stay';
      const dur = s.mins >= 60 ? Math.floor(s.mins / 60) + ' 小時 ' + (s.mins % 60 ? s.mins % 60 + ' 分' : '') : s.mins + ' 分';
      d.innerHTML = '<span class="ic">' + esc(s.icon || '📍') + '</span><span class="t">' + esc(s.a + '–' + s.b) + '</span><span class="b">' + esc(s.place) + ' <small>停 ' + esc(dur) + '</small></span>';
    } else {
      d.className = 'ev ' + esc(s.mode);
      d.innerHTML = '<span class="ic">' + (MODE_ICON[s.mode] || '•') + '</span><span class="t">' + esc(s.a + '–' + s.b) + '</span><span class="b">' + esc(s.label) + ' <small>' + esc(s.km) + ' km・' + esc(s.mins) + ' 分・' + esc(s.kmh) + ' km/h</small></span>';
    }
    el.appendChild(d);
  });
}
$('#btn-track').onclick = loadTrack;
$('#date').onchange = loadTrack;

// ── 美食：骰子／附近／美食庫 ──
let districtsLoaded = false;
async function fillFoodSelects() {
  if (!FEAT) return;
  for (const sel of ['#place', '#lnear']) {
    const el = $(sel);
    if (el.dataset.filled) continue;
    el.dataset.filled = '1';
    FEAT.places.forEach(p => { const o = document.createElement('option'); o.value = p; o.textContent = p; el.appendChild(o); });
  }
  if (districtsLoaded) return;
  districtsLoaded = true;
  try {
    const ds = await api('/api/food/districts');
    if (isNA(ds)) return;
    ds.forEach(x => { const o = document.createElement('option'); o.value = x.district; o.textContent = x.district + '（' + x.n + '）'; $('#ddist').appendChild(o); });
  } catch (e) { /* 沒有行政區資料就算了 */ }
}
function tierName(t) { return {daily: '平價', mid: '吃好一點', high: '高價'}[t] || t; }
function openText(o) { return o === true ? '營業中' : o === false ? '現在沒開' : '營業時間不明'; }

async function roll() {
  const out = $('#diceout'); out.className = 'pick muted'; out.textContent = '骰子滾動中…';
  const p = new URLSearchParams({place: $('#place').value, district: $('#ddist').value, tier: $('#dtier').value, price: $('#price').value,
    cat: $('#cat').value.trim(), drinks: $('#drinks').checked ? '1' : '', open: $('#dopen').checked ? '1' : '0'});
  let r;
  try { r = await api('/api/food/dice?' + p); } catch (e) { errBox(out, e); return; }
  if (isNA(r)) { naBox(out, r); return; }
  if (!r.pick) { out.textContent = '符合條件的店 0 家（' + r.note + '）。放寬一點條件試試。'; return; }
  const x = r.pick; out.className = 'pick';
  out.innerHTML = '<div class="nm">' + esc(x.name) + '</div><div>' + esc(x.cat) + '・' + esc(tierName(x.tier)) + (x.price ? '・約 ' + esc(x.price) + ' 元' : '') +
    (x.rating ? '・' + esc(x.rating) + '★' : '') + (x.dist != null ? '・' + esc(x.dist) + ' 公尺' : '') + '</div>' +
    '<div>' + esc(openText(x.open)) + (x.hours ? '：' + esc(x.hours) : '') + '</div><div class="muted">' + esc(x.addr) + (x.note ? '・' + esc(x.note) : '') + '</div>' +
    '<div class="muted">候選池 ' + esc(r.pool) + ' 家・' + esc(r.note) + '</div><div class="row" style="margin-top:8px"><button class="btn alt" id="btn-tried">我吃了這家</button><span class="muted" id="triedmsg"></span></div>';
  $('#btn-tried').onclick = async () => {
    try { await post('/api/food/tried', {id: x.id}); $('#triedmsg').textContent = '已記錄為今天吃過'; $('#btn-tried').disabled = true; } catch (e) { $('#triedmsg').textContent = e.message; }
  };
}
$('#btn-roll').onclick = roll;

async function near() {
  const out = $('#nearout'); out.textContent = '查詢中…';
  let r;
  try { r = await api('/api/food/near?radius=' + encodeURIComponent($('#radius').value) + '&open=' + ($('#opennow').checked ? '1' : '0')); } catch (e) { errBox(out, e); return; }
  if (isNA(r)) { naBox(out, r); return; }
  if (!r.rows.length) { out.textContent = r.radius + ' 公尺內沒有符合的店（' + r.note + '）'; return; }
  out.innerHTML = '<div class="muted">' + esc(r.note) + '，半徑 ' + esc(r.radius) + ' 公尺，共 ' + esc(r.total) + ' 家' + (r.total > r.rows.length ? '（列最近 ' + r.rows.length + ' 家）' : '') + '</div>' +
    r.rows.map(x => '<div class="item"><div class="n">' + esc(x.name) + ' <span class="muted">' + esc(x.cat) + '・' + esc(x.dist) + ' 公尺</span></div><div class="m">' +
      esc(openText(x.open)) + (x.hours ? '：' + esc(x.hours) : '') + (x.price ? '・約 ' + esc(x.price) + ' 元' : '') + (x.rating ? '・' + esc(x.rating) + '★' : '') + '</div></div>').join('');
}
$('#btn-near').onclick = near;

let curCat = '', curTier = '', chipsOpen = false;
const TIERS = [['', '全部', 'all'], ['daily', '平價', 'daily'], ['mid', '吃好一點', 'mid'], ['high', '高價', 'high']];
function lparams() { return new URLSearchParams({near: $('#lnear').value, radius: $('#lradius').value, q: $('#lq').value, tried: $('#ltried').checked ? '1' : '', cat: curCat, tier: curTier}); }
async function loadDb() {
  const box = $('#stats');
  try {
    const s = await api('/api/food/stats');
    if (isNA(s)) { naBox(box, s); return; }
    const st = (k, v) => '<span class="stat"><b>' + esc(v) + '</b>' + esc(k) + '</span>';
    box.innerHTML = st('店家', s.places) + st('有營業時間', s.with_hours) + st('吃過', s.tried) + st('類別數', s.categories) + st('已排除', s.excluded);
  } catch (e) { errBox(box, e); return; }
  loadCats(); loadAte();
}
async function loadCats() {
  let c;
  try { c = await api('/api/food/cats?' + lparams()); } catch (e) { errBox($('#linfo'), e); return; }
  if (isNA(c)) return;
  const LIMIT = 12, tc = c.tiers || {};
  $('#tiers').innerHTML = TIERS.map(([k, n, ck]) => '<span class="chip ' + (k === curTier ? 'on' : '') + '" data-t="' + k + '">' + esc(n) + ' ' + esc(tc[ck] ?? '') + '</span>').join('');
  document.querySelectorAll('#tiers .chip').forEach(ch => { ch.onclick = () => { curTier = ch.dataset.t; curCat = ''; chipsOpen = false; loadCats(); }; });
  $('#chips').innerHTML = '<span class="chip ' + (curCat ? '' : 'on') + '" data-c="">全部 ' + esc(c.total) + '</span>' +
    c.cats.map(([k, n], i) => '<span class="chip ' + (k === curCat ? 'on' : '') + (i >= LIMIT && k !== curCat ? ' more' : '') + '" data-c="' + esc(k) + '">' + esc(k) + ' ' + esc(n) + '</span>').join('') +
    (c.cats.length > LIMIT ? '<span class="chip" id="chiptog">' + (chipsOpen ? '收起 ▴' : '更多 ' + (c.cats.length - LIMIT) + ' ▾') + '</span>' : '');
  $('#chips').classList.toggle('open', chipsOpen);
  document.querySelectorAll('#chips .chip[data-c]').forEach(ch => { ch.onclick = () => { curCat = ch.dataset.c; loadCats(); }; });
  const tg = $('#chiptog'); if (tg) tg.onclick = () => { chipsOpen = !chipsOpen; loadCats(); };
  loadList();
}
async function loadList() {
  $('#linfo').textContent = '載入中…';
  let r;
  try { r = await api('/api/food/list?' + lparams()); } catch (e) { errBox($('#linfo'), e); return; }
  if (isNA(r)) return;
  const tb = $('#ltable tbody');
  tb.innerHTML = r.rows.map(x => '<tr><td>' + esc(x.name) + '</td><td>' + esc(x.cat) + '</td><td class="r">' + (x.dist == null ? '—' : esc(x.dist) + ' m') + '</td><td>' +
    (x.rating ? esc(x.rating) + '★(' + esc(x.reviews || 0) + ')' : '') + '</td><td class="r">' + esc(x.price ?? '') + '</td><td>' + esc(x.hours) + '</td><td>' +
    (x.tried ? (x.mine ? esc(x.mine) + '★ ' : '') + esc(x.tried) : '') + '</td><td>' + esc(x.addr) + '</td></tr>').join('');
  $('#lcards').innerHTML = r.rows.map(x => '<div class="item"><div class="n">' + esc(x.name) + ' <span class="muted">' + esc(x.cat) + (x.dist == null ? '' : '・' + esc(x.dist) + 'm') + '</span></div><div class="m">' +
    (x.rating ? esc(x.rating) + '★' : '') + (x.price ? '・' + esc(x.price) + ' 元' : '') + (x.tried ? '・吃過' : '') + '</div></div>').join('');
  $('#linfo').textContent = r.total + ' 家' + (r.total > 300 ? '（只列前 300，用關鍵字或分類縮小）' : '') + '，依距離／評論數排序｜' + r.note;
}
async function loadAte() {
  const box = $('#ate');
  try {
    const a = await api('/api/food/ate');
    if (isNA(a)) { naBox(box, a); return; }
    box.innerHTML = a.total ? '<div class="muted">共 ' + esc(a.total) + ' 家；最常吃的類別：' + a.top_cats.map(([k, n]) => esc(k) + ' ' + esc(n)).join('、') + '</div>' +
      a.rows.slice(0, 30).map(x => '<div class="item"><div class="n">' + esc(x.name) + ' <span class="muted">' + esc(x.cat) + '</span></div><div class="m">' + esc(x.tried) + (x.mine ? '・' + esc(x.mine) + '★' : '') + '</div></div>').join('') : '<span class="muted">還沒有吃過的紀錄。在骰子頁按「我吃了這家」，或直接改資料庫的 tried_at 欄位。</span>';
  } catch (e) { errBox(box, e); }
}
$('#btn-cats').onclick = () => { curCat = ''; loadCats(); };

// ── 自訂分頁：依設定檔宣告的唯讀卡片（資料來自 Home Assistant）──
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined && text !== null) n.textContent = String(text); return n; }
function svg(tag, attrs) { const n = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.keys(attrs || {}).forEach(k => n.setAttribute(k, String(attrs[k]))); return n; }
function safeLink(url) {
  try { const u = new URL(url); if (u.protocol === 'http:' || u.protocol === 'https:') return u.href; } catch (e) { /* 不合法就不顯示連結 */ }
  return null;
}
function fmtTime(ms) { const d = new Date(ms); return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); }
function entityValue(e) {
  const v = el('span', 'cval' + (e.available ? '' : ' off'));
  v.textContent = e.display;
  if (e.available && e.unit && e.value !== null) { const u = el('small', 'cunit', ' ' + e.unit); v.appendChild(u); }
  return v;
}
function valueBody(c) {
  const g = el('div', 'ctiles');
  c.entities.forEach(e => {
    const t = el('div', 'ctile');
    t.appendChild(el('div', 'cname', (e.icon ? e.icon + ' ' : '') + e.name));
    t.appendChild(entityValue(e));
    if (e.changed) t.appendChild(el('div', 'muted', '更新 ' + e.changed));
    t.title = e.id;
    g.appendChild(t);
  });
  return g;
}
function listBody(c) {
  const box = el('div', 'tablewrap'), t = el('table'), tb = el('tbody');
  c.entities.forEach(e => {
    const tr = el('tr'), a = el('td'), b = el('td', 'r');
    a.appendChild(document.createTextNode((e.icon ? e.icon + ' ' : '') + e.name));
    a.appendChild(el('div', 'muted', e.id));
    b.appendChild(entityValue(e));
    tr.appendChild(a); tr.appendChild(b); tb.appendChild(tr);
  });
  t.appendChild(tb); box.appendChild(t);
  if (c.total > c.entities.length) box.appendChild(el('div', 'muted', '共 ' + c.total + ' 個符合，只列前 ' + c.entities.length + ' 個（可在卡片設定調整 limit，最多 50）'));
  if (!c.entities.length) box.appendChild(el('div', 'muted', '沒有符合條件的實體。'));
  return box;
}
function chartBody(c) {
  const box = el('div'), ch = c.chart, pts = ch.points || [];
  box.appendChild(el('div', 'cname', (c.entity.icon ? c.entity.icon + ' ' : '') + c.entity.name + '・過去 ' + c.hours + ' 小時'));
  if (pts.length < 2) { box.appendChild(el('div', 'muted', '這段時間沒有足夠的數值資料（只有 ' + ch.count + ' 筆）。')); return box; }
  const W = 480, H = 190, L = 56, R = 10, T = 12, B = 28;
  let lo = ch.min, hi = ch.max; if (lo === hi) { lo -= 1; hi += 1; }
  const x = t => L + (t - ch.start_ms) / Math.max(ch.end_ms - ch.start_ms, 1) * (W - L - R), y = v => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const s = svg('svg', {viewBox: '0 0 ' + W + ' ' + H, class: 'cchart', role: 'img'});
  s.appendChild(svg('line', {x1: L, y1: T, x2: L, y2: H - B, stroke: '#9aa7c7', 'stroke-width': 1}));
  s.appendChild(svg('line', {x1: L, y1: H - B, x2: W - R, y2: H - B, stroke: '#9aa7c7', 'stroke-width': 1}));
  s.appendChild(svg('polyline', {points: pts.map(p => x(p[0]).toFixed(1) + ',' + y(p[1]).toFixed(1)).join(' '), fill: 'none', stroke: '#3b4a6b', 'stroke-width': 2, 'stroke-linejoin': 'round'}));
  const lab = (txt, ax, ay, anchor) => { const t = svg('text', {x: ax, y: ay, 'font-size': 15, fill: '#8893ad', 'text-anchor': anchor}); t.textContent = txt; s.appendChild(t); };
  const dec = (c.decimals === null || c.decimals === undefined) ? null : c.decimals;
  const f = v => dec !== null ? v.toFixed(dec) : (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(1)).replace(/\.0$/, '');
  lab(f(ch.max === ch.min ? hi : ch.max), L - 6, T + 10, 'end'); lab(f(ch.max === ch.min ? lo : ch.min), L - 6, H - B, 'end');
  lab(fmtTime(ch.start_ms), L, H - 6, 'start'); lab(fmtTime(ch.end_ms), W - R, H - 6, 'end');
  const last = pts[pts.length - 1]; s.appendChild(svg('circle', {cx: x(last[0]).toFixed(1), cy: y(last[1]).toFixed(1), r: 3.5, fill: '#e0a336'}));
  box.appendChild(s);
  const unit = c.entity.unit ? ' ' + c.entity.unit : '';
  box.appendChild(el('div', 'muted', '最新 ' + f(ch.last === null || ch.last === undefined ? last[1] : ch.last) + unit + '・最低 ' + f(ch.min) + '・最高 ' + f(ch.max) + '・共 ' + ch.count + ' 筆' + (ch.count > pts.length ? '（為了繪圖縮減成 ' + pts.length + ' 點，最高、最低與最新值保留原值）' : '')));
  return box;
}
function customCard(c) {
  const card = el('div', 'card ccard');
  const h = el('h2', null, (c.icon ? c.icon + ' ' : '') + c.title);
  card.appendChild(h);
  if (c.note) card.appendChild(el('div', 'muted cnote', c.note));
  if (c.error) card.appendChild(el('div', 'err', c.error));
  else card.appendChild(c.type === 'value' ? valueBody(c) : c.type === 'list' ? listBody(c) : chartBody(c));
  const href = c.link ? safeLink(c.link) : null;
  if (href) { const a = el('a', 'clink', '開啟連結 ↗'); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; card.appendChild(a); }
  return card;
}
async function loadCustom() {
  const box = $('#custom-cards');
  let d;
  try { d = await api('/api/custom'); } catch (e) { box.textContent = ''; errBox(box, e); return; }
  if (isNA(d)) { showNA('custom-na', d); box.textContent = ''; $('#custom-note').textContent = ''; return; }
  showNA('custom-na', null);
  box.textContent = '';
  d.cards.forEach(c => box.appendChild(customCard(c)));
  $('#custom-note').textContent = '讀取 ' + d.fetched + '（伺服器端快取 ' + d.cache_seconds + ' 秒）';
}
$('#btn-custom').onclick = loadCustom;

// ── 捷運：列車到站倒數／車廂擁擠度 ──
// 時間規則：伺服器對每一筆資料都給「還能新鮮幾秒」（expires_in）與倒數剩餘秒數（seconds）。前端以「這個請求送出的時刻」為起點計算經過時間
// （所以傳輸與等待的延遲也算進去，只會偏保守），沒有新回應時，每秒自己撤下過期的值：倒數歸零顯示「等待更新」，超過期限顯示「資料已過期」，
// 「列車進站」也有期限。到站與擁擠度各自完成、各自渲染；舊回應不會覆蓋新的一輪；請求有逾時，逾時就撤下舊資料。
const METRO_TIMEOUT_MS = 20000;
let metroSeq = 0;
function clockNow() { return {p: performance.now(), d: Date.now()}; }
function elapsedSince(t0) { const c = clockNow(); return Math.max(0, (c.p - t0.p) / 1000, (c.d - t0.d) / 1000); }   // 取兩種時鐘較大者：電腦睡眠時 performance.now 可能停住
function mmss(sec) { const m = Math.floor(sec / 60), x = sec % 60; return m + ':' + String(x).padStart(2, '0'); }
function paintArrival(n) {
  const dt = elapsedSince(n._t0), st = n.dataset.state, exp = n.dataset.exp === undefined ? null : Number(n.dataset.exp);
  let text, cls = 'mtime';
  if (st === 'stale' || (exp !== null && dt >= exp)) { text = st === 'stale' ? n.dataset.text : '資料已過期'; cls += ' odd stale'; n.dataset.dead = '1'; }
  else if (st === 'countdown') {
    const left = Math.ceil(Number(n.dataset.sec) - dt);
    if (left > 0) { text = mmss(left); if (left < 90) cls += ' soon'; } else { text = '等待更新'; cls += ' odd'; }   // 歸零不會變成「列車進站」
  } else if (st === 'arriving') { text = '列車進站'; cls += ' soon'; }
  else { text = n.dataset.text; cls += ' odd'; }
  n.className = cls; n.textContent = text;
}
function paintTrain(n) {
  const expired = elapsedSince(n._t0) >= Number(n.dataset.exp);
  n.hidden = expired;
  if (expired) n.dataset.dead = '1';
}
function metroTickAll() {
  const arr = $('#metro-arr'), cr = $('#metro-crowd');
  if (arr) {
    arr.querySelectorAll('.mtime').forEach(paintArrival);
    arr.querySelectorAll('.mstation').forEach(st => {
      const spans = Array.from(st.querySelectorAll('.mtime')), notice = st.querySelector('.mnotice');
      if (notice) notice.hidden = !(spans.length && spans.every(n => n.dataset.dead === '1'));
    });
  }
  if (cr) {
    cr.querySelectorAll('.mtrain').forEach(paintTrain);
    cr.querySelectorAll('.mstation').forEach(st => {
      const note = st.querySelector('.mhidden'); if (!note) return;
      const extra = Array.from(st.querySelectorAll('.mtrain')).filter(n => n.dataset.dead === '1').length, total = Number(note.dataset.base) + extra;
      note.hidden = total === 0;
      note.textContent = '另有 ' + total + ' 列的資料超過 ' + note.dataset.mins + ' 分鐘沒更新（或時間不明），未顯示。';
    });
  }
}
setInterval(metroTickAll, 1000);
function arrivalSpan(t, t0) {
  const n = el('span', 'mtime');
  n._t0 = t0; n.dataset.state = t.state; n.dataset.text = t.text || '';
  if (t.state === 'countdown') n.dataset.sec = String(t.seconds);
  if (t.expires_in !== undefined && t.expires_in !== null) n.dataset.exp = String(t.expires_in);
  paintArrival(n);
  return n;
}
function renderArrivals(d, t0) {
  const box = $('#metro-arr');
  box.textContent = ''; box.className = '';
  $('#metro-note').textContent = '讀取 ' + d.fetched + '（資料超過 ' + Math.round(d.max_age / 60) + ' 分鐘沒更新會標過期）';
  d.stations.forEach(st => {
    const sec = el('div', 'mstation'), h = el('h3', null, st.name);
    if (st.updated) h.appendChild(el('small', 'muted', ' 資料時間 ' + st.updated));
    sec.appendChild(h);
    if (!st.found) { sec.appendChild(el('div', 'muted', '這個站名在捷運 API 的資料裡找不到：請確認 metro.stations 的站名拼法（例如「台北車站」）。')); box.appendChild(sec); return; }
    const notice = el('div', 'err mnotice', '這個站的資料已過期或時間不明，暫無即時資訊（捷運資料來源可能停止更新）。');
    notice.hidden = !st.all_stale; sec.appendChild(notice);
    st.directions.forEach(dir => {
      const row = el('div', 'mdir');
      row.appendChild(el('span', 'to', '往 ' + dir.to));
      dir.trains.forEach((t, i) => {
        if (i) row.appendChild(el('span', 'muted', '／'));
        row.appendChild(arrivalSpan(t, t0));
        if (t.train) row.appendChild(el('span', 'muted', '車次 ' + t.train));
      });
      if (dir.ambiguous_line) row.appendChild(el('span', 'muted', '（同終點有 ' + dir.count + ' 筆，路線未區分，不一定是同一路線的下一班）'));
      sec.appendChild(row);
    });
    box.appendChild(sec);
  });
  box.appendChild(el('div', 'muted', d.note));
  metroTickAll();
}
function carBox(lv, i, lt) {
  const known = lv !== null && lv !== undefined && Object.prototype.hasOwnProperty.call(lt, lv);
  const c = el('span', 'car' + (known ? ' lv' + lv : ''));
  c.title = '等級 ' + (lv === null || lv === undefined ? '無資料' : lv) + (known ? '（依資料推斷：' + lt[lv] + '）' : '');
  c.appendChild(document.createTextNode(String(i + 1))); c.appendChild(el('br'));
  c.appendChild(document.createTextNode(lv === null || lv === undefined ? '–' : (known ? lt[lv] : '等級' + lv)));
  return c;
}
function renderCrowding(d, t0) {
  const box = $('#metro-crowd'), lt = d.level_text || {};
  box.textContent = ''; box.className = '';
  (d.errors || []).forEach(e => box.appendChild(el('div', 'err', e)));
  d.stations.forEach(st => {
    const sec = el('div', 'mstation'); sec.appendChild(el('h3', null, st.name));
    if (!st.found) sec.appendChild(el('div', 'muted', '在車站代碼表裡找不到這個站名。'));
    else if (!st.covered) sec.appendChild(el('div', 'muted', '這個站所在的路線沒有車廂擁擠度資料（API 只提供高運量與文湖線）。'));
    else {
      if (!st.trains.length) sec.appendChild(el('div', 'muted', '目前附近沒有新鮮的列車擁擠度資料。'));
      st.trains.forEach(t => {
        const tr = el('div', 'mtrain'), hd = el('div', 'hd');
        tr._t0 = t0; tr.dataset.exp = String(t.expires_in === undefined || t.expires_in === null ? 0 : t.expires_in);
        hd.appendChild(el('b', null, t.line));
        hd.appendChild(document.createTextNode(' 車次 ' + t.train + (t.cars ? '（車號 ' + t.cars + '）' : '') + '・在 ' + (t.at_name || t.at || '—') +
          (t.offset ? '（站碼 ' + (t.offset > 0 ? '+' : '−') + Math.abs(t.offset) + '）' : '（本站）') + (t.direction ? '・' + t.direction : '') + ' '));
        hd.appendChild(el('span', 'muted', t.updated + (t.age !== null && t.age !== undefined ? '（資料已 ' + t.age + ' 秒，載入時）' : '')));
        tr.appendChild(hd);
        const cars = el('div', 'cars'); t.levels.forEach((lv, i) => cars.appendChild(carBox(lv, i, lt))); tr.appendChild(cars);
        sec.appendChild(tr);
      });
      const note = el('div', 'muted mhidden'); note.dataset.base = String(st.stale_hidden || 0); note.dataset.mins = String(Math.round(d.max_age / 60));
      sec.appendChild(note);
    }
    box.appendChild(sec);
  });
  box.appendChild(el('div', 'muted', d.nearby_note + '（目前設定前後 ' + d.nearby + ' 站內）'));
  $('#metro-levelnote').textContent = d.level_note || '';
  metroTickAll();
}
async function metroFetch(url) {
  const ctl = new AbortController(), timer = setTimeout(() => ctl.abort(), METRO_TIMEOUT_MS);
  try { return await api(url, {signal: ctl.signal}); }
  catch (e) { if (ctl.signal.aborted) throw new Error('連線逾時（超過 ' + METRO_TIMEOUT_MS / 1000 + ' 秒沒有回應），舊資料已撤下'); throw e; }
  finally { clearTimeout(timer); }
}
async function metroPart(seq, url, sel, fn) {
  const box = $(sel), t0 = clockNow();                      // t0＝請求送出的時刻（保守：把傳輸與等待時間也算進去）
  let d;
  try { d = await metroFetch(url); }
  catch (e) { if (seq === metroSeq) { box.textContent = ''; errBox(box, e); } return; }
  if (seq !== metroSeq) return;                              // 已經開始了更新的一輪：舊回應丟掉，不覆蓋新結果
  if (isNA(d)) { showNA('metro-na', d); box.textContent = ''; return; }
  showNA('metro-na', null);
  fn(d, t0);                                                 // 到站與擁擠度各自完成就各自渲染，不互相等待
}
async function loadMetro() {
  const seq = ++metroSeq;
  await Promise.all([metroPart(seq, '/api/metro/arrivals', '#metro-arr', renderArrivals), metroPart(seq, '/api/metro/crowding', '#metro-crowd', renderCrowding)]);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) { metroTickAll(); if ($('#metro').classList.contains('on')) loadMetro(); } });   // 頁面恢復：立刻撤下過期值並重讀
$('#btn-metro').onclick = loadMetro;

init();
