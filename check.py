import sqlite3, sys
sys.path.insert(0, ".")
from kufar_detail_parser import get_ad

con = sqlite3.connect("cars.db")
con.row_factory = sqlite3.Row

# 1) что в БД сейчас
print("=== в БД ===")
r = con.execute("SELECT id, last_parsed_at FROM cars WHERE id=1073937179").fetchone()
print("  cars:", dict(r) if r else "нет")
imgs = list(con.execute("SELECT position, url FROM car_images WHERE car_id=1073937179 ORDER BY position"))
print(f"  car_images: {len(imgs)} строк")
for r in imgs[:3]: print(f"   {r['position']}: {r['url']}")

# 2) что отдаёт парсер прямо сейчас
print("\n=== парсер прямо сейчас ===")
try:
    ad = get_ad(1073937179)
    print(f"  brand: {ad.get('brand')} {ad.get('model')}")
    print(f"  images: {len(ad.get('images') or [])}")
    for u in (ad.get("images") or [])[:3]:
        print(f"   {u}")
except Exception as e:
    print(f"  ОШИБКА: {e}")
