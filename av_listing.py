"""
av_listing.py
av.by used-car listing collector for car-car.by.

Pulls every used-car advert from av.by via the JSON listing endpoint and writes
it into a SQLite database that uses the SAME schema as the kufar collector
(db.py), so the two sources can be compared / merged later. Rows are tagged
source='av'.

Why this approach
-----------------
The av.by front (cars.av.by) is server-side rendered and protected by a WAF that
returns a custom 468 code even to browser-impersonating clients. The JSON host
web-api.av.by, however, IS reachable with curl_cffi. The listing itself is the
`apply` filter endpoint, captured from the SPA:

    POST https://web-api.av.by/offer-types/cars/filters/main/apply
    body: {
        "page": 2,
        "properties": [
            {"name": "brands", "property": 6,
             "value": [[{"name": "brand", "value": 1444},
                        {"name": "model", "value": 1449}]]},
            {"name": "price_currency", "value": 2}
        ],
        "sorting": 1
    }

Response carries: count, pageCount, advertsPerPage (25), and an `adverts` array.
Each advert already contains exact brand/model/generation (both as `properties`
and in `metadata`), full description, whole-unit prices, photos on avcdn.av.by,
seller / organization, masked VIN, and a `publicUrl` back to the ad.

Crawl strategy
--------------
Iterate brands (numeric ids only — slugs are not needed; the apply payload and
the per-offer publicUrl give us everything). For each brand we first try a
brand-only crawl across all pages. If av caps pagination (pageCount * perPage <
count) we fall back to per-model segmentation, taking model ids from the filters
`init` endpoint. In the common case the model taxonomy is never needed.

Usage
-----
    pip install curl_cffi
    python av_listing.py                # full crawl -> cars_avby.db
    python av_listing.py --test         # only Acura, for a quick smoke test
    python av_listing.py --brand 6      # only one brand id (e.g. Audi)
    python av_listing.py --db other.db  # custom output path

Ctrl+C is safe: progress is committed after every brand.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests as cffi
except ImportError:  # pragma: no cover
    sys.exit("curl_cffi is required:  pip install curl_cffi")

# db.py lives next to this script in the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import init_db  # noqa: E402

API = "https://web-api.av.by"
APPLY_URL = f"{API}/offer-types/cars/filters/main/apply"
INIT_URL = f"{API}/offer-types/cars/filters/main/init"

# property group id for the brand/model selector and currency code, taken from
# the captured payload (price_currency=2). Both BYN and USD come back per offer
# regardless of this value.
BRANDS_PROPERTY_ID = 6
PRICE_CURRENCY = 2
SORTING_ACTUAL = 1  # "актуальные"

DEFAULT_DB = Path(__file__).resolve().parent / "cars_avby.db"

# Optional global cap on how many adverts to save (for quick tests). None = no cap.
MAX_ADS: int | None = None


class _ReachedMax(Exception):
    """Raised internally once MAX_ADS adverts have been saved."""


_SAVED = [0]  # mutable counter shared across the crawl

# Full av.by brand taxonomy (id -> label), captured from the filters response.
# Embedded so the brand loop needs no extra endpoint call; brands change rarely.
BRANDS: dict[int, str] = {
    10297: "Abarth", 1444: "Acura", 10423: "Aito", 1: "Alfa Romeo",
    5940: "Alpina", 5324: "ARO", 5772: "Asia", 2325: "Aston Martin", 6: "Audi",
    10346: "Avatr", 10034: "BAIC", 10530: "BAW", 10457: "Belgee",
    1676: "Bentley", 8: "BMW", 1506: "Buick", 5459: "BYD", 40: "Cadillac",
    2632: "Changan", 1998: "Chery", 41: "Chevrolet", 42: "Chrysler",
    43: "Citroen", 10236: "Cupra", 1841: "Dacia", 46: "Daewoo", 47: "Daihatsu",
    2578: "Datsun", 10287: "Denza", 5109: "Derways", 45: "Dodge",
    5780: "Dongfeng", 10106: "Dongfeng Honda", 10563: "DS", 10877: "Epai",
    10303: "EXEED", 10572: "FangChengBao", 10761: "Farizon", 2465: "FAW",
    288: "Ferrari", 301: "Fiat", 2323: "Fisker", 330: "Ford", 2355: "Foton",
    10131: "GAC", 2012: "Geely", 10006: "Genesis", 372: "GMC",
    1726: "Great Wall", 2215: "Hafei", 5070: "Haima", 5782: "Haval",
    11074: "Hedmos", 10279: "HiPhi", 383: "Honda", 10275: "Hongqi",
    2681: "Hongxing", 10259: "Hozon", 1498: "Hummer", 10326: "Hycan",
    433: "Hyundai", 1343: "Infiniti", 2022: "Iran Khodro", 461: "Isuzu",
    10813: "IVECO", 2030: "JAC", 526: "Jaguar", 540: "Jeep", 10362: "Jetour",
    10450: "Jetta", 2272: "Jiangling", 10517: "Jmev", 10463: "Kaiyi",
    10612: "Karma", 10930: "KGM", 545: "Kia", 1279: "Lada (ВАЗ)",
    2437: "Lamborghini", 572: "Lancia", 584: "Land Rover", 10268: "Leapmotor",
    589: "Lexus", 10209: "Li Auto", 2586: "Lifan", 601: "Lincoln",
    11034: "Lingbao", 10907: "Lingxi", 10519: "Livan", 2295: "Lotus",
    10998: "Lucid", 10471: "Lynk & Co", 1625: "Maserati", 10592: "Maxus",
    634: "Mazda", 5970: "McLaren", 683: "Mercedes-Benz", 825: "Mercury",
    1906: "MG", 10510: "MHERO", 1850: "MINI", 834: "Mitsubishi", 10411: "Nio",
    892: "Nissan", 1364: "Oldsmobile", 10483: "Omoda", 966: "Opel",
    10539: "Ora", 10727: "Oting", 989: "Peugeot", 1012: "Plymouth",
    10534: "Polar", 10042: "Polestar", 1022: "Pontiac", 1485: "Porsche",
    1609: "Proton", 10226: "RAM", 5503: "Ravon", 1039: "Renault",
    10100: "Renault Samsung", 5800: "Roewe", 1067: "Rover", 10836: "Rox",
    1085: "Saab", 5029: "Saipa", 1703: "Saturn", 2698: "Scion", 1091: "SEAT",
    10289: "SERES", 10545: "Shenlan (Deepal)", 10447: "Shineray", 1126: "Skoda",
    10308: "Skywell", 2449: "Smart", 1597: "SsangYong", 1136: "Subaru",
    1155: "Suzuki", 10620: "Tank", 5447: "Tata", 2521: "Tesla", 5520: "Tianma",
    1181: "Toyota", 1216: "Volkswagen", 1238: "Volvo", 5437: "Vortex",
    10244: "Voyah", 1857: "Wartburg", 10067: "Weltmeister", 10617: "Wey",
    10334: "Wuling", 10584: "Xiaomi", 6019: "Xpeng", 10185: "Zeekr",
    2510: "Zotye", 5066: "ZX", 5076: "Богдан", 1310: "ГАЗ", 10094: "ЕрАЗ",
    1551: "ЗАЗ", 2894: "ИЖ", 2345: "ЛуАЗ", 2051: "Москвич", 5032: "ТагАЗ",
    1464: "УАЗ", 5019: "Эксклюзив",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
#  HTTP layer
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Adaptive delay with jitter; backs off hard and recovers slowly on 429."""

    def __init__(self, base: float = 1.5):
        self.base = base
        self.delay = base

    def wait(self) -> None:
        time.sleep(self.delay + random.uniform(0.0, 0.6))

    def ok(self) -> None:
        # recover very slowly so a big brand's later pages stay gentle
        if self.delay > self.base:
            self.delay = max(self.base, self.delay * 0.97)

    def throttled(self) -> None:
        self.delay = min(max(self.delay * 2, 5.0), 60.0)


class AvClient:
    def __init__(self, limiter: RateLimiter):
        self.rl = limiter
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://cars.av.by",
            "Referer": "https://cars.av.by/",
        }
        self.req_count = 0
        self._consecutive_429 = 0
        self._warm()

    def _warm(self) -> None:
        """Fresh cffi session + warm av.by cookies."""
        self.s = cffi.Session(impersonate="chrome")
        try:
            self.s.get("https://av.by/", timeout=20)
        except Exception:
            pass

    def _rewarm(self, reason: str) -> None:
        print(f"  ~ refreshing session ({reason})", file=sys.stderr)
        self._warm()
        time.sleep(2.0)

    def _post(self, url: str, json_body: dict) -> dict:
        backoff = 30.0
        for attempt in range(12):
            self.rl.wait()
            try:
                r = self.s.post(url, json=json_body, headers=self.headers, timeout=30)
            except Exception as exc:  # network hiccup -> wait and retry
                print(f"  ! network error ({exc}); retry", file=sys.stderr)
                self.rl.throttled()
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 300.0)
                continue
            if r.status_code == 429:
                self._consecutive_429 += 1
                retry_after = r.headers.get("Retry-After", "")
                sleep_s = float(retry_after) if retry_after.isdigit() else backoff
                print(f"  ! 429; waiting out the limit ({sleep_s:.0f}s)", file=sys.stderr)
                self.rl.throttled()
                time.sleep(sleep_s)
                backoff = min(backoff * 1.6, 300.0)
                # the limit may be session-bound: refresh cookies after a couple hits
                if self._consecutive_429 == 2:
                    self._rewarm("sustained 429")
                continue
            if r.status_code != 200:
                print(f"  ! HTTP {r.status_code} on {url}", file=sys.stderr)
                self.rl.throttled()
                time.sleep(backoff)
                backoff = min(backoff * 1.6, 300.0)
                continue
            # success
            self._consecutive_429 = 0
            self.rl.ok()
            self.req_count += 1
            # periodic cookie refresh keeps long brand crawls under the radar
            if self.req_count % 150 == 0:
                self._rewarm("periodic")
            return r.json()
        raise RuntimeError(f"giving up on POST {url}")

    def _get(self, url: str, params: dict) -> dict | None:
        self.rl.wait()
        try:
            r = self.s.get(url, params=params, headers=self.headers, timeout=30)
        except Exception:
            return None
        if r.status_code != 200:
            return None
        self.rl.ok()
        try:
            return r.json()
        except Exception:
            return None

    def apply(self, brand_id: int, model_id: int | None, page: int) -> dict:
        inner = [{"name": "brand", "value": brand_id}]
        if model_id is not None:
            inner.append({"name": "model", "value": model_id})
        body = {
            "page": page,
            "properties": [
                {"name": "brands", "property": BRANDS_PROPERTY_ID, "value": [inner]},
                {"name": "price_currency", "value": PRICE_CURRENCY},
            ],
            "sorting": SORTING_ACTUAL,
        }
        return self._post(APPLY_URL, body)

    def get_models(self, brand_id: int) -> list[tuple[int, str]]:
        """Model ids for a brand, from the filters init endpoint. May be []."""
        data = self._get(INIT_URL, {"brands[0][brand]": brand_id})
        if not data:
            return []
        opts = _find_options(data, "model")
        out = []
        for o in opts:
            mid = o.get("intValue", o.get("id"))
            if mid is not None:
                out.append((int(mid), o.get("label", "")))
        return out


def _find_options(node, name: str) -> list:
    """Recursively locate the `options` list of the property called `name`."""
    if isinstance(node, dict):
        if node.get("name") == name and isinstance(node.get("options"), list):
            return node["options"]
        for v in node.values():
            found = _find_options(v, name)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_options(item, name)
            if found:
                return found
    return []


# --------------------------------------------------------------------------- #
#  Offer -> row mapping
# --------------------------------------------------------------------------- #
def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def map_offer(offer: dict) -> dict:
    """Flatten one av.by advert into the cars-table column set."""
    props = {p.get("name"): p.get("value") for p in offer.get("properties", [])}
    meta = offer.get("metadata", {}) or {}
    price = offer.get("price", {}) or {}

    brand = props.get("brand") or meta.get("brandSlug")
    model = props.get("model")
    generation = (props.get("generation") or "").strip() or None

    capacity = props.get("engine_capacity")  # e.g. "3,0"
    capacity_l = f"{capacity.replace(',', '.')} л" if capacity else None

    # location: "Пинск, Брестская обл." -> city=Пинск, region=Брестская обл.
    loc = offer.get("locationName") or ""
    short = offer.get("shortLocationName") or ""
    parts = [s.strip() for s in loc.split(",") if s.strip()]
    city = short or (parts[0] if parts else None)
    region = parts[1] if len(parts) > 1 else city

    org_id = offer.get("organizationId")
    vin_info = meta.get("vinInfo") or {}

    photos = []
    for ph in offer.get("photos", []):
        big = ph.get("big") or ph.get("medium") or {}
        if big.get("url"):
            photos.append(big["url"])

    title = " ".join(x for x in [brand, model, generation] if x) or None

    return {
        "id": offer.get("id"),
        "url": offer.get("publicUrl"),
        "title": title,
        "subject": title,
        "brand": brand,
        "model": model,
        "generation": generation,
        "year": offer.get("year") or _to_int(props.get("year")),
        "mileage_km": _to_int(props.get("mileage_km")),
        "engine_type": props.get("engine_type"),
        "capacity_l": capacity_l,
        "power_hp": _to_int(props.get("engine_power")),
        "gearbox": props.get("transmission_type"),
        "body_type": props.get("body_type"),
        "drive": props.get("drive_type"),
        "seats": props.get("number_of_seats"),  # TEXT, e.g. "7 мест"
        "condition": props.get("condition"),
        "color": props.get("color"),
        "interior_color": props.get("interior_color"),
        "interior_material": props.get("interior_material"),
        "price_byn": _to_int((price.get("byn") or {}).get("amount")),
        "price_usd": _to_int((price.get("usd") or {}).get("amount")),
        "region": region,
        "city": city,
        "district": None,
        "account_id": str(org_id) if org_id else None,
        "seller": offer.get("sellerName"),
        "is_company": 1 if org_id else 0,
        "vin": vin_info.get("vin"),  # masked in the listing (full VIN is card-only)
        "vin_verified": 1 if vin_info.get("checked") else 0,
        "description": offer.get("description"),
        "video": offer.get("videoUrl"),
        "published_at": offer.get("publishedAt"),
        "_photos": photos,
    }


# --------------------------------------------------------------------------- #
#  Persistence
# --------------------------------------------------------------------------- #
INSERT_COLUMNS = [
    "id", "url", "title", "subject", "brand", "model", "generation", "year",
    "mileage_km", "engine_type", "capacity_l", "power_hp", "gearbox",
    "body_type", "drive", "seats", "condition", "color", "interior_color",
    "interior_material", "price_byn", "price_usd", "region", "city", "district",
    "account_id", "seller", "is_company", "vin", "vin_verified", "description",
    "video", "published_at",
]


def ensure_source_column(con) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(cars)")}
    if "source" not in cols:
        con.execute("ALTER TABLE cars ADD COLUMN source TEXT DEFAULT 'kufar'")
        con.commit()


def _done_key(brand_id: int) -> str:
    return f"av_brand_done:{brand_id}"


def mark_brand_done(con, brand_id: int, count: int) -> None:
    con.execute(
        "INSERT INTO _progress (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_done_key(brand_id), str(count)),
    )


def load_done_brands(con) -> set[int]:
    rows = con.execute(
        "SELECT key FROM _progress WHERE key LIKE 'av_brand_done:%'"
    ).fetchall()
    return {int(r[0].split(":", 1)[1]) for r in rows}


def save_row(con, row: dict) -> None:
    """UPSERT a car, append a price-history point on change, replace its images."""
    car_id = row["id"]
    if car_id is None:
        return
    ts = now_iso()

    prev = con.execute(
        "SELECT price_byn, price_usd FROM cars WHERE id=?", (car_id,)
    ).fetchone()

    cols = INSERT_COLUMNS + [
        "first_seen_at", "last_seen_at", "last_parsed_at", "is_active", "source"
    ]
    placeholders = ", ".join("?" for _ in cols)
    values = [row.get(c) for c in INSERT_COLUMNS] + [ts, ts, ts, 1, "av"]

    # On conflict keep first_seen_at; refresh mutable fields + timestamps.
    update_cols = [c for c in INSERT_COLUMNS if c != "id"] + [
        "last_seen_at", "last_parsed_at", "is_active", "source"
    ]
    update_set = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

    con.execute(
        f"INSERT INTO cars ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}",
        values,
    )

    # price history: new ad, or price changed
    changed = prev is None or prev[0] != row["price_byn"] or prev[1] != row["price_usd"]
    if changed:
        con.execute(
            "INSERT INTO car_prices (car_id, checked_at, price_byn, price_usd, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (car_id, ts, row["price_byn"], row["price_usd"]),
        )

    # images: replace wholesale (order preserved)
    con.execute("DELETE FROM car_images WHERE car_id=?", (car_id,))
    con.executemany(
        "INSERT INTO car_images (car_id, position, url) VALUES (?, ?, ?)",
        [(car_id, i, url) for i, url in enumerate(row["_photos"])],
    )


# --------------------------------------------------------------------------- #
#  Crawl
# --------------------------------------------------------------------------- #
def _crawl_filtered(client: AvClient, con, brand_id: int, model_id: int | None,
                    first_page: dict | None = None) -> int:
    """Walk every page for a (brand[, model]) filter. Returns rows saved."""
    page1 = first_page or client.apply(brand_id, model_id, 1)
    page_count = page1.get("pageCount", 1) or 1
    saved = 0
    for page in range(1, page_count + 1):
        data = page1 if page == 1 else client.apply(brand_id, model_id, page)
        for offer in data.get("adverts", []):
            save_row(con, map_offer(offer))
            saved += 1
            if MAX_ADS is not None and _SAVED[0] + saved >= MAX_ADS:
                _SAVED[0] += saved
                raise _ReachedMax
    _SAVED[0] += saved
    return saved


#  Above this many ads a brand is crawled model-by-model even if pagination is
#  not capped — splits one long page burst into short per-model runs, which the
#  av rate limiter tolerates far better.
BIG_BRAND_ADS = 1500


def _crawl_by_models(client: AvClient, con, brand_id: int, count: int,
                     label: str, page1: dict | None) -> int | None:
    """Crawl a brand model-by-model. Returns saved, or None if no taxonomy."""
    models = client.get_models(brand_id)
    if not models:
        return None
    saved = 0
    for mid, mlabel in models:
        saved += _crawl_filtered(client, con, brand_id, mid)
    mark_brand_done(con, brand_id, count)
    print(f"  {label}: {saved}/{count}  (via {len(models)} models)")
    return saved


def crawl_brand(client: AvClient, con, brand_id: int, label: str) -> int:
    page1 = client.apply(brand_id, None, 1)
    count = page1.get("count", 0)
    if not count:
        print(f"  {label}: 0")
        return 0
    page_count = page1.get("pageCount", 1) or 1
    per = page1.get("advertsPerPage", 25) or 25
    capped = page_count * per < count

    # Big brands -> per-model crawl (shorter bursts), even when not capped.
    if capped or count >= BIG_BRAND_ADS:
        saved = _crawl_by_models(client, con, brand_id, count, label, page1)
        if saved is not None:
            return saved
        # no model taxonomy: fall back to brand-only, best effort
        saved = _crawl_filtered(client, con, brand_id, None, first_page=page1)
        if capped:
            print(f"  {label}: {saved}/{count}  ! capped and no model taxonomy "
                  f"(missing ~{count - saved})", file=sys.stderr)
        else:
            mark_brand_done(con, brand_id, count)
            print(f"  {label}: {saved}/{count}")
        return saved

    # Small/medium brand: a single brand-only page walk is fine.
    saved = _crawl_filtered(client, con, brand_id, None, first_page=page1)
    mark_brand_done(con, brand_id, count)
    print(f"  {label}: {saved}/{count}")
    return saved


def run(db_path: Path, only_brand: int | None, test: bool, restart: bool,
        delay: float = 1.5) -> None:
    con = init_db(db_path)
    ensure_source_column(con)
    client = AvClient(RateLimiter(base=delay))

    if test:
        brands = {1444: "Acura"}
    elif only_brand is not None:
        brands = {only_brand: BRANDS.get(only_brand, str(only_brand))}
    else:
        brands = BRANDS

    done = set() if (restart or test or only_brand) else load_done_brands(con)
    if done:
        brands = {b: l for b, l in brands.items() if b not in done}
        print(f"resuming: {len(done)} brand(s) already done, {len(brands)} to go")

    total = 0
    failed: list[tuple[int, str]] = []
    started = time.time()
    print(f"av.by -> {db_path}  ({len(brands)} brands)")
    for i, (bid, label) in enumerate(brands.items(), 1):
        try:
            total += crawl_brand(client, con, bid, label)
        except _ReachedMax:
            con.commit()
            print(f"  reached --max ({MAX_ADS}); stopping")
            break
        except Exception as exc:
            print(f"  ! {label} failed: {exc}", file=sys.stderr)
            failed.append((bid, label))
        con.commit()  # checkpoint after every brand
        if i % 10 == 0:
            elapsed = time.time() - started
            print(f"  -- {i}/{len(brands)} brands, {total} ads, {elapsed:.0f}s")

    con.commit()
    print(f"\nDone. {total} ads in {time.time() - started:.0f}s -> {db_path}")
    if failed:
        ids = ",".join(str(b) for b, _ in failed)
        names = ", ".join(label for _, label in failed)
        print(f"\n{len(failed)} brand(s) did not finish: {names}")
        print("Re-run just those (idempotent, fills the gaps):")
        for b, label in failed:
            print(f"    python av_listing.py --brand {b}    # {label}")
        print(f"(brand ids: {ids})")

    if test:
        sample = con.execute(
            "SELECT id, brand, model, year, mileage_km, price_usd, city, url "
            "FROM cars ORDER BY id LIMIT 5"
        ).fetchall()
        print("\nsample rows:")
        for r in sample:
            print("  ", r)
        n_img = con.execute("SELECT COUNT(*) FROM car_images").fetchone()[0]
        print(f"images: {n_img}")
    con.close()


def main() -> None:
    global MAX_ADS
    ap = argparse.ArgumentParser(description="av.by used-car listing collector")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="output SQLite path")
    ap.add_argument("--brand", type=int, default=None, help="single brand id")
    ap.add_argument("--test", action="store_true", help="only Acura, smoke test")
    ap.add_argument("--max", type=int, default=None,
                    help="stop after saving this many adverts (quick test)")
    ap.add_argument("--restart", action="store_true",
                    help="ignore saved progress and re-crawl every brand")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="base seconds between requests (raise if you hit 429)")
    args = ap.parse_args()
    MAX_ADS = args.max
    run(Path(args.db), args.brand, args.test, args.restart, args.delay)


if __name__ == "__main__":
    main()