"""寫入型 API 的輸入驗證（只用標準庫）。

這裡只放「檢查」本身：檢查不過就丟 Invalid，由 server.py 統一回 HTTP 400 ＋中文訊息
（`{"error": "…"}`）。合法輸入原值通過，不做修剪或改寫。
"""
import datetime
import json
import math
import re

MAX_JSON = 256 * 1024          # POST JSON 本文上限
MAX_TEXT = 200                 # 一般文字欄位長度上限
DATE_MIN = datetime.date(2000, 1, 1)    # 日期合理範圍
DATE_MAX = datetime.date(2100, 12, 31)
YM_RE = re.compile(r'20\d\d-(0[1-9]|1[0-2])')
INT_RE = re.compile(r'-?\d{1,18}')


class Invalid(ValueError):
    """輸入不合法。server.py 把它對應成 400；繼承 ValueError 讓既有的 except ValueError 也擋得住。"""


def content_length(headers, limit=None, what='內容'):
    """Content-Length → int。沒帶當 0；不是非負整數（負數、小數、重複標頭「5, 7」）就 400。"""
    raw = headers.get('Content-Length')
    if raw in (None, ''):
        return 0
    text_ = str(raw).strip()
    if not text_.isdigit() or not text_.isascii():
        raise Invalid('Content-Length 必須是非負整數')
    n = int(text_)
    if limit is not None and n > limit:
        raise Invalid(f'{what}太大（上限 {limit} 位元組）')
    return n


def json_body(raw):
    """POST 本文 → dict。空本文當 {}（原本的行為）；不是 JSON 物件就 400，不讓它到後面變成 500。"""
    if not raw or not raw.strip():
        return {}
    def _reject(const):   # NaN／Infinity 不是合法 JSON，Python 預設卻會收；一律拒絕
        raise ValueError(const)
    try:
        data = json.loads(raw, parse_constant=_reject)
    except ValueError:
        raise Invalid('JSON 格式不正確') from None
    if not isinstance(data, dict):
        raise Invalid('JSON 本文必須是物件')
    return data


def text(value, name, maxlen=MAX_TEXT, pattern=None, default=''):
    """字串欄位：型別、長度上限、可選的字元規則。None／空字串回 default。"""
    if value is None or value == '':
        return default
    if not isinstance(value, str):
        raise Invalid(f'{name}必須是字串')
    if len(value) > maxlen:
        raise Invalid(f'{name}最多 {maxlen} 個字')
    if '\x00' in value:
        raise Invalid(f'{name}不能含空字元')
    if pattern is not None and not re.fullmatch(pattern, value):
        raise Invalid(f'{name}格式不符')
    return value


def date(value, name, lo=DATE_MIN, hi=DATE_MAX):
    """YYYY-MM-DD（datetime.date.fromisoformat 認得的那種）→ 同字串，並檢查落在合理範圍。"""
    s = text(value, name, maxlen=10)
    if not s:
        raise Invalid(f'{name}不能空白')
    try:
        d = datetime.date.fromisoformat(s)
    except ValueError:
        raise Invalid(f'{name}要是 YYYY-MM-DD') from None
    if not lo <= d <= hi:
        raise Invalid(f'{name}要在 {lo.isoformat()} 到 {hi.isoformat()} 之間')
    return d.isoformat()


def ym(value, name):
    """YYYY-MM（只收 2000–2099 年）。"""
    s = text(value, name, maxlen=7)
    if not s or not YM_RE.fullmatch(s):
        raise Invalid(f'{name}要是 YYYY-MM')
    return s


def number(value, name, lo, hi):
    """數值欄位：型別（不收 True／False／list／dict）、非 NaN／Inf、上下界。"""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Invalid(f'{name}要是數字')
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise Invalid(f'{name}要是數字') from None
    if not math.isfinite(x):
        raise Invalid(f'{name}要是數字')
    if not lo <= x <= hi:
        raise Invalid(f'{name}要在 {lo:g} 到 {hi:g} 之間')
    return x


def integer(value, name, lo, hi, default=None):
    """整數欄位（例：?days=）。None／空字串回 default；不是十進位整數或超出範圍就 400。"""
    if value is None or value == '':
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise Invalid(f'{name}要是整數')
    if isinstance(value, str):
        s = value.strip()
        # 先看長度再轉：'9' * 5000 這種 int() 在 Python 3.11+ 會自己丟 ValueError
        if not s.isascii() or not INT_RE.fullmatch(s):
            raise Invalid(f'{name}要是整數')
        value = int(s)
    if not lo <= value <= hi:
        raise Invalid(f'{name}要在 {lo} 到 {hi} 之間')
    return int(value)


def path_part(value, name, maxlen=120):
    """只准當成單一檔名／目錄名用的字串：不含 ..、不含分隔符、不是絕對路徑。

    目前沒有端點讓外面指定路徑；留著是為了日後收到路徑參數時有現成的檢查可用。
    """
    s = text(value, name, maxlen=maxlen)
    if not s:
        raise Invalid(f'{name}不能空白')
    if s.startswith(('/', '\\')) or (len(s) > 1 and s[1] == ':'):
        raise Invalid(f'{name}不能是絕對路徑')
    if '/' in s or '\\' in s:
        raise Invalid(f'{name}不能含路徑分隔符')
    if s == '..' or '..' in s:
        raise Invalid(f'{name}不能含 ..')
    if s in ('.',):
        raise Invalid(f'{name}不合法')
    return s
