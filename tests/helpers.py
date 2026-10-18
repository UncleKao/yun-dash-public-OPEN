"""測試共用：把設定換成臨時目錄裡的一份，測完還原成預設值。"""
import contextlib
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dash_config as C   # noqa: E402
import dash_custom   # noqa: E402


@contextlib.contextmanager
def config(overrides=None):
    """產生臨時資料夾與設定檔並載入；yield 資料夾路徑（data 在其下的 data/）。"""
    with tempfile.TemporaryDirectory() as d:
        cfg = {'data_dir': os.path.join(d, 'data')}
        cfg.update(overrides or {})
        # 安全網：測試絕不能連到真的捷運 API（就算忘了指向假伺服器），預設指向一定連不上的本機埠
        m = dict(cfg.get('metro') or {})
        m.setdefault('api_base', 'http://127.0.0.1:1')
        m.setdefault('station_list_url', 'http://127.0.0.1:1/RouteControl.asmx')
        cfg['metro'] = m
        path = os.path.join(d, 'config.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(cfg, f)
        C.reload(path)
        dash_custom.reset()               # 自訂分頁有伺服器端快取，不同測試不能互相沾到
        import dash_metro
        dash_metro.reset()                # 捷運的快取與失敗紀錄
        import server
        server._login_fails.clear()       # 登入失敗紀錄是模組層級狀態
        try:
            yield d
        finally:
            C.reload(os.path.join(d, 'none.json'))
