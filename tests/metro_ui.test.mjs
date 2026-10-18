// 「捷運」分頁的前端時間行為：用假 DOM＋可控制的時鐘，真的執行 app.js 裡的函式（不只檢查字串）。
// 情境來自複核報告 N8：資料過期要自己撤下、另一支 API 慢回不能讓倒數偏多、舊回應不能覆蓋新結果、逾時要撤下舊資料。
import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC = fs.readFileSync(path.join(HERE, '..', 'static', 'app.js'), 'utf8');

class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.hidden = false; this.className = ''; this._text = ''; this.title = ''; }
  appendChild(c) { this.children.push(c); return c; }
  set textContent(v) { this.children = []; this._text = String(v); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  get classList() {
    const self = this;
    return {contains: c => self.className.split(/\s+/).includes(c), add: c => { if (!self.className.split(/\s+/).includes(c)) self.className = (self.className + ' ' + c).trim(); },
      remove: c => { self.className = self.className.split(/\s+/).filter(x => x && x !== c).join(' '); }};
  }
  walk(f) { f(this); this.children.forEach(c => c.walk(f)); }
  querySelectorAll(sel) { const out = [], cls = sel.slice(1); this.children.forEach(c => c.walk(n => { if (n.classList.contains(cls)) out.push(n); })); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}

function makeEnv() {
  const clock = {p: 1000, d: 1_800_000_000_000};
  const nodes = {};
  for (const id of ['#metro-arr', '#metro-crowd', '#metro-note', '#metro-levelnote', '#metro', '#btn-metro', '#metro-na']) nodes[id] = new Node('div');
  nodes['#metro'].className = 'tab on';
  const timers = [], listeners = {}, calls = [], pending = [];
  const document = {hidden: false, createElement: t => new Node(t), createTextNode: t => { const n = new Node('#text'); n._text = String(t); return n; },
    addEventListener: (ev, fn) => { listeners[ev] = fn; }};
  class AbortController {
    constructor() { const ls = []; this.signal = {aborted: false, addEventListener: (e, f) => ls.push(f)}; this.abort = () => { this.signal.aborted = true; ls.forEach(f => f()); }; }
  }
  const api = (url, opt) => new Promise((resolve, reject) => {
    const rec = {url, resolve, reject, t: clock.p}; pending.push(rec); calls.push(url);
    if (opt && opt.signal) opt.signal.addEventListener('abort', () => reject(new Error('aborted')));
  });
  const errBox = (box, e) => { const n = new Node('div'); n.className = 'err'; n._text = '讀取失敗：' + (e.message || e); box.appendChild(n); };
  const ctx = vm.createContext({document, performance: {now: () => clock.p}, Date: {now: () => clock.d}, AbortController, api, errBox,
    $: sel => nodes[sel], isNA: d => d && d.configured === false, showNA: () => {}, Math, Number, String, Array, Object, Promise, Boolean,
    setInterval: (f, ms) => { timers.push({f, ms, kind: 'interval'}); return timers.length; },
    setTimeout: (f, ms) => { timers.push({f, ms, kind: 'timeout'}); return timers.length; }, clearTimeout: () => {}});
  const elSrc = SRC.match(/^function el\(.*\}$/m)[0];
  const a = SRC.indexOf('// ── 捷運：'), b = SRC.indexOf("$('#btn-metro').onclick = loadMetro;") + "$('#btn-metro').onclick = loadMetro;".length;
  assert.ok(a > 0 && b > a, 'app.js 找不到捷運那一段');
  const section = SRC.slice(a, b);
  assert.ok(!section.includes('innerHTML'), '捷運畫面不可使用 innerHTML');
  vm.runInContext(elSrc + '\n' + section + '\nthis.metroTickAll = metroTickAll; this.renderArrivals = renderArrivals; this.renderCrowding = renderCrowding; this.loadMetro = loadMetro; this.clockNow = clockNow;', ctx);
  const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
  const advance = (sec, which = 'both') => { if (which !== 'wall') clock.p += sec * 1000; if (which !== 'perf') clock.d += sec * 1000; };
  const take = url => { const i = pending.findIndex(p => p.url === url); return i < 0 ? null : pending.splice(i, 1)[0]; };
  return {ctx, nodes, clock, timers, listeners, calls, pending, flush, advance, take, document};
}

const arrData = (trains, extra = {}) => Object.assign({fetched: '12:00:00', max_age: 120, note: 'n', stations: [{name: '台北車站', found: true, all_stale: false, updated: 't',
  directions: [{to: '淡水站', trains, ambiguous_line: false, count: trains.length}]}]}, extra);
const cd = (seconds, expires_in, extra = {}) => Object.assign({state: 'countdown', seconds, text: '', expires_in, train: ''}, extra);
const texts = (env, sel = '#metro-arr') => { const out = []; env.nodes[sel].walk(n => { if (n.classList.contains('mtime')) out.push(n.textContent); }); return out; };
const crowdData = (trains, hidden = 0) => ({fetched: 'x', max_age: 600, nearby: 2, nearby_note: 'n', level_text: {1: '寬鬆', 2: '適中', 3: '擁擠'}, level_note: 'note', errors: [],
  stations: [{name: '台北車站', found: true, covered: true, stale_hidden: hidden, trains}]});
const train = (n, exp) => ({line: '高運量', train: n, cars: 'a/b', at: 'R10', at_name: '台北車站', offset: 0, direction: '', levels: [1, 2, 3, 1, 9, null], updated: 'u', age: 20, expires_in: exp});

test('複核情境1：回應時資料已 110 秒、倒數剩 430 秒；沒有新回應再過 20 秒 → 已超過 120 秒期限，必須撤下', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(430, 10)]), env.ctx.clockNow());
  assert.deepEqual(texts(env), ['7:10']);
  env.advance(5); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['7:05']);                                  // 期限內：倒數照時間扣
  env.advance(15); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['資料已過期']);                            // 不是 6:50
});

test('複核情境2：「列車進站」有期限（回應時 55 秒、期限 60 秒），65 秒後不可再顯示進站', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([{state: 'arriving', seconds: 0, text: '列車進站', expires_in: 5, train: ''}]), env.ctx.clockNow());
  assert.deepEqual(texts(env), ['列車進站']);
  env.advance(65); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['資料已過期']);
});

test('複核情境3：到站先回、擁擠度慢 30 秒才回，到站倒數要扣掉這 30 秒（不是從 render 重新計時）', async () => {
  const env = makeEnv();
  const run = env.ctx.loadMetro();
  env.take('/api/metro/arrivals').resolve(arrData([cd(120, 110)]));
  await env.flush();
  assert.deepEqual(texts(env), ['2:00']);                                  // 到站不必等擁擠度，先畫出來
  env.advance(30);
  env.take('/api/metro/crowding').resolve(crowdData([train('1', 500)]));
  await env.flush(); await run;
  assert.deepEqual(texts(env), ['1:30']);                                  // 擁擠度晚到，到站的倒數已扣 30 秒
});

test('回應本身很慢：以「請求送出」為起點計時，傳輸與等待時間也扣掉（只會偏保守）', async () => {
  const env = makeEnv();
  const run = env.ctx.loadMetro();
  env.advance(30);
  env.take('/api/metro/arrivals').resolve(arrData([cd(120, 110)]));
  env.take('/api/metro/crowding').resolve(crowdData([]));
  await env.flush(); await run;
  assert.deepEqual(texts(env), ['1:30']);
});

test('倒數歸零顯示「等待更新」，絕不顯示成「列車進站」', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(5, 100)]), env.ctx.clockNow());
  env.advance(6); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['等待更新']);
  env.advance(30); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['等待更新']);
  env.advance(100); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['資料已過期']);                             // 資料期限一到，連「等待更新」也改成過期
});

test('過 120／60／600 秒期限的邊界', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(900, 120), {state: 'arriving', seconds: 0, text: '列車進站', expires_in: 60, train: ''}]), env.ctx.clockNow());
  env.advance(59); env.ctx.metroTickAll();
  assert.equal(texts(env)[1], '列車進站');
  env.advance(1); env.ctx.metroTickAll();
  assert.equal(texts(env)[1], '資料已過期');                               // 進站 60 秒到期
  assert.notEqual(texts(env)[0], '資料已過期');                            // 倒數那筆還沒到 120 秒
  env.advance(60); env.ctx.metroTickAll();
  assert.equal(texts(env)[0], '資料已過期');                               // 120 秒到期
  const e2 = makeEnv();
  e2.ctx.renderCrowding(crowdData([train('1', 600)]), e2.ctx.clockNow());
  e2.advance(599); e2.ctx.metroTickAll();
  assert.equal(e2.nodes['#metro-crowd'].querySelectorAll('.mtrain')[0].hidden, false);
  e2.advance(1); e2.ctx.metroTickAll();
  assert.equal(e2.nodes['#metro-crowd'].querySelectorAll('.mtrain')[0].hidden, true);   // 600 秒到期：撤下
});

test('只有牆上時鐘前進（電腦睡眠後 performance.now 沒走）也會判過期', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(300, 100)]), env.ctx.clockNow());
  env.advance(200, 'wall'); env.ctx.metroTickAll();
  assert.deepEqual(texts(env), ['資料已過期']);
});

test('整個站的資料都過期時顯示「暫無即時資訊」；還有活的資料就不顯示', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(300, 50), cd(400, 200)]), env.ctx.clockNow());
  const notice = () => env.nodes['#metro-arr'].querySelector('.mnotice');
  assert.equal(notice().hidden, true);
  env.advance(60); env.ctx.metroTickAll();
  assert.equal(notice().hidden, true);                                     // 還有一筆沒過期
  env.advance(150); env.ctx.metroTickAll();
  assert.equal(notice().hidden, false);
  assert.ok(notice().textContent.includes('暫無即時資訊'));
});

test('伺服器標成 stale 的資料原樣顯示過期文字，不顯示倒數', () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([{state: 'stale', seconds: null, text: '資料已過期', why: 'old', train: ''}], {}), env.ctx.clockNow());
  assert.deepEqual(texts(env), ['資料已過期']);
  const e2 = makeEnv();
  const d = arrData([{state: 'stale', seconds: null, text: '資料時間不明', why: 'no_time', train: ''}]); d.stations[0].all_stale = true;
  e2.ctx.renderArrivals(d, e2.ctx.clockNow());
  assert.deepEqual(texts(e2), ['資料時間不明']);
  assert.equal(e2.nodes['#metro-arr'].querySelector('.mnotice').hidden, false);
});

test('擁擠度：到期的列車自己撤下，並更新「另有幾列未顯示」', () => {
  const env = makeEnv();
  env.ctx.renderCrowding(crowdData([train('1', 100), train('2', 500)], 2), env.ctx.clockNow());
  const trains = () => env.nodes['#metro-crowd'].querySelectorAll('.mtrain');
  const note = () => env.nodes['#metro-crowd'].querySelector('.mhidden');
  assert.deepEqual(trains().map(t => t.hidden), [false, false]);
  assert.ok(note().textContent.includes('另有 2 列'));
  env.advance(101); env.ctx.metroTickAll();
  assert.deepEqual(trains().map(t => t.hidden), [true, false]);
  assert.ok(note().textContent.includes('另有 3 列'));
});

test('回應順序顛倒：較新的一輪先回，舊一輪後回的資料不會覆蓋', async () => {
  const env = makeEnv();
  const r1 = env.ctx.loadMetro();
  const r1Arr = env.take('/api/metro/arrivals'), r1Cr = env.take('/api/metro/crowding');
  const r2 = env.ctx.loadMetro();
  env.take('/api/metro/arrivals').resolve(arrData([cd(60, 100, {text: 'NEW'})]));
  env.take('/api/metro/crowding').resolve(crowdData([]));
  await env.flush();
  assert.deepEqual(texts(env), ['1:00']);
  r1Arr.resolve(arrData([cd(999, 100)]));                                  // 舊的一輪晚到
  r1Cr.resolve(crowdData([train('OLD', 100)]));
  await env.flush(); await r1; await r2;
  assert.deepEqual(texts(env), ['1:00']);
  assert.equal(env.nodes['#metro-crowd'].querySelectorAll('.mtrain').length, 0);
});

test('請求逾時：舊資料被撤下並顯示錯誤，而不是一直留著舊倒數', async () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(300, 100)]), env.ctx.clockNow());
  assert.equal(texts(env).length, 1);
  const run = env.ctx.loadMetro();
  const timeouts = env.timers.filter(t => t.kind === 'timeout' && t.ms === 20000);
  assert.equal(timeouts.length, 2);                                        // 到站與擁擠度各一個逾時計時器
  timeouts.forEach(t => t.f());                                            // 20 秒到了還沒回應
  await env.flush(); await run;
  assert.equal(texts(env).length, 0);
  assert.ok(env.nodes['#metro-arr'].textContent.includes('逾時'));
});

test('頁面恢復可見：立刻撤下過期值並重新讀取', async () => {
  const env = makeEnv();
  env.ctx.renderArrivals(arrData([cd(300, 100)]), env.ctx.clockNow());
  env.advance(500, 'wall');                                                // 分頁在背景、計時器被凍結
  env.document.hidden = false;
  env.listeners['visibilitychange']();
  assert.deepEqual(texts(env), ['資料已過期']);                            // 還沒等新資料回來，就先撤下
  assert.ok(env.calls.includes('/api/metro/arrivals'));                    // 同時重新讀取
});

test('站名、終點、車次含 HTML 時只會是純文字', () => {
  const env = makeEnv();
  const evil = '<img src=x onerror=alert(1)>';
  const d = arrData([cd(60, 100, {train: evil})]); d.stations[0].name = evil; d.stations[0].directions[0].to = evil;
  env.ctx.renderArrivals(d, env.ctx.clockNow());
  let bad = 0, shown = 0;
  env.nodes['#metro-arr'].walk(n => { if (/^(img|script|svg|iframe)$/i.test(n.tagName)) bad++; if (n.textContent.includes('<img')) shown++; });
  assert.equal(bad, 0);
  assert.ok(shown > 0);
});
