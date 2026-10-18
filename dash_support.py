"""共用小工具：只依賴 Python 標準庫。"""
import contextlib
import copy
import fcntl
import json
import os
import tempfile
import threading
import time
from collections import OrderedDict


class APIError(Exception):
    """可以直接轉成 HTTP 回應的錯誤（status 為 HTTP 狀態碼）。"""

    def __init__(self, message, status=500, code='internal_error', retryable=False):
        super().__init__(message)
        self.status, self.code, self.retryable = status, code, retryable


class NotConfigured(APIError):
    """功能尚未設定（缺設定檔欄位、缺資料）。回 200 與 {"configured": false, "reason": …}，不是錯誤。"""

    def __init__(self, reason):
        super().__init__(reason, 200, 'not_configured')
        self.reason = reason


class TimedCache:
    """有限大小、只保存成功結果的快取；例外不寫入。

    build() 在「單一 key 的鎖」裡執行、不在全域鎖裡：慢的查詢（例如向 HA 查 40 秒）不會卡住其他 key 的請求；
    同一個 key 的並行請求會等第一個做完、直接用它的結果（不重複打 HA）。
    """

    def __init__(self, limit=32, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.data, self.lock = OrderedDict(), threading.RLock()
        self._building = {}          # key -> 該 key 的建置鎖

    def _lookup(self, key):
        found = self.data.get(key)
        if found and found[0] > self.clock():
            self.data.move_to_end(key)
            return True, copy.deepcopy(found[1])
        return False, None

    def get(self, key, ttl, build):
        with self.lock:
            hit, value = self._lookup(key)
            if hit:
                return value
            self.data.pop(key, None)
            kl = self._building.setdefault(key, threading.Lock())
        with kl:
            with self.lock:
                hit, value = self._lookup(key)      # 等鎖期間別人可能已經做好了
            if hit:
                return value
            try:
                value = build()                     # 例外不寫入快取
            finally:
                with self.lock:
                    if self._building.get(key) is kl:
                        del self._building[key]
            with self.lock:
                self.data[key] = (self.clock() + ttl, copy.deepcopy(value))
                while len(self.data) > self.limit:
                    self.data.popitem(last=False)
            return value


_store_thread_lock = threading.RLock()


@contextlib.contextmanager
def store_lock(path):
    """跨執行緒、跨程序的檔案鎖；必須涵蓋「讀取、修改、儲存」整段。"""
    with _store_thread_lock:
        os.makedirs(os.path.dirname(path) or '.', mode=0o700, exist_ok=True)
        fd = os.open(path + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def read_json(path, default):
    """檔案不存在回 default；存在但壞掉就丟 ValueError（避免之後把壞檔覆蓋掉）。"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, ValueError):
        raise ValueError(f'資料檔 {os.path.basename(path)} 無法讀取，已停止寫入，請核對原檔或備份') from None


def atomic_json(path, data):
    """寫暫存檔、落盤、再原子取代。"""
    d = os.path.dirname(path) or '.'
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.dash-', suffix='.tmp', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
