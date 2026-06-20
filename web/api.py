r"""
web/api.py
FastAPI бэкенд для car-car.by.

Запуск из корня проекта:
    .\.venv\Scripts\python.exe -m uvicorn web.api:app --reload

Открыть http://127.0.0.1:8000

Эндпоинты:
    GET /              — статический фронт (web/static/index.html)
    GET /api/stats     — счётчики для шапки
    GET /api/brands    — список марок с кол-вом машин
    GET /api/cars      — список с фильтрами и пагинацией
    GET /api/cars/{id} — одно объявление + фото + история цены
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# cars.db лежит в корне проекта (на уровень выше web/)
DB_PATH = Path(__file__).resolve().parent.parent / "cars.db"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="car-car.by API")


# ============================================================
# helpers
# ============================================================

def db() -> sqlite3.Connection:
    """
    Новое подключение на запрос. SQLite в read-mostly режиме это норма —
    можно безопасно использовать без пула, особенно с WAL.
    Row_factory=Row даёт обращение по имени колонки.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def car_to_dict(row: sqlite3.Row) -> dict:
    """Строка cars -> dict, разворачивая JSON-поля и приводя bools."""
    d = dict(row)
    # JSON-массивы хранятся как строки
    for k in ("lights_json", "features_json"):
        if k in d and d[k]:
            try: d[k.replace("_json", "")] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError): d[k.replace("_json", "")] = []
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
          COUNT(*)                                   AS total,
          SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active,
          SUM(CASE WHEN is_company=1 THEN 1 ELSE 0 END) AS company_ads,
          MAX(last_seen_at)                          AS last_update
        FROM cars
    """).fetchone()
    return dict(s)


# ============================================================
# brands — для дропдауна
# ============================================================

@app.get("/api/brands")
def brands():
    """Все марки с количеством активных объявлений, отсортированы по убыванию."""
    con = db()
    rows = con.execute("""
        SELECT brand, COUNT(*) AS cnt
        FROM cars
        WHERE is_active=1 AND brand IS NOT NULL
        GROUP BY brand
        ORDER BY cnt DESC, brand
    """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/models")
def models(brand: str = Query(..., description="марка для фильтрации моделей")):
    """Модели конкретной марки. Используется при выборе марки на фронте."""
    con = db()
    rows = con.execute("""
        SELECT model, COUNT(*) AS cnt
        FROM cars
        WHERE is_active=1 AND brand=? AND model IS NOT NULL
        GROUP BY model
        ORDER BY cnt DESC, model
    """, (brand,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/facets")
def facets():
    """
    Уникальные значения для дропдаунов фильтров (кузов, регион, КПП, и т.п.).
    Считается только по активным объявлениям. На 43к строк отрабатывает мгновенно
    благодаря тому, что это DISTINCT по одной колонке без JOIN.
    """
    con = db()
    def uniq(col: str) -> list[str]:
        return [r[0] for r in con.execute(
            f"SELECT {col} FROM cars WHERE is_active=1 AND {col} IS NOT NULL "
            f"GROUP BY {col} ORDER BY COUNT(*) DESC"
        )]

    return {
        "body_type":   uniq("body_type"),
        "region":      uniq("region"),
        "gearbox":     uniq("gearbox"),
        "drive":       uniq("drive"),
        "engine_type": uniq("engine_type"),
        "color":       uniq("color"),
    }


# ============================================================
# cars — главный листинг с фильтрами
# ============================================================

# валидные ключи сортировки → SQL ORDER BY (защита от SQL-инъекций)
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
    include_inactive: bool = False,
    sort:         str = Query("newest", description="newest|price_asc|price_desc|year_desc|mileage_asc"),
    page:         int = Query(1, ge=1),
    page_size:    int = Query(24, ge=1, le=100),
):
    """
    Главный поиск. Все фильтры опциональны. По умолчанию:
      - только активные (is_active=1)
      - сортировка по дате публикации (новые сверху)
    Возвращает {total, page, page_size, items: [...]} — total нужен для пагинации.
    """
    where = []
    params: list = []

    if not include_inactive:
        where.append("is_active=1")

    # точные равенства / IN-фильтры
    for col, val in [
        ("brand",       brand),
        ("model",       model),
        ("region",      region),
        ("body_type",   body_type),
        ("engine_type", engine_type),
        ("gearbox",     gearbox),
    ]:
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)

    if is_company is not None:
        where.append("is_company = ?")
        params.append(1 if is_company else 0)

    # диапазоны
    if year_min is not None:    where.append("year >= ?");       params.append(year_min)
    if year_max is not None:    where.append("year <= ?");       params.append(year_max)
    if price_min is not None:   where.append("price_usd >= ?");  params.append(price_min)
    if price_max is not None:   where.append("price_usd <= ?");  params.append(price_max)
    if mileage_max is not None: where.append("mileage_km <= ?"); params.append(mileage_max)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = SORT_OPTIONS.get(sort, SORT_OPTIONS["newest"])

    con = db()
    total = con.execute(f"SELECT COUNT(*) FROM cars {where_sql}", params).fetchone()[0]

    offset = (page - 1) * page_size
    rows = con.execute(f"""
        SELECT id, brand, model, generation, year, mileage_km, price_byn, price_usd,
               region, city, body_type, engine_type, gearbox, is_active, published_at
        FROM cars
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
    """, params + [page_size, offset]).fetchall()

    # к каждой машине — первое фото, отдельным запросом одной пачкой
    ids = [r["id"] for r in rows]
    thumbs = {}
    if ids:
        ph = ",".join("?" for _ in ids)
        for r in con.execute(f"""
            SELECT car_id, url FROM car_images
            WHERE car_id IN ({ph}) AND position=0
        """, ids):
            thumbs[r["car_id"]] = r["url"]

    items = []
    for r in rows:
        d = dict(r)
        d["thumb"] = thumbs.get(r["id"])
        d["source"] = "kufar"   # пока единственный источник; av.by/abw.by/onliner — позже
        items.append(d)

    return {"total": total, "page": page, "page_size": page_size, "items": items}


# ============================================================
# cars/{id} — детальная страница
# ============================================================

@app.get("/api/cars/{ad_id}")
def get_car(ad_id: int):
    con = db()
    row = con.execute("SELECT * FROM cars WHERE id=?", (ad_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    car = car_to_dict(row)

    # фото в правильном порядке
    car["images"] = [r["url"] for r in con.execute(
        "SELECT url FROM car_images WHERE car_id=? ORDER BY position", (ad_id,)
    )]

    # история цены
    car["price_history"] = [
        {"checked_at": r["checked_at"], "price_byn": r["price_byn"],
         "price_usd": r["price_usd"], "is_active": bool(r["is_active"])}
        for r in con.execute(
            "SELECT checked_at, price_byn, price_usd, is_active FROM car_prices "
            "WHERE car_id=? ORDER BY checked_at", (ad_id,)
        )
    ]

    # дилер если есть
    if car.get("account_id") and car.get("is_company"):
        d = con.execute("SELECT * FROM dealers WHERE account_id=?",
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