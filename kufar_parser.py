"""
kufar_detail_parser.py
Парсер карточки объявления kufar.by по ID.

Данные авто рендерятся на сервере (Next.js) и лежат в HTML
в теге <script id="__NEXT_DATA__">. Отдельного JSON-API для одного
объявления у kufar нет (проверено: все кандидаты возвращают 404).

Путь к объекту объявления:
    __NEXT_DATA__ -> props -> initialState -> adView -> data

Важно: поле cars_brand_v2 ОТСУТСТВУЕТ в листинг-API, но ПРИСУТСТВУЕТ
здесь, в карточке — поэтому марку/модель не нужно вытаскивать из
заголовка, они приходят готовым текстом в adParams[*]['vl'].
"""

import json
import re
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


def fetch_next_data(ad_id: int | str) -> dict:
    """Скачивает карточку по ID и возвращает распарсенный __NEXT_DATA__."""
    url = f"https://auto.kufar.by/vi/{ad_id}"
    resp = requests.get(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "ru,en;q=0.9",
            "Referer": "https://auto.kufar.by/cars",
        },
        timeout=15,
    )
    resp.raise_for_status()
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        raise ValueError(f"__NEXT_DATA__ не найден для id={ad_id} "
                         "(возможно капча/блок — добавь cookies/сессию)")
    return json.loads(m.group(1))


def _p(adparams: dict, key: str, field: str = "vl"):
    """
    Безопасно достаёт значение параметра из adParams.
    Пустые строки и пустые списки нормализует в None — чтобы в БД
    лежал NULL, а не '' (важно для индексов и WHERE col IS NULL).
    """
    node = adparams.get(key)
    if not node:
        return None
    val = node.get(field)
    if val == "" or val == []:
        return None
    return val


def _ap(accountparams: dict, key: str):
    """
    Достаёт значение из accountParams. В отличие от adParams, здесь
    основное значение лежит в .v, а .vl всегда пустой.
    """
    node = (accountparams or {}).get(key)
    if not node:
        return None
    val = node.get("v")
    return val if val not in ("", [], None) else None


# путь картинки kufar: 'adim1/<uuid>.jpg'
_IMG_PATH_RE = re.compile(r'adim\d*/[0-9a-fA-F-]+\.jpe?g')
# rule в URL: gallery = полный размер, list_thumbs_2x = превью
_IMG_FULL_TPL = "https://rms.kufar.by/v1/gallery/{path}"


def _collect_images(node, _seen=None, _out=None) -> list:
    """
    Рекурсивно обходит структуру объявления и собирает все уникальные
    фото в виде полноразмерных URL. Дубли (одно фото в превью и полном
    размере) схлопываются по имени файла, порядок сохраняется.
    """
    if _seen is None:
        _seen, _out = set(), []

    if isinstance(node, dict):
        # явный объект картинки {path: 'adim1/...', media_storage: 'rms'}
        path = node.get("path")
        if isinstance(path, str) and "adim" in path:
            _add_img(path, _seen, _out)
        for v in node.values():
            _collect_images(v, _seen, _out)
    elif isinstance(node, list):
        for v in node:
            _collect_images(v, _seen, _out)
    elif isinstance(node, str):
        m = _IMG_PATH_RE.search(node)
        if m:
            _add_img(m.group(0), _seen, _out)

    return _out


def _add_img(path: str, seen: set, out: list):
    path = path.lstrip("/")
    fname = path.rsplit("/", 1)[-1]   # дедуп по имени файла
    if fname not in seen:
        seen.add(fname)
        out.append(_IMG_FULL_TPL.format(path=path))


def _brand(ap: dict, title: str | None) -> str | None:
    """
    Марка с тройным фолбэком:
      1) новая схема — carsBrandV2 (есть в современных объявлениях)
      2) старая схема — brand (есть в архивных)
      3) первое слово заголовка ('BMW X5 I (E53)' -> 'BMW')
    """
    return _p(ap, "carsBrandV2") or _p(ap, "brand") \
        or (title.split()[0] if title else None)


def _model(ap: dict) -> str | None:
    """Модель: новая схема carsModelV2, иначе старая carsLevel1."""
    return _p(ap, "carsModelV2") or _p(ap, "carsLevel1")


def parse_ad(next_data: dict) -> dict:
    """
    Превращает __NEXT_DATA__ в плоскую запись под схему cars.db.
    Возвращает dict с нормализованными полями.

    Соглашения:
      - цены — int в минимальной валюте (BYN/USD), без копеек
      - mileage_km — int
      - булевые опции — настоящие True/False (не 'Да'/'-')
      - списочные опции (lights, features) — list[str]
      - картинки — list[str] полноразмерных URL
    """
    data = next_data["props"]["initialState"]["adView"]["data"]
    ap = data.get("adParams", {})
    acp = data.get("accountParams", {})

    # цены приходят строками вида '24 376 р.' / '8 800 $ *' — чистим до int
    def money(s):
        if not s:
            return None
        digits = re.sub(r"[^\d]", "", s)
        return int(digits) if digits else None

    # пробег: числовое 'v' если есть, иначе парсим '470 000 км'
    mileage = _p(ap, "mileage", "v") or money(_p(ap, "mileage"))

    # картинки — рекурсивный сборщик из всей структуры
    images = _collect_images(data)
    if not images and data.get("image"):
        images.append(data["image"])

    # списочные параметры (фары, характеристики) — list[str]
    def as_list(key):
        val = _p(ap, key)
        if isinstance(val, list):
            return val
        return [val] if val else []

    # булевые опции — берём из сырого .v, а не из 'Да'/'-' в .vl
    def flag(key):
        v = _p(ap, key, "v")
        return bool(v) if v is not None else None

    title = data.get("title")

    # company — публичное название компании. Только для дилерских объявлений
    # (isCompanyAd=true), у физлиц всегда None. Порядок по убыванию качества:
    #   1) trademark в adParams — бренд автосалона ('Автохаус Полоцк')
    #   2) contactPerson в accountParams — то, что чаще всего видно
    #      в шапке профиля ('РЕСПЕКТ АВТО Автомобили с Пробегом')
    #   3) name в accountParams — официальное юрлицо ('ООО ...')
    is_company = bool(data.get("isCompanyAd", False))
    company = None
    if is_company:
        company = (_p(ap, "trademark")
                   or _ap(acp, "contactPerson")
                   or _ap(acp, "name"))

    return {
        # идентификация
        "id":            int(data["adId"]),
        "url":           data.get("adViewLink"),
        "title":         title,
        "subject":       data.get("subject"),

        # марка/модель — с фолбэком на старую схему и заголовок
        "brand":         _brand(ap, title),
        "model":         _model(ap),
        "generation":    _p(ap, "carsGenV2"),

        # технические характеристики
        "year":          _p(ap, "regdate", "v"),
        "mileage_km":    mileage,
        "engine_type":   _p(ap, "carsEngine"),         # 'Бензин' / 'Дизель' / ...
        "capacity_l":    _p(ap, "carsCapacity"),       # '3.0 л' — текст, в БД лучше отдельно парсить
        "power_hp":      _p(ap, "carsEnginePower", "v"),  # 231 (int)
        "gearbox":       _p(ap, "carsGearbox"),        # 'Автоматическая' / 'Механика'
        "auto_gearbox":  _p(ap, "carsAutogearbox"),    # 'Автомат' / 'Робот' / 'Вариатор'
        "body_type":     _p(ap, "carsType"),           # 'Внедорожник'
        "drive":         _p(ap, "carsDrive"),          # 'Полный' / 'Передний' / 'Задний'
        "seats":         _p(ap, "carsSeats"),
        "condition":     _p(ap, "condition"),
        "repair_needed": flag("repairNeeded"),

        # внешний вид и салон
        "color":             _p(ap, "carsColor"),
        "interior_color":    _p(ap, "carsInteriorColor"),
        "interior_material": _p(ap, "carsInteriorMaterial"),

        # опции
        "lights":            as_list("carsLights"),       # ['Ксеноновые/биксеноновые', ...]
        "features":          as_list("carsFeatures"),     # ['Фаркоп', 'Заводская тонировка', ...]
        "has_climate":       flag("carsClimate"),
        "has_seatwarmer":    flag("carsSeatwarmer"),
        "has_abs":           flag("carsAbs"),
        "has_navigation":    flag("carsNavigation"),
        "has_alloy_wheels":  flag("carsAlloyWheels"),
        "has_parktronic":    flag("carsParktronic"),
        "has_sunroof":       flag("carsSunroof"),
        "has_alarm":         flag("carsAlarm"),
        "has_cruise":        flag("carsCruiseControl"),
        "has_aux":           flag("carsAux"),

        # цены
        "price_byn":     money(data.get("price")),     # 24376
        "price_usd":     money(data.get("priceUsd")),  # 8800
        "auction":       _p(ap, "auction"),            # 'Торг уместен' и т.п.
        "exchange":      flag("possibleExchange"),

        # локация
        "region":        _p(ap, "region") or data.get("region"),   # область
        "city":          _p(ap, "area"),                            # город

        # продавец / дилер
        # seller — что показывает kufar в самом объявлении (поле "Имя"),
        # для физлиц это имя, для дилеров — название юрлица
        "seller":        data.get("userName"),
        "is_company":    is_company,
        "company":       company,

        # реквизиты дилера. У физлиц всегда None, потому что accountParams
        # для них либо отсутствует, либо содержит только имя — а юрлицо/УНП/адрес
        # имеют смысл только для компаний.
        "company_legal":   _ap(acp, "name") if is_company else None,
        "company_unp":     _ap(acp, "vatNumber"),
        "company_address": _ap(acp, "companyAddress"),
        "egr_number":      _ap(acp, "egrNumber"),
        "egr_date":        _ap(acp, "egrDate"),
        "account_id":      data.get("accountId"),       # для группировки объявлений одного продавца

        # VIN
        "vin":           _p(ap, "fullVehicleVin", "v"),
        "vin_verified":  flag("vehicleVinVerifiedCheckbox"),

        # контент
        "description":   data.get("description") or data.get("body"),
        "video":         _p(ap, "contentVideo", "v"),
        "date":          data.get("date"),
        "images":        images,
    }


def get_ad(ad_id: int | str) -> dict:
    """Удобный one-liner: ID -> готовая запись."""
    return parse_ad(fetch_next_data(ad_id))


if __name__ == "__main__":
    import sys
    ad = get_ad(sys.argv[1] if len(sys.argv) > 1 else 1073332333)
    print(json.dumps(ad, ensure_ascii=False, indent=2))