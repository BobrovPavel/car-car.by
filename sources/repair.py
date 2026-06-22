"""
repair.py
Перепарсивает объявления с проблемами.

Режимы:
    --no-images   — чинить без фото (по умолчанию)
    --no-brand    — чинить без марки
    --bad-price   — чинить с подозрительной ценой USD
                    (соотношение BYN/USD вне диапазона нормального курса)
    --id N        — один конкретный ID
    --all         — все известные проблемы
    --dry-run     — только показать, не чинить

При перепарсинге история цены НЕ записывается (skip_price_history=True),
поскольку мы исправляем ошибку парсинга, а не фиксируем реальное изменение цены.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sources.db import init_db
from sources.kufar import upsert_car, fetch_detail_safe, RateLimiter, DELAY_BETWEEN_DETAILS


# нормальный курс BYN/USD в 2024-2026 колеблется в районе 3.0-3.5.
# Если соотношение price_byn/price_usd сильно за этими пределами — значит
# одна из цен распарсилась некорректно (классический случай: "286.74 $ *"
# → 28674 вместо 286, тогда соотношение становится ~1 вместо ~3).
BYN_USD_RATIO_MIN = 1.5    # ниже — подозрительно (USD завышен)
BYN_USD_RATIO_MAX = 10.0   # выше — подозрительно (USD занижен)


def find_broken(con: sqlite3.Connection, mode: str) -> list[int]:
    """Возвращает ID активных объявлений, у которых не хватает данных."""
    if mode == "no-images":
        q = """SELECT c.id FROM cars c
               LEFT JOIN car_images i ON i.car_id = c.id
               WHERE c.is_active=1
               GROUP BY c.id
               HAVING COUNT(i.car_id) = 0"""
        return [r[0] for r in con.execute(q)]

    if mode == "no-brand":
        q = "SELECT id FROM cars WHERE is_active=1 AND (brand IS NULL OR brand='')"
        return [r[0] for r in con.execute(q)]

    if mode == "bad-price":
        # Подозрительно, когда соотношение BYN к USD вне нормального коридора.
        # NULL/0 в одной из цен оставляем в покое — это другая проблема.
        q = f"""SELECT id FROM cars
                WHERE is_active=1
                  AND price_byn IS NOT NULL AND price_usd IS NOT NULL
                  AND price_byn > 0 AND price_usd > 0
                  AND (price_byn * 1.0 / price_usd < {BYN_USD_RATIO_MIN}
                       OR price_byn * 1.0 / price_usd > {BYN_USD_RATIO_MAX})"""
        return [r[0] for r in con.execute(q)]

    if mode == "all":
        ids = set()
        for m in ("no-images", "no-brand", "bad-price"):
            ids.update(find_broken(con, m))
        return sorted(ids)

    raise ValueError(f"unknown mode: {mode}")


def repair_ids(con: sqlite3.Connection, ids: list[int], dry_run: bool):
    if not ids:
        print("нечего чинить, БД в порядке")
        return

    print(f"найдено к починке: {len(ids)}")

    if dry_run:
        for i in ids[:20]:
            row = con.execute(
                "SELECT id, brand, model, price_byn, price_usd FROM cars WHERE id=?",
                (i,)
            ).fetchone()
            if row:
                ratio = (row["price_byn"] / row["price_usd"]
                         if row["price_byn"] and row["price_usd"] else None)
                ratio_str = f" (BYN/USD = {ratio:.2f})" if ratio else ""
                print(f"  {i}: {row['brand']} {row['model']} — "
                      f"BYN {row['price_byn']}, USD {row['price_usd']}{ratio_str}")
        if len(ids) > 20:
            print(f"  ... и ещё {len(ids) - 20}")
        return

    rl = RateLimiter(DELAY_BETWEEN_DETAILS)
    fixed = failed = 0

    for n, ad_id in enumerate(ids, 1):
        # читаем ДО парсинга — иначе после upsert_car увидим уже новые значения
        before = con.execute(
            "SELECT price_byn, price_usd FROM cars WHERE id=?", (ad_id,)
        ).fetchone()
        old_usd = before["price_usd"] if before else None

        ad = fetch_detail_safe(ad_id, rl)
        if ad is None:
            failed += 1
            print(f"  [{n}/{len(ids)}] {ad_id}: не удалось забрать карточку")
            continue

        upsert_car(con, ad, skip_price_history=True)
        con.commit()
        fixed += 1

        nimg = len(ad.get("images") or [])
        new_usd = ad.get("price_usd")
        price_change = (f" ${old_usd} → ${new_usd}"
                        if old_usd != new_usd else f" ${new_usd}")
        print(f"  [{n}/{len(ids)}] {ad_id}: {ad.get('brand')} {ad.get('model')} —"
              f"{price_change} ({nimg} фото)")
        rl.wait()

    print(f"\nготово: исправлено {fixed}, не смогли {failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-images", action="store_true",
                    help="чинить объявления без фото (по умолчанию)")
    ap.add_argument("--no-brand",  action="store_true",
                    help="чинить объявления без марки")
    ap.add_argument("--bad-price", action="store_true",
                    help="чинить объявления с подозрительной ценой USD")
    ap.add_argument("--all",       action="store_true",
                    help="все известные проблемы")
    ap.add_argument("--id",        type=int, help="один конкретный ID")
    ap.add_argument("--dry-run",   action="store_true",
                    help="только показать, не чинить")
    args = ap.parse_args()

    con = init_db()
    con.row_factory = sqlite3.Row   # для удобного доступа по имени колонки

    if args.id:
        ids = [args.id]
    else:
        if args.all:         mode = "all"
        elif args.bad_price: mode = "bad-price"
        elif args.no_brand:  mode = "no-brand"
        else:                mode = "no-images"
        print(f"режим: {mode}")
        ids = find_broken(con, mode)

    try:
        repair_ids(con, ids, args.dry_run)
    except KeyboardInterrupt:
        print("\n[прервано]", file=sys.stderr)
    finally:
        con.commit()
        con.close()