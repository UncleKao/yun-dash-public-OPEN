// app.js 是給瀏覽器直接載的一整份 script（載入時就會操作 DOM），沒有 export。
// 這裡不改它，改用 node:vm 只切出幾個「純函式」那幾行來測；其餘靠 node --check 與人工看畫面。
// 跑法（在專案根目錄）：node --test
import {test} from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

const SRC = fs.readFileSync(path.join(HERE, '..', 'static', 'app.js'), 'utf8');

function grab(name) {
  const m = SRC.match(new RegExp('^const ' + name + ' = .*;$', 'm'));
  assert.ok(m, `app.js 裡找不到 const ${name}`);
  return m[0];
}

const ctx = vm.createContext({});
vm.runInContext(['esc', 'fmtN', 'isNA'].map(grab).join('\n') + '\nthis.esc = esc; this.fmtN = fmtN; this.isNA = isNA;', ctx);

test('esc 跳脫 HTML 特殊字元（防止店名／備註塞進標籤）', () => {
  assert.equal(ctx.esc('<img src=x onerror=alert(1)>'), '&lt;img src=x onerror=alert(1)&gt;');
  assert.equal(ctx.esc('a&b"c\'d'), 'a&amp;b&quot;c&#39;d');
  assert.equal(ctx.esc(null), '');
  assert.equal(ctx.esc(undefined), '');
  assert.equal(ctx.esc(0), '0');
});

test('fmtN 空值顯示破折號，其餘照位數格式化', () => {
  assert.equal(ctx.fmtN(null), '—');
  assert.equal(ctx.fmtN(undefined), '—');
  assert.equal(ctx.fmtN('abc'), '—');
  assert.equal(ctx.fmtN(1234.5, 1), '1,234.5');
  assert.equal(ctx.fmtN(1234), '1,234');
});

test('isNA 只在 configured === false 時成立', () => {
  assert.equal(ctx.isNA({configured: false, reason: 'x'}), true);
  assert.equal(ctx.isNA({rows: []}), false);
  assert.equal(ctx.isNA(null), null);
});

test('index.html 引用的 id 在 app.js 裡都有被用到或存在（抽查）', () => {
  const html = fs.readFileSync(path.join(HERE, '..', 'static', 'index.html'), 'utf8');
  for (const id of ['btn-track', 'btn-roll', 'btn-near', 'btn-cats', 'btn-custom', 'btn-metro', 'custom-cards', 'segs', 'map']) {
    assert.ok(html.includes(`id="${id}"`), `index.html 缺 id=${id}`);
    assert.ok(SRC.includes(id), `app.js 沒用到 ${id}`);
  }
});

// ── 「自訂」分頁：卡片畫面（用假的 DOM 檢查：HA 來的文字一律當純文字，不會變成標籤）──
function loadCustomSection() {
  const a = SRC.indexOf('// ── 自訂分頁'), b = SRC.indexOf('async function loadCustom');
  assert.ok(a >= 0 && b > a, 'app.js 找不到自訂分頁那一段');
  const section = SRC.slice(a, b);
  assert.ok(!section.includes('innerHTML'), '自訂分頁的程式不可使用 innerHTML');
  assert.ok(!section.includes('insertAdjacentHTML') && !section.includes('outerHTML'), '自訂分頁的程式不可用任何 HTML 字串注入');
  class Node {
    constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this._text = ''; this.className = ''; }
    appendChild(c) { this.children.push(c); return c; }
    setAttribute(k, v) { this.attrs[k] = v; }
    set textContent(v) { this._text = String(v); } get textContent() { return this._text; }
    set innerHTML(v) { throw new Error('innerHTML 不可使用'); }
  }
  const doc = {createElement: t => new Node(t), createElementNS: (ns, t) => new Node(t), createTextNode: t => { const n = new Node('#text'); n._text = String(t); return n; }};
  const ctx = vm.createContext({document: doc, URL, Date, Math, String, Object, Number, Array});
  vm.runInContext(section + '\nthis.customCard = customCard; this.safeLink = safeLink;', ctx);
  return ctx;
}
function walk(n, f) { f(n); n.children.forEach(c => walk(c, f)); }

test('safeLink 只放行 http／https', () => {
  const c = loadCustomSection();
  assert.equal(c.safeLink('https://example.com/a?b=1'), 'https://example.com/a?b=1');
  assert.equal(c.safeLink('http://homeassistant.local:8123/x'), 'http://homeassistant.local:8123/x');
  for (const bad of ['javascript:alert(1)', 'data:text/html,hi', 'ftp://x', 'file:///etc/passwd', '//evil.example', 'not a url', '', 'JaVaScRiPt:alert(1)'])
    assert.equal(c.safeLink(bad), null, bad);
});

test('實體名稱、狀態值、標題、備註含 HTML 時，畫面上只會是純文字', () => {
  const c = loadCustomSection();
  const evil = '<img src=x onerror=alert(1)>';
  const ent = {id: 'sensor.a', name: evil, icon: evil, unit: evil, display: evil, available: true, value: 1, found: true, changed: evil};
  const cards = [
    {type: 'value', title: evil, icon: evil, note: evil, link: 'javascript:alert(1)', entities: [ent]},
    {type: 'list', title: evil, icon: '', note: '', link: '', entities: [ent], total: 99, limit: 1},
    {type: 'chart', title: evil, icon: '', note: '', link: '', hours: 24, entity: ent, chart: {points: [[0, 1], [1000, 2]], count: 2, min: 1, max: 2, start_ms: 0, end_ms: 1000}},
    {type: 'value', title: 't', icon: '', note: '', link: '', error: evil},
  ];
  const allowed = new Set(['div', 'h2', 'span', 'small', 'table', 'tbody', 'tr', 'td', 'a', 'svg', 'line', 'polyline', 'circle', 'text', '#text']);
  let sawEvilText = 0;
  for (const card of cards) {
    const root = c.customCard(card);
    walk(root, n => {
      assert.ok(allowed.has(n.tagName), '出現不該有的元素：' + n.tagName);   // 沒有任何元素是由資料決定的
      if (n._text.includes('<img')) sawEvilText++;
      for (const v of Object.values(n.attrs)) assert.ok(!String(v).includes('<img'), '惡意字串進了屬性');
      if (n.tagName === 'a') assert.ok(/^https?:/.test(n.href), '連結必須是 http(s)');
    });
    walk(root, n => { assert.ok(!(n.tagName === 'a' && card.link === 'javascript:alert(1)'), 'javascript: 連結不可變成 <a>'); });
  }
  assert.ok(sawEvilText > 0, '惡意字串應該以純文字出現在畫面上');
});

test('折線圖點數不足、值全相同時不會出錯', () => {
  const c = loadCustomSection();
  const ent = {id: 'sensor.a', name: 'A', icon: '', unit: '', display: '1', available: true, value: 1, found: true, changed: ''};
  const base = {type: 'chart', title: 't', icon: '', note: '', link: '', hours: 1, entity: ent};
  c.customCard(Object.assign({}, base, {chart: {points: [], count: 0, min: null, max: null, start_ms: 0, end_ms: 1}}));
  c.customCard(Object.assign({}, base, {chart: {points: [[0, 5], [10, 5]], count: 2, min: 5, max: 5, start_ms: 0, end_ms: 10}}));
});

test('折線圖的「最新」用原始最後值（ch.last），不是畫面上縮點後的最後一點；有縮點時說明極值保留原值', () => {
  const c = loadCustomSection();
  const ent = {id: 'sensor.a', name: 'A', icon: '', unit: 'W', display: '1', available: true, value: 1, found: true, changed: ''};
  const card = {type: 'chart', title: 't', icon: '', note: '', link: '', hours: 24, entity: ent, decimals: 2,
    chart: {points: [[0, 1], [500, 100], [1000, 9]], count: 5000, min: -3, max: 100, last: 7.5, start_ms: 0, end_ms: 1000}};
  const root = c.customCard(card);
  const texts = [];
  walk(root, n => { if (n._text) texts.push(n._text); });
  const summary = texts.find(t => t.startsWith('最新'));
  assert.ok(summary, '找不到摘要文字');
  assert.ok(summary.includes('最新 7.50 W'), summary);          // 7.5，不是 9（畫面最後一點）
  assert.ok(summary.includes('最低 -3.00') && summary.includes('最高 100.00'), summary);
  assert.ok(summary.includes('保留原值'), summary);
});

test('捷運畫面：過期、等待更新與路線未區分都有標示（行為測試見 metro_ui.test.mjs）', () => {
  assert.ok(SRC.includes('等待更新') && SRC.includes('路線未區分') && SRC.includes('資料已過期'));
  assert.ok(SRC.includes('dataset.exp') && SRC.includes('visibilitychange') && SRC.includes('AbortController'));
});
