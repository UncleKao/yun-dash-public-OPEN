"""「自訂」分頁的卡片宣告：格式檢查與正規化（純函式，不連網、不依賴其他模組）。

設定檔 custom.cards 是一個清單，每個元素是一張卡片。只支援唯讀的三種卡片（value、chart、list），
沒有任何會寫入或控制 Home Assistant 的類型。每個欄位都有格式與上限；不合法時丟 CardError，
訊息用中文指出是哪一張卡、哪個欄位、哪裡不對（啟動時就會顯示，不會等到使用時才壞）。
說明書：docs/自訂分頁說明書.md
"""
import difflib
import re
import urllib.parse

MAX_CARDS = 20            # 一個自訂分頁最多幾張卡
MAX_VALUE_ENTITIES = 12   # value 卡一次最多顯示幾個實體
MAX_LIST_ENTITIES = 50    # list 卡最多列幾行
MAX_HOURS = 168           # chart 卡最多回看 168 小時（7 天）
DEFAULT_HOURS = 24
DEFAULT_LIST_LIMIT = 30
ENTITY_RE = re.compile(r'[a-z0-9_]+\.[a-z0-9_]+')
TYPES = ('value', 'chart', 'list')
WRITE_LIKE = ('button', 'switch', 'toggle', 'service', 'script', 'scene', 'light', 'climate', 'lock', 'cover', 'action', 'control', 'input', 'call_service')
COMMON_KEYS = ('type', 'title', 'icon', 'note', 'link')
KEYS = {'value': COMMON_KEYS + ('entities',),
        'chart': COMMON_KEYS + ('entity', 'name', 'unit', 'hours', 'decimals'),
        'list': COMMON_KEYS + ('entities', 'filter', 'limit', 'sort')}
ENTITY_KEYS = ('id', 'name', 'icon', 'unit', 'decimals')
FILTER_KEYS = ('domain', 'unit', 'contains', 'exclude')
SORTS = ('value_desc', 'value_asc', 'name')


class CardError(ValueError):
    """卡片宣告有問題；訊息已經是給使用者看的中文。"""


def _hint(bad, options):
    m = difflib.get_close_matches(str(bad), options, n=1, cutoff=0.6)
    return f'（是不是想寫「{m[0]}」？）' if m else ''


def _text(v, where, field, maxlen, required=False):
    if v is None or v == '':
        if required:
            raise CardError(f'{where} 的 {field} 不能空白')
        return ''
    if not isinstance(v, str):
        raise CardError(f'{where} 的 {field} 要是文字（用雙引號包起來），目前是 {type(v).__name__}')
    if len(v) > maxlen:
        raise CardError(f'{where} 的 {field} 太長（最多 {maxlen} 個字，目前 {len(v)}）')
    if any(not c.isprintable() for c in v):
        raise CardError(f'{where} 的 {field} 含有控制字元')
    return v


def _int(v, where, field, lo, hi, default):
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int):
        raise CardError(f'{where} 的 {field} 要是整數（不要加引號），目前是 {v!r}')
    if not lo <= v <= hi:
        raise CardError(f'{where} 的 {field} 要在 {lo}～{hi} 之間，目前是 {v}')
    return v


def entity_id(v, where, field):
    if not isinstance(v, str) or not ENTITY_RE.fullmatch(v) or len(v) > 100:
        raise CardError(f'{where} 的 {field}（{str(v)[:40]!r}）不是正確的實體 ID：格式是「類型.名稱」，只能用小寫英文字母、數字與底線，例如 sensor.living_room_temperature'
                        '（到 Home Assistant 的「開發者工具 › 狀態」可以查到）')
    return v


def link(v, where):
    if v is None or v == '':
        return ''
    s = _text(v, where, 'link', 300)
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise CardError(f'{where} 的 link 只能是 http:// 或 https:// 開頭的網址')
    return s


def _entity_item(item, where, field):
    """value／list 卡的一個實體：可以只寫實體 ID 字串，或寫成 {"id": …, "name": …}。"""
    if isinstance(item, str):
        item = {'id': item}
    if not isinstance(item, dict):
        raise CardError(f'{where} 的 {field} 要是實體 ID 文字，或像 {{"id": "sensor.xxx", "name": "名稱"}} 這樣的設定')
    for k in item:
        if k not in ENTITY_KEYS:
            raise CardError(f'{where} 的 {field} 有不認得的欄位「{k}」{_hint(k, ENTITY_KEYS)}；可用的欄位：{"、".join(ENTITY_KEYS)}')
    out = {'id': entity_id(item.get('id'), where, f'{field}.id'),
           'name': _text(item.get('name'), where, f'{field}.name', 40),
           'icon': _text(item.get('icon'), where, f'{field}.icon', 16),
           'unit': _text(item.get('unit'), where, f'{field}.unit', 12),
           'decimals': _int(item.get('decimals'), where, f'{field}.decimals', 0, 6, None)}
    return out


def _filter(f, where):
    if not isinstance(f, dict):
        raise CardError(f'{where} 的 filter 要是 {{"domain": "sensor", "unit": "W"}} 這樣的設定')
    for k in f:
        if k not in FILTER_KEYS:
            raise CardError(f'{where} 的 filter 有不認得的欄位「{k}」{_hint(k, FILTER_KEYS)}；可用的欄位：{"、".join(FILTER_KEYS)}')
    out = {'domain': _text(f.get('domain'), where, 'filter.domain', 30).lower(), 'unit': _text(f.get('unit'), where, 'filter.unit', 12)}
    for k in ('contains', 'exclude'):
        v = f.get(k)
        if v is None:
            out[k] = []
            continue
        if not isinstance(v, list) or len(v) > 10:
            raise CardError(f'{where} 的 filter.{k} 要是文字清單（最多 10 個），例如 ["power"]')
        out[k] = [_text(x, where, f'filter.{k}', 40, required=True).lower() for x in v]
    if not (out['domain'] or out['unit'] or out['contains']):
        raise CardError(f'{where} 的 filter 至少要填 domain、unit、contains 其中一個，否則會把 Home Assistant 全部的實體都列出來')
    return out


def validate_card(card, index):
    """index 從 0 開始；訊息用「第 N 張」。回傳正規化後的卡片（dict）。"""
    n = index + 1
    if not isinstance(card, dict):
        raise CardError(f'custom.cards 的第 {n} 張卡要是一組設定（用 {{ }} 包起來），目前是 {type(card).__name__}')
    title = card.get('title')
    label = f'第 {n} 張卡' + (f'（「{title[:20]}」）' if isinstance(title, str) and title else '')
    where = f'custom.cards 的{label}'
    t = card.get('type')
    if t not in TYPES:
        extra = '本專案只支援唯讀的卡片，沒有會寫入或控制 Home Assistant 的類型。' if isinstance(t, str) and any(w in t.lower() for w in WRITE_LIKE) else ''
        raise CardError(f'{where} 的 type（{t!r}）不認得：只能是 {"、".join(TYPES)}。{extra}{_hint(t, TYPES) if isinstance(t, str) else ""}')
    for k in card:
        if k not in KEYS[t]:
            raise CardError(f'{where} 有不認得的欄位「{k}」{_hint(k, KEYS[t])}；{t} 卡可用的欄位：{"、".join(KEYS[t])}')
    out = {'type': t, 'title': _text(title, where, 'title', 40, required=True), 'icon': _text(card.get('icon'), where, 'icon', 16),
           'note': _text(card.get('note'), where, 'note', 200), 'link': link(card.get('link'), where)}
    if t == 'value':
        ents = card.get('entities')
        if not isinstance(ents, list) or not ents:
            raise CardError(f'{where} 的 entities 要是至少一個實體的清單，例如 ["sensor.xxx"]')
        if len(ents) > MAX_VALUE_ENTITIES:
            raise CardError(f'{where} 的 entities 太多（最多 {MAX_VALUE_ENTITIES} 個，目前 {len(ents)}）；請拆成兩張卡')
        out['entities'] = [_entity_item(e, where, f'entities[{i + 1}]') for i, e in enumerate(ents)]
    elif t == 'chart':
        if card.get('entity') is None:
            raise CardError(f'{where} 缺少 entity（要畫哪一個實體的歷史，寫一個實體 ID）')
        out.update(entity=entity_id(card.get('entity'), where, 'entity'), name=_text(card.get('name'), where, 'name', 40),
                   unit=_text(card.get('unit'), where, 'unit', 12), hours=_int(card.get('hours'), where, 'hours', 1, MAX_HOURS, DEFAULT_HOURS),
                   decimals=_int(card.get('decimals'), where, 'decimals', 0, 6, None))
    else:
        has_e, has_f = card.get('entities') is not None, card.get('filter') is not None
        if has_e == has_f:
            raise CardError(f'{where} 要在 entities（自己列出實體）與 filter（依條件挑）兩種寫法裡擇一，不能都沒有、也不能兩個都有')
        limit = _int(card.get('limit'), where, 'limit', 1, MAX_LIST_ENTITIES, DEFAULT_LIST_LIMIT)
        sort = card.get('sort')
        if sort is not None and sort not in SORTS:
            raise CardError(f'{where} 的 sort（{sort!r}）只能是 {"、".join(SORTS)}')
        out.update(limit=limit, sort=sort or '')
        if has_e:
            ents = card['entities']
            if not isinstance(ents, list) or not ents:
                raise CardError(f'{where} 的 entities 要是至少一個實體的清單')
            if len(ents) > MAX_LIST_ENTITIES:
                raise CardError(f'{where} 的 entities 太多（最多 {MAX_LIST_ENTITIES} 個，目前 {len(ents)}）')
            out['entities'] = [_entity_item(e, where, f'entities[{i + 1}]') for i, e in enumerate(ents)]
        else:
            out['filter'] = _filter(card['filter'], where)
    return out


def validate(cards):
    """custom.cards 整份檢查；回傳正規化後的清單。"""
    if cards is None:
        return []
    if not isinstance(cards, list):
        raise CardError('custom.cards 要是卡片的清單（用 [ ] 包起來）')
    if len(cards) > MAX_CARDS:
        raise CardError(f'custom.cards 太多張（最多 {MAX_CARDS} 張，目前 {len(cards)} 張）')
    return [validate_card(c, i) for i, c in enumerate(cards)]
