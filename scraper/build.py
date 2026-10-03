"""Build docs/data.json from the raw cache in data/raw/ (no API calls, so re-run freely).

For every product in data/raw/_listing.json:
  - parse the nutrition table into per-100g label macros (choosing the right column),
  - work out the pack weight and price per kg (shelf and Clubcard),
  - flag suspect data,
and write the result to docs/data.json in a compact rows-of-arrays format.

Price, promotions and sale status come from the listing (one consistent snapshot), the
nutrition table and pack size from the product files.

Usage:
    python scraper/build.py
    python scraper/build.py --check 250028337   # print the parsed record for one product
"""

import argparse
import datetime
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
LISTING_PATH = RAW_DIR / "_listing.json"
OUT_PATH = ROOT / "docs" / "data.json"

CATEGORIES = ["Fresh Food", "Bakery", "Frozen Food", "Food Cupboard", "Treats & Snacks"]
NUTRIENTS = ["kcal", "fat", "sat", "carb", "sugar", "fibre", "protein", "salt"]
VALUE_KEYS = ("value1", "value2", "value3", "value4")
IMAGE_PREFIX = "https://digitalcontent.api.tesco.com/v2/media/"

# Flag bits (docs/app.js has the same table).
F_BAD_MACROS = 1    # a macro over 100 g per 100 g, or macros adding up to more than 100 g
F_BAD_KCAL = 2      # 4P + 4C + 9F + 2 fibre is far from the listed kcal
F_BAD_PRICE = 4     # price/kg disagrees with Tesco's unit price, or is implausible
F_EST_WEIGHT = 8    # pack weight estimated (from the title or the label's serving size)
F_COOKED = 16       # label values are for the cooked/prepared product, not as sold
F_SCALED = 32       # no per-100g column; scaled from a per-serving column
F_NOT_FOR_SALE = 64
F_LIQUID = 128      # values per 100 ml (1 litre is treated as 1 kg)
F_MULTIBUY = 256    # Clubcard price is a multibuy ("Any 3 for £5"), shown per item
F_YIELD = 512       # cooked values converted back to as-sold weight with the label's yield statement
F_EDIBLE = 1024     # price/kg is per kg of edible food: a typical share for bone or eggshell left out
FLAG_NAMES = {
    F_BAD_MACROS: "bad_macros", F_BAD_KCAL: "bad_kcal", F_BAD_PRICE: "bad_price",
    F_EST_WEIGHT: "est_weight", F_COOKED: "cooked", F_SCALED: "scaled",
    F_NOT_FOR_SALE: "not_for_sale", F_LIQUID: "liquid", F_MULTIBUY: "multibuy", F_YIELD: "yield_adjusted",
    F_EDIBLE: "edible",
}

KCAL_PER_KJ = 1 / 4.184
PPK_RANGE = (0.15, 20000.0)  # plausible £/kg for food; outside this the weight or price is wrong


def read_json(path: Path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- numbers and weights

NUMBER = r"\d+(?:[.,]\d+)?"
WEIGHT_UNITS = {"g": 1e-3, "gr": 1e-3, "gm": 1e-3, "gms": 1e-3, "grams": 1e-3, "kg": 1.0,
                "ml": 1e-3, "cl": 1e-2, "l": 1.0, "ltr": 1.0, "litre": 1.0, "litres": 1.0, "mg": 1e-6}
WEIGHT_RE = re.compile(rf"({NUMBER})\s*(kg|grams|gms|gm|gr|g|ml|cl|ltr|litres|litre|l)\b", re.I)
MULTI_WEIGHT_RE = re.compile(rf"(\d+)\s*x\s*({NUMBER})\s*(kg|grams|gms|gm|gr|g|ml|cl|ltr|litres|litre|l)\b", re.I)


def to_float(text: str) -> float:
    return float(text.replace(",", "."))


def title_weight_kg(title: str) -> float | None:
    """Pack weight in the title: '400G', '4 x 125g', '1.5 Litre'. None if absent."""
    m = MULTI_WEIGHT_RE.search(title)
    if m:
        return int(m.group(1)) * to_float(m.group(2)) * WEIGHT_UNITS[m.group(3).lower()]
    matches = WEIGHT_RE.findall(title)
    if matches:
        value, unit = matches[-1]
        return to_float(value) * WEIGHT_UNITS[unit.lower()]
    return None


COUNT_PATTERNS = [
    re.compile(r"\b(\d+)\s*(?:pack|pk|pck|pieces|portions|servings)\b", re.I),
    re.compile(r"\b(\d+)\s*x\b", re.I),
    # A bare count before the product name: "Tesco 6 Snowy Mince Pies", "10 Large Free Range Eggs"
    re.compile(r"\b(\d+)\b(?!\s*(?:[.,]\d|(?:kg|g|gr|gms|ml|cl|l|ltr|litres?|p)\b|%|/|-))", re.I),
]


def title_count(title: str) -> int | None:
    """Number of items in the pack from the title ('6 Pack', 'Twin Pack', 'Each'), if stated."""
    for pattern in COUNT_PATTERNS:
        m = pattern.search(title)
        if m and 0 < int(m.group(1)) <= 100:
            return int(m.group(1))
    if re.search(r"\btwin\b", title, re.I):
        return 2
    if re.search(r"\beach\b", title, re.I):
        return 1
    return None


# ---------------------------------------------------------------- nutrition table

PER100_RE = re.compile(
    r"(?<![\d.,])100\s*(?:g|gr|gms?|grams?|ml|millilit)|per\s*100\b|/\s*100\b|hundred grams|^\s*100\s+contains", re.I)
PERCENT_COLUMN_RE = re.compile(r"%|\bRI\b|NRV|reference intake|daily value|\bGDA\b", re.I)
COOKED_RE = re.compile(
    r"(?<!un)prepared|(?<!un)cooked|baked|fried|fryer|grilled|microwaved|heated|boiled|made up|reconstituted|"
    r"roasted|toasted|steamed|poached|as served|with\s+[\w\s.%-]*milk", re.I)
AS_SOLD_RE = re.compile(r"unprepared|uncooked|as sold|as packed|frozen|\bdry\b|\braw\b", re.I)
COOKED_FOOTER_RE = re.compile(
    r"^\W*(?:when|if|values?|nutrition\w*|figures?|all values)\b.*\b(?:(?<!un)cooked|(?<!un)prepared|baked|fried|"
    r"grilled|microwaved|heated|boiled|made up|reconstituted|roasted|toasted|steamed)\b", re.I)
# '** When grilled according to instructions 400g typically weighs 354g.'
YIELD_RE = re.compile(rf"({NUMBER})\s*g\b[^0-9]{{0,60}}?\bweigh\w*[^0-9]{{0,20}}?({NUMBER})\s*g\b", re.I)
SERVING_WEIGHT_RE = re.compile(rf"({NUMBER})\s*(g|gr|gms?|grams?|ml)\b", re.I)


def classify_row(name: str) -> str | None:
    """Map a nutrition row name ('of which Sturates', 'Energy (kcal)', 'Fat (g)') to a field."""
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    n = " ".join(n.split())
    if not n:
        return None
    if "energy" in n or re.fullmatch(r"(?:energy )?(?:kj|kcal|kj kcal)", n):
        return "energy"
    if "calorie" in n:  # US-style labels: 'Calories', but not 'Calories from Fat'
        return None if "from" in n else "energy"
    if re.search(r"prot[ei]{2}n", n):  # 'Protien' (sic) appears too
        return "protein"
    if "salt" in n:
        return "salt"
    if "sodium" in n:
        return "sodium"
    if "fibre" in n or "fiber" in n:
        return "fibre"
    if "polyol" in n:
        return "polyols"
    if re.search(r"s\w{0,2}turat", n) and not re.search(r"unsat|mono|poly|trans|omega", n):
        return "sat"
    if "sugar" in n and not re.search(r"polyol|alcohol|added|free sugar|lactose", n):
        return "sugar"
    if "carbohydrate" in n or re.fullmatch(r"(?:total )?carbs?", n):
        return "carb"
    if re.search(r"\bfat\b|\bfats\b|\blipids?\b", n) and not re.search(
            r"s\w{0,2}turat|unsat|mono|poly|trans|omega|of which", n):
        return "fat"
    return None


def parse_amount(text) -> float | None:
    """'12.6g' -> 12.6, '<0.5g' -> 0.5, 'trace' -> 0, '780mg (=98% NRV*)' -> 0.78, '-' -> None."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if not s or s in {"-", "--", "n/a", "na", "null", "null / null"}:
        return None
    if re.match(r"^(trace|nil|negligible|0\s*g?$)", s):
        return 0.0
    m = re.search(rf"({NUMBER})\s*(mg|µg|ug|mcg|g)?", s)
    if not m:
        return None
    value = to_float(m.group(1))
    unit = m.group(2)
    if unit == "mg":
        value /= 1000
    elif unit in ("µg", "ug", "mcg"):
        value /= 1e6
    return value


def parse_energy(text, name: str) -> tuple[float | None, float | None]:
    """Return (kJ, kcal) from an energy cell: '541kJ / 129kcal', '1264kJ/', '304kcal', '541/129'."""
    if text is None:
        return None, None
    s = str(text).lower().replace(" ", "")
    s = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", s)  # thousands separator: 1,264kJ
    s = re.sub(r"\(?[<\d.,]*%[^)]*\)?", "", s)  # %RI: '824 kJ (10%*)'
    m = re.fullmatch(rf"({NUMBER})/({NUMBER})\(?(kj|kcal)/(kj|kcal)\)?", s)  # '2506/603 kJ/kcal'
    if m and m.group(3) != m.group(4):
        first, second = to_float(m.group(1)), to_float(m.group(2))
        return (first, second) if m.group(3) == "kj" else (second, first)
    kj = kcal = None
    pairs = re.findall(rf"({NUMBER})(kj|kcal|cal)?", s)
    unitless = []
    for value, unit in pairs:
        v = to_float(value)
        if unit == "kj":
            kj = kj if kj is not None else v
        elif unit in ("kcal", "cal"):
            kcal = kcal if kcal is not None else v
        else:
            unitless.append(v)
    if unitless:
        lname = name.lower()
        has_kj, has_kcal = "kj" in lname, "kcal" in lname or "cal" in lname
        if has_kj and has_kcal and len(unitless) >= 2 and kj is None and kcal is None:
            kj, kcal = unitless[0], unitless[1]
        elif has_kcal and not has_kj and kcal is None:
            kcal = unitless[0]
        elif has_kj and not has_kcal and kj is None:
            kj = unitless[0]
        elif kj is None and kcal is None and len(unitless) >= 2:
            kj, kcal = unitless[0], unitless[1]
        elif kj is not None and kcal is None:
            kcal = unitless[0]
        elif kcal is not None and kj is None:
            kj = unitless[0]
        elif kj is None and kcal is None:
            kcal = ("?", unitless[0])  # decided later from the macros
    return kj, kcal


def column_has_values(rows: list[dict], key: str) -> bool:
    found = 0
    for row in rows[1:]:
        if classify_row(row.get("name") or "") in ("energy", "protein", "fat", "carb", "salt"):
            if parse_amount(row.get(key)) is not None:
                found += 1
    return found >= 2


def choose_column(rows: list[dict]) -> tuple[str | None, float, int, str]:
    """Pick the per-100g column. Returns (key, scale to per-100g, flags, header text)."""
    header = rows[0]
    cols = [(k, (header.get(k) or "").strip()) for k in VALUE_KEYS]
    per100 = [(k, h) for k, h in cols
              if PER100_RE.search(h) and not PERCENT_COLUMN_RE.search(h) and column_has_values(rows, k)]
    if not per100 and column_has_values(rows, "value1") and not cols[0][1]:
        per100 = [cols[0]]  # some tables leave the header blank; value1 is then per 100g
    if per100:
        as_sold = [(k, h) for k, h in per100 if AS_SOLD_RE.search(h) or not COOKED_RE.search(h)]
        key, text = (as_sold or per100)[0]
        return key, 1.0, (0 if as_sold else F_COOKED), text
    # No per-100g column: scale a 'Per 30g' serving column instead.
    for key, text in cols:
        m = SERVING_WEIGHT_RE.search(text)
        if m and not PERCENT_COLUMN_RE.search(text) and column_has_values(rows, key):
            grams = to_float(m.group(1))
            if 5 <= grams <= 1000:
                flags = F_SCALED | (F_COOKED if COOKED_RE.search(text) and not AS_SOLD_RE.search(text) else 0)
                return key, 100 / grams, flags, text
    return None, 1.0, 0, ""


def read_column(rows: list[dict], key: str) -> dict:
    """Read the label macros from one column. Values are as printed (not yet scaled)."""
    values: dict = {}
    energy = {"kj": None, "kcal": None}
    previous = None
    for row in rows[1:]:
        name = row.get("name") or ""
        field = classify_row(name)
        cell = row.get(key)
        if field is None and name.strip() in ("-", "") and previous == "energy":
            field = "energy"  # 'Energy' = '1264kJ/' continued by a row named '-' = '304kcal'
        previous = field if field else (previous if name.strip() in ("-", "") else None)
        if field is None:
            continue
        if field != "energy" and re.search(r"k?cal|kj", str(cell or ""), re.I):
            field = "energy"  # an energy value spilling into the next row: 'Fat' = '(194kcal)'
        if field == "energy":
            kj, kcal = parse_energy(cell, name)
            if energy["kj"] is None and kj is not None:
                energy["kj"] = kj
            if energy["kcal"] is None and kcal is not None:
                energy["kcal"] = kcal
        elif field not in values:
            amount = parse_amount(cell)
            if amount is not None:
                values[field] = amount
    if "salt" not in values and "sodium" in values:
        values["salt"] = values["sodium"] * 2.5
    values["_kj"], values["_kcal"] = energy["kj"], energy["kcal"]
    return values


def macro_kcal(v: dict) -> float | None:
    if "protein" not in v or "fat" not in v or "carb" not in v:
        return None
    polyols = v.get("polyols", 0.0)
    # UK labels: carbohydrate includes polyols (2.4 kcal/g); fibre is listed separately at 2 kcal/g.
    return 4 * v["protein"] + 4 * (v["carb"] - polyols) + 2.4 * polyols + 9 * v["fat"] + 2 * v.get("fibre", 0.0)


def resolve_kcal(v: dict) -> float | None:
    kj, kcal = v.pop("_kj"), v.pop("_kcal")
    if isinstance(kcal, tuple):  # one unitless energy number: kJ or kcal, whichever fits the macros
        number = kcal[1]
        estimate = macro_kcal(v)
        if estimate is not None and abs(number * KCAL_PER_KJ - estimate) < abs(number - estimate):
            kj, kcal = number, None
        else:
            kcal = number
    if kcal is not None:
        return kcal
    if kj is not None:
        return kj * KCAL_PER_KJ
    return None


def cooked_yield(rows: list[dict]) -> float | None:
    """Cooked weight / as-sold weight from a footer like '400g typically weighs 354g'."""
    for row in rows[1:]:
        m = YIELD_RE.search(row.get("name") or "")
        if m:
            raw, cooked = to_float(m.group(1)), to_float(m.group(2))
            if raw > 0 and 0.3 <= cooked / raw <= 6:
                return cooked / raw
    return None


def footer_says_cooked(rows: list[dict]) -> bool:
    for row in rows[1:]:
        if all(parse_amount(row.get(k)) is None for k in VALUE_KEYS):
            if COOKED_FOOTER_RE.search(row.get("name") or ""):
                return True
    return False


def parse_nutrition(rows: list[dict]) -> dict | None:
    """Per-100g macros {kcal, fat, ..., _flags, _column} or None if there is no usable table."""
    if not rows or (rows[0].get("name") or "").strip().lower() != "typical values":
        return None
    key, scale, flags, header_text = choose_column(rows)
    if key is None:
        return None
    v = read_column(rows, key)
    v["kcal"] = resolve_kcal(v)
    if "protein" not in v:
        return None
    if not AS_SOLD_RE.search(header_text) and footer_says_cooked(rows):
        flags |= F_COOKED
    if flags & F_COOKED:
        # Per 100g cooked x (cooked weight / raw weight) = per 100g as sold. Rice, pasta and pulse labels
        # often give a yield that doesn't match their cooked values (75g rice -> 235g, but 154 kcal/100g
        # cooked would make 482 kcal/100g dry), so keep the cooked values when the result is impossible.
        ratio = cooked_yield(rows)
        macros = sum(v.get(n) or 0 for n in ("protein", "carb", "fat", "fibre"))
        if ratio and macros * scale * ratio <= 100 and (v["kcal"] or 0) * scale * ratio <= 900:
            scale *= ratio
            flags = (flags & ~F_COOKED) | F_YIELD
    out = {n: (round(v[n] * scale, 2) if v.get(n) is not None else None) for n in NUTRIENTS}
    if re.search(r"100\s*ml", header_text, re.I) and not re.search(r"100\s*g", header_text, re.I):
        flags |= F_LIQUID
    out["_flags"] = flags
    out["_column"] = key
    out["_polyols"] = v.get("polyols", 0.0) * scale
    return out


# ---------------------------------------------------------------- weight estimate for 'each' items

ADJECTIVES = r"(?:(?:typical|average|avg|approx\.?|medium|large|small|mixed|sized?|whole|individual)\s+)*"
COUNTED_ITEM_RE = re.compile(rf"\b(\d+)[\s-]*{ADJECTIVES}([a-z]{{3,}})", re.I)        # '(2 Large Eggs)'
SINGLE_ITEM_RE = re.compile(rf"\b(?:per|one|0ne|each|an?)\s+{ADJECTIVES}([a-z]{{3,}})", re.I)  # 'per roll'
FRACTION_RE = re.compile(r"\b1\s*/\s*(\d+)|(\d+)\s*(?:st|nd|rd|th)\b", re.I)
UNICODE_FRACTIONS = {"½": 2, "⅓": 3, "¼": 4, "⅕": 5, "⅙": 6, "⅛": 8}
SERVINGS_FOOTER_RE = re.compile(
    r"(?:pack|box|tray|tub) (?:contains|provides|makes) (?:approx\.? )?(\d+) (?:servings?|portions?)|"
    r"(\d+) (?:servings?|portions?) per (?:pack|box)|number of (?:servings|portions):?\s*(\d+)", re.I)
PACK_SERVING_RE = re.compile(r"\b(?:per|each|whole|one|1|the)\s+(?:portion\s+)?pack\b|^\s*pack\s*$", re.I)
GENERIC_SERVING_RE = re.compile(r"serving|portion|contains|provides|^\s*per\s*$", re.I)
EGG_WEIGHTS = [("very large", 68), ("large", 61), ("medium", 52), ("small", 44), ("mixed", 50)]  # edible g/egg


def items_per_serving(header: str, title: str) -> int | None:
    """Items per serving if the header counts the same item as the title ('One roll' / '4 White Rolls').

    'Per 2-biscuit serving' on 'Weetabix 48 Pack' or '2 slices of mango' on 'Mango Twin Pack' return None.
    """
    text = SERVING_WEIGHT_RE.sub("", header)
    lower_title = title.lower()
    for pattern, count in ((COUNTED_ITEM_RE, True), (SINGLE_ITEM_RE, False)):
        m = pattern.search(text)
        if m:
            noun = m.group(2 if count else 1).lower()
            stem = re.sub(r"(?:es|s)$", "", noun) if len(noun) > 4 else noun
            if stem in lower_title:
                items = int(m.group(1)) if count else 1
                return items if 0 < items <= 12 else None
    return None


def estimate_each_weight(title: str, rows: list[dict] | None, nut: dict | None, aisle: str) -> float | None:
    """Pack weight in kg for an item sold 'each' with no pack size or title weight, from the label.

    Egg weights are without the shell, like the label's 'One typical egg (61g)'.
    """
    if rows and nut and nut.get("kcal"):
        header = rows[0]
        footer_servings = None
        for row in rows[1:]:
            m = SERVINGS_FOOTER_RE.search(row.get("name") or "")
            if m:
                footer_servings = int(next(g for g in m.groups() if g))
                break
        for key in VALUE_KEYS:
            text = (header.get(key) or "").strip()
            if key == nut["_column"] or not text or PERCENT_COLUMN_RE.search(text):
                continue
            # Serving weight: stated ('One typical egg (61g)') or from the energy ratio to the per-100g column.
            m = SERVING_WEIGHT_RE.search(text)
            if m:
                grams = to_float(m.group(1))
            else:
                serving = read_column(rows, key)
                serving_kcal = resolve_kcal(serving)
                if not serving_kcal:
                    continue
                grams = 100 * serving_kcal / nut["kcal"]
            if not 5 <= grams <= 3000:
                continue
            # Servings in the pack.
            servings = None
            fraction = FRACTION_RE.search(text)
            if fraction:
                servings = int(fraction.group(1) or fraction.group(2))
            elif any(ch in text for ch in UNICODE_FRACTIONS):
                servings = next(n for ch, n in UNICODE_FRACTIONS.items() if ch in text)
            elif re.search(r"\bhalf\b", text, re.I):
                servings = 2
            elif PACK_SERVING_RE.search(text):
                servings = 1
            elif items := items_per_serving(text, title):
                # Per item ('per roll', 'One roll (70g)', 'Per Serving (2 Large Eggs)'): count from the title.
                servings = (title_count(title) or 1) / items
            elif GENERIC_SERVING_RE.search(text):
                servings = footer_servings
            if servings:
                return grams * servings / 1000
    if aisle == "Eggs" or re.search(r"\beggs\b", title, re.I):
        count = title_count(title)
        if count:
            lower = title.lower()
            grams = next((g for size, g in EGG_WEIGHTS if size in lower), 55)
            return count * grams / 1000
    return None


# ---------------------------------------------------------------- edible share (bone, eggshell)

# Labels give nutrition for the edible part (meat off the bone, egg out of its shell), but the pack weight
# includes the bone or shell. Wing labels show it: 19 g protein per 100 g, with ~46% of the weight bone,
# would make the meat itself 35 g/100 g, more than any raw chicken. Edible shares are rough, from USDA
# refuse figures. First match wins.
BONE_IN_DEPARTMENT_RE = re.compile(r"meat|poultry|festive", re.I)
BONELESS_RE = re.compile(
    r"boneless|fillet|tender|strips?\b|pieces|diced|mince|slices|\bpie\b|plant|meat free|\bno-|isn'?t", re.I)
EDIBLE_SHARES = [
    (re.compile(r"thighs? (?:&|and) drumsticks?|drumsticks? (?:&|and) thighs?", re.I), 0.75),
    (re.compile(r"\bwings\b", re.I), 0.54),
    (re.compile(r"\bdrumsticks?\b", re.I), 0.70),
    (re.compile(r"\b(?:chicken|duck|turkey) legs?\b|\bleg quarters?\b", re.I), 0.73),
    (re.compile(r"\bthighs\b", re.I), 0.80),
    (re.compile(r"\bwhole\b(?:\s+\S+){0,2}\s+(?:chicken|turkey|duck|goose)\b|\bwhole bird\b|turkey bird|"
                r"spatchcock|poussin|half chicken", re.I), 0.68),
    (re.compile(r"\bcrown\b", re.I), 0.80),  # breast on the bone
    (re.compile(r"\blamb chops\b", re.I), 0.75),
    (re.compile(r"\bchops\b", re.I), 0.80),
    (re.compile(r"\bribs\b|\brib rack\b", re.I), 0.70),
    (re.compile(r"\bshanks?\b", re.I), 0.70),
    (re.compile(r"\blamb (?:\w+ )?leg joint\b|\bleg of lamb\b", re.I), 0.80),
    (re.compile(r"bone[- ]in|on the bone|\d+[- ]bone|t-bone|tomahawk|wing rib", re.I), 0.85),
]
SHELL_EGG_SHARE = 0.88  # the shell is about 12% of an egg's weight


def edible_share(title: str, department: str, aisle: str, gross_weight: bool) -> float:
    """Share of the pack weight that the label's nutrition describes (1.0 when it's all edible).

    gross_weight: the weight came from the pack size or title (eggs: with shells), not the label's serving size.
    """
    if aisle == "Eggs":
        is_shell_egg = re.search(r"\beggs\b", title, re.I) and not re.search(r"liquid", title, re.I)
        return SHELL_EGG_SHARE if is_shell_egg and gross_weight else 1.0
    if not BONE_IN_DEPARTMENT_RE.search(department) or BONELESS_RE.search(title):
        return 1.0
    return next((share for pattern, share in EDIBLE_SHARES if pattern.search(title)), 1.0)


# ---------------------------------------------------------------- prices

UOM_TO_KG = {"kg": 1, "litre": 1, "kg DR.WT": 1, "litre DR.WT": 1, "100g": 10, "100ml": 10, "10g": 100}
MONEY_RE = r"(?:£\s*(\d+(?:\.\d+)?)|(\d+)\s*p\b)"


def money(m_pound, m_pence) -> float:
    return float(m_pound) if m_pound else int(m_pence) / 100


def pack_kg(product: dict, title: str) -> tuple[float | None, str]:
    """(kg, source) from details.packSize, fixing unit slips like '400 KG' for a 400g pack."""
    sizes = (product.get("details") or {}).get("packSize") or []
    if not sizes or not sizes[0].get("value"):
        return None, ""
    unit = (sizes[0].get("units") or "").lower()
    if unit not in WEIGHT_UNITS:
        return None, ""
    try:
        kg = float(sizes[0]["value"]) * WEIGHT_UNITS[unit]
    except ValueError:
        return None, ""
    if kg <= 0:
        return None, ""
    # Unit slips: {"400", "KG"} for a '400G' meal, {"240", "KG"} for a can with 240g drained weight,
    # {"1", "G"} for a '1Kg' bag. Shift by 1000 rather than use the title, which has the net weight.
    from_title = title_weight_kg(title)
    if from_title:
        ratio = kg / from_title
        if 200 < ratio < 5000:
            return kg / 1000, "fixed"
        if 1 / 5000 < ratio < 1 / 200:
            return kg * 1000, "fixed"
    return kg, "pack"


def clubcard_price(promotions: list[dict], price: float, ppk: float | None, kg: float | None):
    """Best Clubcard price per item (and per kg), or None. Meal deals are ignored.

    Returns (price per item, price per kg, is_multibuy, description).
    """
    best = None
    for promo in promotions or []:
        if "CLUBCARD_PRICING" not in (promo.get("attributes") or []):
            continue
        desc = (promo.get("description") or "").strip()
        if not desc or re.search(r"meal deal", desc, re.I):
            continue
        item, item_ppk, multibuy = None, None, False
        m = re.search(rf"{MONEY_RE}\s*per\s*kg\b", desc, re.I)
        if m:  # loose produce: '£2.50 per kg Clubcard Price', 'Save 25% £1.50 per kg Clubcard Price'
            item_ppk = money(*m.groups())
            item = item_ppk * kg if kg else None
        elif re.match(rf"^{MONEY_RE}", desc):  # '£2.50 Clubcard Price', '£3.00 Save 25% Clubcard Price'
            item = money(*re.match(rf"^{MONEY_RE}", desc).groups())
        else:
            # Multibuys: 'Any 3 for £10', '4 for £3 or 8 for £5', 'Any 3 for 2' (cheapest free).
            options = []
            for n, pounds, pence, m_items in re.findall(
                    rf"(\d+)\s*for\s*(?:£\s*(\d+(?:\.\d+)?)|(\d+)\s*p\b|(\d+)\b)", desc, re.I):
                n = int(n)
                if pounds or pence:
                    options.append(money(pounds, pence) / n)
                elif m_items and int(m_items) < n:
                    options.append(price * int(m_items) / n)
            if options:
                item, multibuy = min(options), True
        if item is None and item_ppk is None:
            continue
        if item_ppk is None and item is not None and ppk is not None:
            item_ppk = ppk * item / price
        if item is not None and not 0 < item < price:
            continue
        cost = item if item is not None else item_ppk  # only loose produce lacks a per-item price
        if best is None or cost < best[0]:
            best = (cost, (item, item_ppk, multibuy, desc))
    return best[1] if best else None


# ---------------------------------------------------------------- build

def fix_mojibake(text: str) -> str:
    """'Vanilla CrÃ¨me' -> 'Vanilla Crème' (UTF-8 that was decoded as cp1252 somewhere upstream)."""
    if "Ã" in text or "â€" in text:
        try:
            return text.encode("cp1252").decode("utf-8")
        except UnicodeError:
            pass
    return text


def build_record(tpnc: str, listed: dict, product: dict) -> dict:
    title = fix_mojibake(" ".join((product.get("title") or listed.get("title") or "").split()))
    # Price and promotions from the listing (one snapshot); fall back to the product file.
    price_block = listed.get("price") or product.get("price") or {}
    promotions = listed.get("promotions") if "promotions" in listed else product.get("promotions")
    price = price_block.get("actual")
    unit_price = price_block.get("unitPrice")
    uom = price_block.get("unitOfMeasure")
    rows = (product.get("details") or {}).get("nutrition") or []
    nut = parse_nutrition(rows)
    aisle = listed.get("aisleName") or product.get("aisleName") or ""
    department = listed.get("departmentName") or product.get("departmentName") or ""

    flags = 0
    kg, source = pack_kg(product, title)
    ppk = None
    if price:
        if kg:
            ppk = price / kg
        elif uom in UOM_TO_KG and unit_price:
            ppk = unit_price * UOM_TO_KG[uom]  # loose produce sold by weight
            kg = price / ppk
            source = "unit"
        elif uom == "each":
            kg, source = title_weight_kg(title), "title"
            if not kg:
                kg, source = estimate_each_weight(title, rows, nut, aisle), "estimate"
            if kg:
                ppk = price / kg
                flags |= F_EST_WEIGHT
                if not PPK_RANGE[0] <= ppk <= PPK_RANGE[1]:
                    kg = ppk = None
        if source == "fixed":
            flags |= F_EST_WEIGHT
    if ppk is not None:
        if not PPK_RANGE[0] <= ppk <= PPK_RANGE[1]:
            flags |= F_BAD_PRICE
        elif source == "pack" and uom in UOM_TO_KG and unit_price and unit_price > 0.01:
            tesco_ppk = unit_price * UOM_TO_KG[uom]
            if abs(ppk / tesco_ppk - 1) > 0.2:
                flags |= F_BAD_PRICE

    cc = clubcard_price(promotions, price, ppk, kg) if price else None
    if cc and cc[2]:
        flags |= F_MULTIBUY

    # Price per kg of the part the label describes (after the unit price check above, which uses the pack weight).
    edible = edible_share(title, department, aisle, gross_weight=source != "estimate")
    if ppk is not None and edible < 1:
        ppk /= edible
        if cc and cc[1] is not None:
            cc = (cc[0], cc[1] / edible, *cc[2:])
        flags |= F_EDIBLE

    if nut:
        flags |= nut["_flags"]
        macros = [nut[n] for n in ("fat", "carb", "protein", "fibre") if nut.get(n) is not None]
        if any(nut.get(n) is not None and nut[n] > 100 for n in NUTRIENTS[1:]) or sum(macros) > 105:
            flags |= F_BAD_MACROS
        estimate = macro_kcal({k: v for k, v in nut.items() if v is not None and not k.startswith("_")}
                              | {"polyols": nut["_polyols"]})
        if nut["kcal"] is not None and estimate is not None:
            if abs(estimate - nut["kcal"]) > max(0.25 * nut["kcal"], 20):
                flags |= F_BAD_KCAL
    if listed.get("isForSale") is False or product.get("isForSale") is False:
        flags |= F_NOT_FOR_SALE
    if not nut or not nut["_flags"] & F_LIQUID:
        sizes = (product.get("details") or {}).get("packSize") or []
        if (sizes and (sizes[0].get("units") or "").lower() in ("ml", "cl", "l")) or uom in ("litre", "litre DR.WT"):
            flags |= F_LIQUID

    return {
        "tpnc": tpnc,
        "title": title,
        "brand": listed.get("brandName") or product.get("brandName"),
        "image": listed.get("defaultImageUrl") or product.get("defaultImageUrl"),
        "categories": listed.get("categories") or [],
        "department": department,
        "aisle": aisle,
        "price": price,
        "kg": round(kg, 4) if kg else None,
        "weight_source": source if kg else "",
        "ppk": round(ppk, 3) if ppk else None,
        "cc_price": round(cc[0], 2) if cc and cc[0] is not None else None,
        "cc_ppk": round(cc[1], 3) if cc and cc[1] is not None else None,
        "cc_desc": cc[3] if cc else None,
        "nutrition": {n: nut[n] for n in NUTRIENTS} if nut else None,
        "flags": flags,
    }


def compact(records: list[dict], generated: str, scraped: str, excluded: dict) -> dict:
    """Rows of arrays with lookup tables for repeated strings, to keep data.json small."""
    departments = sorted({r["department"] for r in records})
    aisles = sorted({r["aisle"] for r in records})
    promos = sorted({r["cc_desc"] for r in records if r["cc_desc"]})
    dept_index = {d: i for i, d in enumerate(departments)}
    aisle_index = {a: i for i, a in enumerate(aisles)}
    promo_index = {p: i for i, p in enumerate(promos)}
    columns = ["tpnc", "title", "img", "cat", "dept", "aisle", "price", "kg", "ppk", "cc", "ccppk", "promo",
               *NUTRIENTS, "flags"]
    rows = []
    for r in records:
        image = r["image"] or ""
        image = image.split("?")[0].removeprefix(IMAGE_PREFIX) if image.startswith(IMAGE_PREFIX) else image
        if "no-image" in image:
            image = ""
        cat = sum(1 << CATEGORIES.index(c) for c in r["categories"] if c in CATEGORIES)
        rows.append([
            r["tpnc"], r["title"], image, cat, dept_index[r["department"]], aisle_index[r["aisle"]],
            r["price"], r["kg"], r["ppk"], r["cc_price"], r["cc_ppk"],
            promo_index[r["cc_desc"]] if r["cc_desc"] else None,
            *(r["nutrition"][n] for n in NUTRIENTS), r["flags"],
        ])
    return {
        "generated": generated,
        "scraped": scraped,
        "imagePrefix": IMAGE_PREFIX,
        "categories": CATEGORIES,
        "departments": departments,
        "aisles": aisles,
        "promos": promos,
        "flags": {name: bit for bit, name in FLAG_NAMES.items()},
        "excluded": excluded,
        "columns": columns,
        "rows": rows,
    }


def load_products(listing: dict):
    for tpnc, listed in listing["products"].items():
        path = RAW_DIR / f"{tpnc}.json"
        if path.exists():
            yield tpnc, listed, read_json(path)["product"]
        else:
            yield tpnc, listed, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build docs/data.json from data/raw/.")
    parser.add_argument("--check", metavar="TPNC", action="append", help="Print the parsed record for a product.")
    parser.add_argument("--out", type=Path, default=OUT_PATH, help=f"Output path (default {OUT_PATH.relative_to(ROOT)}).")
    args = parser.parse_args()

    if not LISTING_PATH.exists():
        sys.exit(f"{LISTING_PATH.relative_to(ROOT)} not found; run scraper/fetch.py first.")
    listing = read_json(LISTING_PATH)

    if args.check:
        for tpnc in args.check:
            listed = listing["products"].get(tpnc, {})
            product = read_json(RAW_DIR / f"{tpnc}.json")["product"]
            record = build_record(tpnc, listed, product)
            record["flag_names"] = [name for bit, name in FLAG_NAMES.items() if record["flags"] & bit]
            print(json.dumps(record, indent=2, ensure_ascii=False))
        return

    records, stats = [], Counter()
    for tpnc, listed, product in load_products(listing):
        stats["listed"] += 1
        if product is None:
            stats["not fetched"] += 1
            continue
        record = build_record(tpnc, listed, product)
        if record["nutrition"] is None:
            stats["no nutrition"] += 1
            continue
        if not record["price"]:
            stats["no price"] += 1
            continue
        records.append(record)
        stats["kept"] += 1
        stats["ranked (has price/kg)"] += record["ppk"] is not None
        stats["clubcard price"] += record["cc_price"] is not None or record["cc_ppk"] is not None
        for bit, name in FLAG_NAMES.items():
            stats[f"flag {name}"] += bool(record["flags"] & bit)
        stats[f"weight from {record['weight_source'] or 'nothing'}"] += 1

    records.sort(key=lambda r: r["tpnc"])
    scraped = (listing.get("updated") or "")[:10]
    generated = datetime.date.today().isoformat()
    excluded = {k: stats[k] for k in ("no nutrition", "no price", "not fetched") if stats[k]}
    data = compact(records, generated, scraped, excluded)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    width = max(len(k) for k in stats)
    for key in sorted(stats, key=lambda k: (not k.startswith(("listed", "kept")), k)):
        print(f"  {key:<{width}}  {stats[key]:>6}")
    size = args.out.stat().st_size
    print(f"Wrote {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}: "
          f"{len(records)} products, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
