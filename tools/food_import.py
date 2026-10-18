#!/usr/bin/env python3
"""把店家 CSV 匯入美食庫（data/food.sqlite 的 places 表）。可重複執行：同名同地址的店會更新而不是重複新增，
但不會動你自己記錄的「吃過日期」「自己的評分」「排除」欄位。

  python3 tools/food_import.py data.example/places.sample.csv
  python3 tools/food_import.py --init           # 只建立空的資料庫

CSV 第一列是欄位名稱（UTF-8，可含 BOM）。認得的欄位（只有 name 是必填）：
  name 店名, category 類別, address 地址, district 行政區, lat 緯度, lng 經度,
  price 一人大約花費（元）, rating 評分, reviews 評論數, hours 營業時間, note 備註
營業時間格式例：一~五 11:00-14:00,17:00-21:00; 六日 10:00-22:00（詳見 dash_food.parse_hours）
"""
import argparse
import csv
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dash_config as C   # noqa: E402
import dash_food as F   # noqa: E402


def _num(v, cast):
    """文字 → 數字；空白、看不懂、inf／nan／超大值一律當沒填（None）。"""
    v = (v or '').strip().replace(',', '')
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    if not math.isfinite(x):
        return None
    try:
        return int(x) if cast is int else x
    except OverflowError:
        return None


def import_csv(path, db):
    added = updated = skipped = 0
    with open(path, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            name = (r.get('name') or '').strip()
            if not name:
                skipped += 1
                continue
            addr = (r.get('address') or '').strip()
            vals = (r.get('category', '').strip() or None, addr, (r.get('district') or '').strip() or None,
                    _num(r.get('lat'), float), _num(r.get('lng'), float), _num(r.get('price'), int),
                    _num(r.get('rating'), float), _num(r.get('reviews'), int),
                    (r.get('hours') or '').strip() or None, (r.get('note') or '').strip() or None)
            exists = db.execute('select id from places where name=? and address=?', (name, addr)).fetchone()
            if exists:
                db.execute('update places set category=?,district=?,lat=?,lng=?,price=?,rating=?,reviews=?,hours=?,note=? '
                           'where id=?', (vals[0],) + vals[2:] + (exists[0],))
                updated += 1
            else:
                db.execute('insert into places(name,category,address,district,lat,lng,price,rating,reviews,hours,note) '
                           'values (?,?,?,?,?,?,?,?,?,?,?)', (name,) + vals)
                added += 1
    db.commit()
    return added, updated, skipped


def main():
    C.exit_if_invalid()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('csv', nargs='?')
    ap.add_argument('--init', action='store_true', help='只建立空的資料庫')
    a = ap.parse_args()
    db = F.init_db()
    if a.csv:
        add, upd, skip = import_csv(a.csv, db)
        print(f'新增 {add}、更新 {upd}、略過 {skip}（沒有店名）')
    elif not a.init:
        ap.error('請給 CSV 檔路徑，或加 --init')
    db.close()
    print('資料庫：', F.db_path())


if __name__ == '__main__':
    main()
