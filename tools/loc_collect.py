#!/usr/bin/env python3
"""定位收集器：每次執行時，向 Home Assistant 讀一次 commute.entities 裡每個 device_tracker 的目前位置，
寫進 data/locations.sqlite（同一筆定位不會重複寫）。請用 cron 或 systemd timer 每 1～2 分鐘跑一次。

  python3 tools/loc_collect.py            # 收集一次
  python3 tools/loc_collect.py --prune    # 另外刪掉超過 commute.retention_days 的舊資料

只讀取 Home Assistant，不會寫回任何東西。出錯時結束碼非 0、訊息印在 stderr。
"""
import argparse
import os
import sqlite3
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dash_config as C   # noqa: E402
import dash_ha            # noqa: E402
from dash_support import APIError   # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
  entity          TEXT NOT NULL,
  ha_last_updated TEXT NOT NULL,
  poll_ts         INTEGER NOT NULL,
  state           TEXT,
  lat             REAL,
  lon             REAL,
  gps_accuracy    REAL,
  PRIMARY KEY (entity, ha_last_updated)
);
CREATE INDEX IF NOT EXISTS idx_loc_entity_ts ON locations(entity, poll_ts);
"""


def collect(db_path):
    entities = C.CFG['commute']['entities']
    if not entities:
        raise SystemExit('設定檔 commute.entities 是空的：請填入要追蹤的 device_tracker 實體')
    C.ensure_data_dir()
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    os.chmod(db_path, 0o600)
    now = int(time.time())
    n = 0
    for ent in entities:
        try:
            s = dash_ha.state(ent)
        except APIError as e:
            print(f'{ent}：{e}', file=sys.stderr)
            continue
        a = s.get('attributes') or {}
        cur = db.execute('INSERT OR IGNORE INTO locations VALUES (?,?,?,?,?,?,?)',
                         (ent, s.get('last_updated', ''), now, s.get('state'),
                          a.get('latitude'), a.get('longitude'), a.get('gps_accuracy')))
        n += cur.rowcount
    db.commit()
    return db, n


def prune(db):
    days = C.CFG['commute']['retention_days']
    cut = int(time.time()) - days * 86400
    db.execute('DELETE FROM locations WHERE poll_ts < ?', (cut,))
    db.commit()


def main():
    C.exit_if_invalid()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--prune', action='store_true')
    args = ap.parse_args()
    db, n = collect(C.data_path('locations.sqlite'))
    if args.prune:
        prune(db)
    db.close()
    print(f'{datetime.datetime.now():%F %T} 新增 {n} 筆')


if __name__ == '__main__':
    main()
