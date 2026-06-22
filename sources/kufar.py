"""
kufar.py
Сборщик объявлений kufar.by в cars.db.

Логика:
  1. Идём по листингу kufar (page-token пагинация), собираем ID
  2. Для каждого нового ID — карточка через kufar_detail_parser
  3. При обновлении существующего ID — пишем новую цену в car_prices,
     если она изменилась; полный re-parse карточки делаем не чаще REPARSE_DAYS
  4. В конце полного прогона (--full) ID, пропавшие из листинга, помечаются
     is_active=0 — sweep-режим. На частичных прогонах sweep отключается,
     иначе мы пометим как мёртвое всё, до чего просто не дошли.

Запуск:
    python kufar.py              # тест: первая страница (~100 объявлений)
    python kufar.py --max 500    # ограничение по числу обработанных ID
    python kufar.py --full       # полный прогон ~43k (≈6 часов при 0.5 сек)

Прерывание Ctrl+C — безопасно, всё закоммичено до этой точки сохраняется.
При следующем запуске сборщик пройдёт листинг заново и пропустит свежие
карточки (need_reparse() вернёт False), поэтому продолжение «бесплатное».
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

from sources.db import init_db
from sources.kufar_detail_parser import fetch_next_data, parse_ad

# ===== настройки =====
LISTING_URL = "https://cre-api.kufar.by/ads-search/v1/engine/v1/search/rendered-paginated"
LISTING_SIZE = 100                  # объявлений на страницу (макс 200 у kufar)
LISTING_CATEGORY = 2010             # легковые авто

DELAY_BETWEEN_DETAILS = 0.5         # стартовая задержка между карточками
DELAY_BETWEEN_LISTINGS = 1.0
MAX_RETRIES = 4                     # ретраи на сетевую ошибку или 5xx
BACKOFF_BASE = 2.0                  # сек, экспоненциальный бэк-офф

# поведение при 429 (rate limit от kufar)
RATELIMIT_PAUSE = 300               # сек, длинная пауза при 429
RATELIMIT_DELAY_BUMP = 1.5          # во сколько раз поднять задержку после 429
RATELIMIT_DELAY_MAX = 5.0           # потолок задержки
RATELIMIT_COOLDOWN_EVERY = 500      # каждые N успешных запросов — пробуем снизить задержку

REPARSE_DAYS = 7                    # карточку перечитываем не чаще раза в N дней
PROGRESS_EVERY = 50                 # печатать прогресс каждые N обработанных карточек

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RateLimiter:
    """
    Адаптивная задержка между запросами. Растёт при 429 (kufar говорит «помедленнее»),
    постепенно возвращается к стартовой после серии успехов.
    """
    def __init__(self, start: float):
        self.delay = start
        self.start = start
        self.success_streak = 0

    def wait(self):
        time.sleep(self.delay)

    def on_success(self):
        self.success_streak += 1
        if self.success_streak >= RATELIMIT_COOLDOWN_EVERY and self.delay > self.start:
            self.delay = max(self.start, self.delay / RATELIMIT_DELAY_BUMP)
            self.success_streak = 0
            print(f"  [rate] {RATELIMIT_COOLDOWN_EVERY} успехов подряд → "
                  f"снижаю задержку до {self.delay:.2f}с")

    def on_throttle(self):
        """Вызывается при 429. Длинная пауза + поднимаем базовую задержку."""
        self.success_streak = 0
        print(f"  [rate] 429! пауза {RATELIMIT_PAUSE}с, потом задержка "
              f"{self.delay:.2f} → ", end="", file=sys.stderr)
        time.sleep(RATELIMIT_PAUSE)
        self.delay = min(RATELIMIT_DELAY_MAX, self.delay * RATELIMIT_DELAY_BUMP)
        print(f"{self.delay:.2f}с", file=sys.stderr)


class Progress:
    """Прогресс с ETA. total задаётся после первой страницы листинга."""
    def __init__(self):
        self.total = None
        self.done = 0
        self.started_at = time.time()

    def tick(self):
        self.done += 1
        if self.total and self.done % PROGRESS_EVERY == 0:
            elapsed = time.time() - self.started_at
            rate = self.done / elapsed if elapsed else 0
            remaining = (self.total - self.done) / rate if rate else 0
            pct = 100 * self.done / self.total
            print(f"  [progress] {self.done}/{self.total} ({pct:.1f}%), "
                  f"ETA {_fmt_eta(remaining)}")


def _fmt_eta(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}ч {m}м" if h else f"{m}м {s}с"


# ===== листинг =====

def fetch_listing_page(token: str | None, rl: RateLimiter) -> dict:
    """Одна страница листинга. token=None для первой страницы."""
    params = {"cat": LISTING_CATEGORY, "size": LISTING_SIZE, "lang": "ru"}
    if token:
        params["cursor"] = token
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(LISTING_URL, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                rl.on_throttle()       # длинная пауза + подъём задержки
                continue                # этот запрос НЕ считается провалом ретрая
            if r.status_code >= 500:
                raise requests.RequestException(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            wait = BACKOFF_BASE ** attempt
            print(f"  ! листинг ошибка ({e}), жду {wait:.0f}с", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("листинг не отвечает после ретраев")


def walk_listing(rl: RateLimiter, max_ids: int | None = None, progress: Progress | None = None):
    """
    Генератор: листает kufar страница за страницей, выдаёт
    (ad_id, listing_record). Первая страница задаёт progress.total.
    """
    token = None
    page_num = 1
    yielded = 0

    while True:
        print(f"[листинг] страница {page_num} {'(token)' if token else '(start)'}")
        page = fetch_listing_page(token, rl)
        if progress is not None and progress.total is None:
            progress.total = page.get("total")
            if progress.total:
                print(f"[листинг] всего в категории: {progress.total}")

        ads = page.get("ads", [])
        if not ads:
            print("[листинг] пустая страница, конец")
            return

        for ad in ads:
            ad_id = ad.get("ad_id") or ad.get("list_id")
            if not ad_id:
                continue
            yield int(ad_id), ad
            yielded += 1
            if max_ids and yielded >= max_ids:
                return

        # ищем next-токен
        next_tok = None
        for p in (page.get("pagination") or {}).get("pages", []):
            if p.get("label") == "next":
                next_tok = p.get("token")
                break
        if not next_tok:
            print("[листинг] next-токен отсутствует, конец")
            return
        token = next_tok
        page_num += 1
        time.sleep(DELAY_BETWEEN_LISTINGS)


# ===== карточка с ретраями =====

def fetch_detail_safe(ad_id: int, rl: RateLimiter) -> dict | None:
    """Карточка с ретраями. None если объявление недоступно (404/410)."""
    for attempt in range(MAX_RETRIES):
        try:
            nd = fetch_next_data(ad_id)
            rl.on_success()
            return parse_ad(nd)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (404, 410):
                return None                            # объявление удалено
            if code == 429:
                rl.on_throttle()
                continue                                # не считается ретраем
            wait = BACKOFF_BASE ** attempt
            print(f"  ! карточка {ad_id} HTTP {code}, жду {wait:.0f}с", file=sys.stderr)
            time.sleep(wait)
        except (requests.RequestException, ValueError) as e:
            wait = BACKOFF_BASE ** attempt
            print(f"  ! карточка {ad_id} ошибка ({e}), жду {wait:.0f}с", file=sys.stderr)
            time.sleep(wait)
    print(f"  ! карточка {ad_id} провалена окончательно", file=sys.stderr)
    return None


# ===== запись в БД =====

CARS_COLUMNS = [
    "id","url","title","subject","brand","model","generation",
    "year","mileage_km","engine_type","capacity_l","power_hp",
    "gearbox","auto_gearbox","body_type","drive","seats","condition","repair_needed",
    "color","interior_color","interior_material",
    "has_climate","has_seatwarmer","has_abs","has_navigation","has_alloy_wheels",
    "has_parktronic","has_sunroof","has_alarm","has_cruise","has_aux",
    "lights_json","features_json",
    "price_byn","price_usd","auction","exchange",
    "region","city","district",
    "account_id","seller","is_company",
    "vin","vin_verified",
    "description","video",
    "published_at","first_seen_at","last_seen_at","last_parsed_at","is_active",
]

def upsert_car(con: sqlite3.Connection, ad: dict, skip_price_history: bool = False) -> str:
    """
    Пишет/обновляет cars + car_images + dealers.
    skip_price_history=True — не добавляет запись в car_prices (для repair).
    Возвращает 'new' / 'updated' / 'unchanged'.
    """
    ts = now_iso()
    car_id = ad["id"]

    existing = con.execute("SELECT price_byn, price_usd, last_parsed_at FROM cars WHERE id=?",
                           (car_id,)).fetchone()

    # year приходит строкой '2003' — конвертим в int
    year_int = None
    if ad.get("year"):
        try: year_int = int(ad["year"])
        except (ValueError, TypeError): pass

    row = {
        "id":              car_id,
        "url":             ad.get("url"),
        "title":           ad.get("title"),
        "subject":         ad.get("subject"),
        "brand":           ad.get("brand"),
        "model":           ad.get("model"),
        "generation":      ad.get("generation"),
        "year":            year_int,
        "mileage_km":      ad.get("mileage_km"),
        "engine_type":     ad.get("engine_type"),
        "capacity_l":      ad.get("capacity_l"),
        "power_hp":        ad.get("power_hp"),
        "gearbox":         ad.get("gearbox"),
        "auto_gearbox":    ad.get("auto_gearbox"),
        "body_type":       ad.get("body_type"),
        "drive":           ad.get("drive"),
        "seats":           ad.get("seats"),
        "condition":       ad.get("condition"),
        "repair_needed":   _bool(ad.get("repair_needed")),
        "color":           ad.get("color"),
        "interior_color":  ad.get("interior_color"),
        "interior_material": ad.get("interior_material"),
        "has_climate":     _bool(ad.get("has_climate")),
        "has_seatwarmer":  _bool(ad.get("has_seatwarmer")),
        "has_abs":         _bool(ad.get("has_abs")),
        "has_navigation":  _bool(ad.get("has_navigation")),
        "has_alloy_wheels":_bool(ad.get("has_alloy_wheels")),
        "has_parktronic":  _bool(ad.get("has_parktronic")),
        "has_sunroof":     _bool(ad.get("has_sunroof")),
        "has_alarm":       _bool(ad.get("has_alarm")),
        "has_cruise":      _bool(ad.get("has_cruise")),
        "has_aux":         _bool(ad.get("has_aux")),
        "lights_json":     json.dumps(ad.get("lights") or [], ensure_ascii=False),
        "features_json":   json.dumps(ad.get("features") or [], ensure_ascii=False),
        "price_byn":       ad.get("price_byn"),
        "price_usd":       ad.get("price_usd"),
        "auction":         ad.get("auction"),
        "exchange":        _bool(ad.get("exchange")),
        "region":          ad.get("region"),
        "city":            ad.get("city"),
        "district":        ad.get("district"),
        "account_id":      ad.get("account_id"),
        "seller":          ad.get("seller"),
        "is_company":      _bool(ad.get("is_company")),
        "vin":             ad.get("vin"),
        "vin_verified":    _bool(ad.get("vin_verified")),
        "description":     ad.get("description"),
        "video":           ad.get("video"),
        "published_at":    ad.get("date"),
        "first_seen_at":   ts,
        "last_seen_at":    ts,
        "last_parsed_at":  ts,
        "is_active":       1,
    }

    if existing is None:
        cols = ",".join(CARS_COLUMNS)
        placeholders = ",".join("?" for _ in CARS_COLUMNS)
        con.execute(f"INSERT INTO cars ({cols}) VALUES ({placeholders})",
                    [row[c] for c in CARS_COLUMNS])
        status = "new"
    else:
        upd_cols = [c for c in CARS_COLUMNS if c not in ("id", "first_seen_at")]
        set_clause = ",".join(f"{c}=?" for c in upd_cols)
        con.execute(f"UPDATE cars SET {set_clause} WHERE id=?",
                    [row[c] for c in upd_cols] + [car_id])
        status = "updated"

    # фото — пересоздаём
    con.execute("DELETE FROM car_images WHERE car_id=?", (car_id,))
    con.executemany(
        "INSERT INTO car_images (car_id, position, url) VALUES (?,?,?)",
        [(car_id, i, url) for i, url in enumerate(ad.get("images") or [])]
    )

    # история цены — только если не skip_price_history
    if not skip_price_history:
        if existing is None or \
           existing[0] != ad.get("price_byn") or existing[1] != ad.get("price_usd"):
            con.execute(
                "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
                "VALUES (?, ?, ?, ?, 1)",
                (car_id, ts, ad.get("price_byn"), ad.get("price_usd"))
            )

    # дилер
    if ad.get("account_id") and ad.get("is_company"):
        con.execute(
            """INSERT INTO dealers
               (account_id, company, company_legal, company_unp, company_address,
                contact_person, egr_number, egr_date, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(account_id) DO UPDATE SET
                 company         = excluded.company,
                 company_legal   = excluded.company_legal,
                 company_unp     = excluded.company_unp,
                 company_address = excluded.company_address,
                 contact_person  = excluded.contact_person,
                 egr_number      = excluded.egr_number,
                 egr_date        = excluded.egr_date""",
            (ad["account_id"], ad.get("company"), ad.get("company_legal"),
             ad.get("company_unp"), ad.get("company_address"),
             ad.get("contact_person"),
             ad.get("egr_number"), ad.get("egr_date"), ts)
        )

    return status

def _bool(v):
    """True/False -> 1/0, None -> None (для SQLite INTEGER с NULL-семантикой)."""
    if v is None:
        return None
    return 1 if v else 0


# ===== главный цикл =====

def need_reparse(con: sqlite3.Connection, ad_id: int) -> bool:
    """True если карточку ad_id нужно перечитать (нет в БД или прошло REPARSE_DAYS)."""
    row = con.execute("SELECT last_parsed_at FROM cars WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row[0])
        # Старые строки или ручные апдейты могут быть без TZ — приводим к UTC.
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        return age_days >= REPARSE_DAYS
    except ValueError:
        return True


def sweep_inactive(con: sqlite3.Connection, seen_ids: set[int]) -> int:
    """
    Помечает is_active=0 для объявлений, которых нет в seen_ids
    (= пропали из листинга = сняты с продажи).
    Добавляет запись в car_prices со is_active=0 для каждого помеченного.
    Возвращает кол-во помеченных.
    """
    ts = now_iso()
    # активные ID, которые не были увидены сегодня
    rows = con.execute("SELECT id FROM cars WHERE is_active=1").fetchall()
    missing = [r[0] for r in rows if r[0] not in seen_ids]
    if not missing:
        return 0

    print(f"[sweep] помечаю {len(missing)} объявлений как неактивные")
    con.executemany(
        "UPDATE cars SET is_active=0, last_seen_at=? WHERE id=?",
        [(ts, mid) for mid in missing]
    )
    con.executemany(
        "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
        "SELECT ?, ?, price_byn, price_usd, 0 FROM cars WHERE id=?",
        [(mid, ts, mid) for mid in missing]
    )
    con.commit()
    return len(missing)


def run(max_ids: int | None, do_sweep: bool):
    con = init_db()
    rl = RateLimiter(DELAY_BETWEEN_DETAILS)
    progress = Progress()
    n_new = n_updated = n_skipped = n_failed = 0
    seen_ids: set[int] = set()

    try:
        for ad_id, listing_row in walk_listing(rl, max_ids=max_ids, progress=progress):
            seen_ids.add(ad_id)
            progress.tick()

            # лёгкий путь: ID уже есть, перечитывать карточку рано
            if not need_reparse(con, ad_id):
                ts = now_iso()
                con.execute("UPDATE cars SET last_seen_at=?, is_active=1 WHERE id=?",
                            (ts, ad_id))
                con.commit()
                n_skipped += 1
                continue

            ad = fetch_detail_safe(ad_id, rl)
            if ad is None:
                n_failed += 1
                continue

            status = upsert_car(con, ad)
            con.commit()
            if status == "new":
                n_new += 1
                tag = "+"
            else:
                n_updated += 1
                tag = "~"
            print(f"  {tag} {ad_id} {ad.get('brand')} {ad.get('model')} "
                  f"({ad.get('year')}) — {ad.get('price_usd')}$")

            rl.wait()

    except KeyboardInterrupt:
        print("\n[прервано пользователем]", file=sys.stderr)
        do_sweep = False           # неполный список seen_ids — нельзя свипать
    finally:
        con.commit()

    n_swept = 0
    if do_sweep:
        n_swept = sweep_inactive(con, seen_ids)

    con.close()

    print(f"\nИтого: новых {n_new}, обновлено {n_updated}, "
          f"пропущено {n_skipped} (свежие), не удалось {n_failed}, "
          f"снято с продажи {n_swept}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=100,
                    help="максимум объявлений за прогон (по умолчанию 100)")
    ap.add_argument("--full", action="store_true",
                    help="полный прогон всех объявлений + sweep пропавших")
    args = ap.parse_args()
    # sweep только при --full: на --max список seen_ids неполный
    run(max_ids=None if args.full else args.max, do_sweep=args.full)