r"""
web/api.py
FastAPI бэкенд для car-car.by.

Запуск из корня проекта:
    .\.venv\Scripts\python.exe -m uvicorn web.api:app --reload

Открыть http://127.0.0.1:8000

Источники данных:
    cars.db        — kufar (основная база)
    cars_avby.db   — av.by (отдельная база, та же схema, source='av')

av.by-база подключается через ATTACH и объединяется с основной во временном
представлении cars_all (UNION ALL с литеральной колонкой source). Если файла
cars_avby.db ещё нет, API работает как раньше — только по kufar.

Эндпоинты:
    GET /              — статический фронт (web/static/index.html)
    GET /api/stats     — счётчики для шапки
    GET /api/brands    — список марок с кол-вом машин
    GET /api/models    — модели выбранной марки
    GET /api/facets    — значения для дропдаунов фильтров
    GET /api/cars      — список с фильтрами и пагинацией
    GET /api/cars/{id} — одно объявление + фото + история цены (нужен ?source=)
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# базы лежат в корне проекта (на уровень выше web/)
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "sources" / "cars.db"
AV_DB_PATH = ROOT / "data" / "cars_avby.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="car-car.by API")

# колонки, по которым строится объединённое представление cars_all.
# Одинаковы в обеих базах (общая схема db.py), поэтому UNION безопасен.
UNION_COLS = (
    "id, url, title, subject, brand, model, generation, year, mileage_km, "
    "engine_type, capacity_l, power_hp, gearbox, body_type, drive, seats, "
    "condition, color, interior_color, interior_material, price_byn, price_usd, "
    "region, city, district, account_id, seller, is_company, vin, vin_verified, "
    "description, video, published_at, first_seen_at, last_seen_at, is_active"
)


# ============================================================
# helpers
# ============================================================

def av_attached(con: sqlite3.Connection) -> bool:
    """True если база av.by подключена к этому соединению."""
    return any(r[1] == "av" for r in con.execute("PRAGMA database_list"))


def db() -> sqlite3.Connection:
    """
    Новое подключение на запрос. Подключает av.by-базу (если есть) и создаёт
    временное представление cars_all = main.cars UNION ALL av.cars с колонкой
    source. Все агрегатные/листинговые эндпоинты ходят в cars_all.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    av_ok = False
    if AV_DB_PATH.exists():
        try:
            con.execute("ATTACH DATABASE ? AS av", (str(AV_DB_PATH),))
            con.execute("SELECT 1 FROM av.cars LIMIT 1")  # таблица на месте?
            av_ok = True
        except sqlite3.Error:
            try:
                con.execute("DETACH DATABASE av")
            except sqlite3.Error:
                pass

    if av_ok:
        con.execute(
            f"CREATE TEMP VIEW cars_all AS "
            f"SELECT {UNION_COLS}, 'kufar' AS source FROM main.cars "
            f"UNION ALL "
            f"SELECT {UNION_COLS}, 'av' AS source FROM av.cars"
        )
    else:
        con.execute(
            f"CREATE TEMP VIEW cars_all AS "
            f"SELECT {UNION_COLS}, 'kufar' AS source FROM main.cars"
        )

    return con


def car_to_dict(row: sqlite3.Row) -> dict:
    """Строка cars -> dict, разворачивая JSON-поля и приводя bools."""
    d = dict(row)
    for k in ("lights_json", "features_json"):
        if k in d and d[k]:
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                d[k.replace("_json", "")] = []
            del d[k]
        elif k in d:
            d[k.replace("_json", "")] = []
            del d[k]
    return d


# ============================================================
# stats — для шапки и админ-страницы
# ============================================================

@app.get("/api/stats")
def stats():
    con = db()
    s = con.execute("""
        SELECT
          COUNT(*)                                     AS total,
          SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN is_company=1 THEN 1 ELSE 0 END) AS company_ads,
          MAX(last_seen_at)                            AS last_update
        FROM cars_all
    """).fetchone()
    out = dict(s)
    # разбивка по источникам — пригодится в шапке/админке
    out["by_source"] = {
        r["source"]: r["cnt"] for r in con.execute(
            "SELECT source, COUNT(*) AS cnt FROM cars_all GROUP BY source"
        )
    }
    return out


# ============================================================
# brands / models — для дропдаунов
# ============================================================

@app.get("/api/brands")
def brands():
    # Brands with active-ad counts, sorted alphabetically (case-insensitive).
    con = db()
    rows = con.execute("""
        SELECT brand, COUNT(*) AS cnt
        FROM cars_all
        WHERE is_active=1 AND brand IS NOT NULL
        GROUP BY brand
        ORDER BY brand COLLATE NOCASE
    """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/models")
def models(brand: str = Query(..., description="марка для фильтрации моделей")):
    # Models of a given brand, sorted alphabetically (case-insensitive).
    con = db()
    rows = con.execute("""
        SELECT model, COUNT(*) AS cnt
        FROM cars_all
        WHERE is_active=1 AND brand=? AND model IS NOT NULL
        GROUP BY model
        ORDER BY model COLLATE NOCASE
    """, (brand,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/facets")
def facets():
    """Уникальные значения для дропдаунов фильтров по активным объявлениям."""
    con = db()

    def uniq(col: str) -> list[str]:
        return [r[0] for r in con.execute(
            f"SELECT {col} FROM cars_all WHERE is_active=1 AND {col} IS NOT NULL "
            f"GROUP BY {col} ORDER BY COUNT(*) DESC"
        )]

    return {
        "body_type":   uniq("body_type"),
        "region":      uniq("region"),
        "gearbox":     uniq("gearbox"),
        "drive":       uniq("drive"),
        "engine_type": uniq("engine_type"),
        "color":       uniq("color"),
        "source":      uniq("source"),
    }


# ============================================================
# cars — главный листинг с фильтрами
# ============================================================

SORT_OPTIONS = {
    "newest":       "published_at DESC",
    "price_asc":    "price_usd ASC NULLS LAST",
    "price_desc":   "price_usd DESC NULLS LAST",
    "year_desc":    "year DESC NULLS LAST",
    "mileage_asc":  "mileage_km ASC NULLS LAST",
}


@app.get("/api/cars")
def list_cars(
    brand:        Optional[str] = None,
    model:        Optional[str] = None,
    year_min:     Optional[int] = None,
    year_max:     Optional[int] = None,
    price_min:    Optional[int] = Query(None, description="USD"),
    price_max:    Optional[int] = Query(None, description="USD"),
    mileage_max:  Optional[int] = Query(None, description="км"),
    region:       Optional[str] = None,
    body_type:    Optional[str] = None,
    engine_type:  Optional[str] = None,
    gearbox:      Optional[str] = None,
    is_company:   Optional[bool] = None,
    source:       Optional[str] = Query(None, description="kufar|av — фильтр по источнику"),
    include_inactive: bool = False,
    sort:         str = Query("newest"),
    page:         int = Query(1, ge=1),
    page_size:    int = Query(24, ge=1, le=100),
):
    """
    Главный поиск. Все фильтры опциональны. По умолчанию только активные,
    сортировка по дате публикации. Возвращает {total, page, page_size, items}.
    """
    where = []
    params: list = []

    if not include_inactive:
        where.append("is_active=1")

    for col, val in [
        ("brand",       brand),
        ("model",       model),
        ("region",      region),
        ("body_type",   body_type),
        ("engine_type", engine_type),
        ("gearbox",     gearbox),
        ("source",      source),
    ]:
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)

    if is_company is not None:
        where.append("is_company = ?")
        params.append(1 if is_company else 0)

    if year_min is not None:    where.append("year >= ?");       params.append(year_min)
    if year_max is not None:    where.append("year <= ?");       params.append(year_max)
    if price_min is not None:   where.append("price_usd >= ?");  params.append(price_min)
    if price_max is not None:   where.append("price_usd <= ?");  params.append(price_max)
    if mileage_max is not None: where.append("mileage_km <= ?"); params.append(mileage_max)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])

    con = db()
    total = con.execute(f"SELECT COUNT(*) FROM cars_all {where_sql}", params).fetchone()[0]

    offset = (page - 1) * page_size
    rows = con.execute(f"""
        SELECT id, brand, model, generation, year, mileage_km, price_byn, price_usd,
               region, city, district, body_type, engine_type, gearbox, is_active,
               published_at, source
        FROM cars_all
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    # первое фото для каждой машины. id могут совпасть между источниками,
    # поэтому тянем превью из соответствующей базы и ключуем по (source, id).
    by_src: dict[str, list[int]] = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r["id"])

    thumbs: dict[tuple[str, int], str] = {}
    for src, ids in by_src.items():
        if not ids:
            continue
        prefix = "av." if (src == "av" and av_attached(con)) else "main."
        ph = ",".join("?" for _ in ids)
        for r in con.execute(
            f"SELECT car_id, url FROM {prefix}car_images "
            f"WHERE car_id IN ({ph}) AND position=0", ids
        ):
            thumbs[(src, r["car_id"])] = r["url"]

    items = []
    for r in rows:
        d = dict(r)
        d["thumb"] = thumbs.get((r["source"], r["id"]))
        items.append(d)

    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ============================================================
# cars/{id} — детальная страница (нужен source для разрешения коллизий id)
# ============================================================

@app.get("/api/cars/{ad_id}")
def get_car(ad_id: int, source: str = Query("kufar", description="kufar|av")):
    con = db()
    av_ready = av_attached(con)
    prefix = "av." if (source == "av" and av_ready) else "main."

    row = con.execute(f"SELECT * FROM {prefix}cars WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    car = car_to_dict(row)
    car["source"] = "av" if prefix == "av." else "kufar"

    car["images"] = [r["url"] for r in con.execute(
        f"SELECT url FROM {prefix}car_images WHERE car_id=? ORDER BY position", (ad_id,)
    )]

    car["price_history"] = [
        {"checked_at": r["checked_at"], "price_byn": r["price_byn"],
         "price_usd": r["price_usd"], "is_active": bool(r["is_active"])}
        for r in con.execute(
            f"SELECT checked_at, price_byn, price_usd, is_active FROM {prefix}car_prices "
            f"WHERE car_id=? ORDER BY checked_at", (ad_id,)
        )
    ]

    # справочник дилеров есть только у kufar; для av показываем seller как есть
    if prefix == "main." and car.get("account_id") and car.get("is_company"):
        d = con.execute("SELECT * FROM main.dealers WHERE account_id=?",
                        (car["account_id"],)).fetchone()
        car["dealer"] = dict(d) if d else None
    else:
        car["dealer"] = None

    return car


# ============================================================
# статика — фронт
# ============================================================

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")