"""
db.py
Схема SQLite для car-car.by. Запуск как скрипт создаёт пустую cars.db.
Повторный запуск безопасен — все CREATE TABLE с IF NOT EXISTS.

Таблицы:
  cars         — основная карточка авто, PK = kufar ad_id
  car_images   — фото (один-ко-многим), FK -> cars.id, индекс по car_id
  car_prices   — история цены и статуса (новая запись при изменении)
  dealers      — справочник дилеров по account_id (нормализация)
  _progress    — чекпоинт сборщика: где остановились, какие ID видели
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).with_name("cars.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cars (
    id              INTEGER PRIMARY KEY,           -- kufar ad_id
    url             TEXT,
    title           TEXT,
    subject         TEXT,

    -- марка/модель
    brand           TEXT,
    model           TEXT,
    generation      TEXT,

    -- технические
    year            INTEGER,
    mileage_km      INTEGER,
    engine_type     TEXT,
    capacity_l      TEXT,
    power_hp        INTEGER,
    gearbox         TEXT,
    auto_gearbox    TEXT,
    body_type       TEXT,
    drive           TEXT,
    seats           TEXT,
    condition       TEXT,
    repair_needed   INTEGER,    -- 0/1, NULL если неизвестно

    -- внешний вид
    color              TEXT,
    interior_color     TEXT,
    interior_material  TEXT,

    -- опции (булевы 0/1, NULL = не указано в объявлении)
    has_climate        INTEGER,
    has_seatwarmer     INTEGER,
    has_abs            INTEGER,
    has_navigation     INTEGER,
    has_alloy_wheels   INTEGER,
    has_parktronic     INTEGER,
    has_sunroof        INTEGER,
    has_alarm          INTEGER,
    has_cruise         INTEGER,
    has_aux            INTEGER,

    -- списочные опции — JSON-строки (массивы маленькие, индекс не нужен)
    lights_json        TEXT,
    features_json      TEXT,

    -- цены (текущая, история в car_prices)
    price_byn       INTEGER,
    price_usd       INTEGER,
    auction         TEXT,
    exchange        INTEGER,

    -- локация
    region          TEXT,
    city            TEXT,
    district        TEXT,                          -- район города (для Минска)

    -- продавец
    account_id      TEXT,                          -- FK в dealers
    seller          TEXT,
    is_company      INTEGER,

    -- VIN
    vin             TEXT,
    vin_verified    INTEGER,

    -- контент
    description     TEXT,
    video           TEXT,

    -- временные метки kufar и наши
    published_at    TEXT,                          -- date из карточки (ISO)
    first_seen_at   TEXT NOT NULL,                 -- когда сборщик впервые увидел
    last_seen_at    TEXT NOT NULL,                 -- последний раз был в листинге
    last_parsed_at  TEXT NOT NULL,                 -- последний раз парсили карточку
    is_active       INTEGER NOT NULL DEFAULT 1     -- 0 = пропал из листинга
);

CREATE INDEX IF NOT EXISTS idx_cars_brand_model ON cars(brand, model);
CREATE INDEX IF NOT EXISTS idx_cars_year       ON cars(year);
CREATE INDEX IF NOT EXISTS idx_cars_price_usd  ON cars(price_usd);
CREATE INDEX IF NOT EXISTS idx_cars_active     ON cars(is_active);


CREATE TABLE IF NOT EXISTS car_images (
    car_id    INTEGER NOT NULL,
    position  INTEGER NOT NULL,                    -- 0 = главное фото
    url       TEXT NOT NULL,
    PRIMARY KEY (car_id, position),
    FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_car_images_car ON car_images(car_id);


CREATE TABLE IF NOT EXISTS car_prices (
    rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
    car_id      INTEGER NOT NULL,
    checked_at  TEXT NOT NULL,
    price_byn   INTEGER,
    price_usd   INTEGER,
    is_active   INTEGER NOT NULL DEFAULT 1,        -- 0 = снято с продажи
    FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_car_prices_car  ON car_prices(car_id);
CREATE INDEX IF NOT EXISTS idx_car_prices_date ON car_prices(checked_at);


CREATE TABLE IF NOT EXISTS dealers (
    account_id        TEXT PRIMARY KEY,            -- kufar accountId
    company           TEXT,                        -- публичное название (берётся из userName/trademark)
    company_legal     TEXT,                        -- юрлицо (accountParams.name)
    company_unp       TEXT,                        -- УНП
    company_address   TEXT,
    contact_person    TEXT,                        -- имя менеджера, отдельно от компании
    egr_number        TEXT,
    egr_date          TEXT,
    first_seen_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dealers_unp ON dealers(company_unp);


-- служебная: одна строка с прогрессом
CREATE TABLE IF NOT EXISTS _progress (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""


def init_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Создаёт БД и схему, возвращает открытое соединение."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")        # параллельные чтения во время записи
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """
    Лёгкие миграции для уже созданных БД. Каждая операция идемпотентна:
    проверяем, что колонки ещё нет, и только тогда ALTER TABLE.
    """
    def has_column(table: str, column: str) -> bool:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    # 2026-06-20: contact_person в dealers (отдельно от company)
    if not has_column("dealers", "contact_person"):
        con.execute("ALTER TABLE dealers ADD COLUMN contact_person TEXT")

    # 2026-06-21: district в cars. Раньше для Минска в city лежал район
    # ('Октябрьский'), т.к. kufar отдаёт area=район для города-региона.
    # Добавляем колонку и бэкофиллим: переносим район в district, city='Минск'.
    # Гард на наличие колонки делает бэкофилл одноразовым.
    if not has_column("cars", "district"):
        con.execute("ALTER TABLE cars ADD COLUMN district TEXT")
        con.execute(
            "UPDATE cars SET district=city, city='Минск' "
            "WHERE region='Минск' AND city IS NOT NULL AND city<>'Минск'"
        )


if __name__ == "__main__":
    con = init_db()
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(f"OK. {DB_PATH} готова. Таблицы: {tables}")
    con.close()