import argparse
import base64
import datetime
import os
import sqlite3
import time
import urllib.parse

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://xapi.tesco.com/"

HEADERS = {
    "x-apikey": os.environ["TESCO_API_KEY"],
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "region": "UK",
    "language": "en-GB",
}

CATEGORY_TPNCS_QUERY = """
query GetCategoryTPNCs($facet: ID, $offset: Int, $count: Int) {
    category(facet: $facet, offset: $offset, count: $count) {
        pageInformation: info { totalCount: total }
        results {
            node {
                ... on ProductInterface { tpnc title }
            }
        }
    }
}
"""

PRODUCT_NUTRITION_PRICE_QUERY = """
query GetProductNutritionAndPrice($tpnc: String) {
  product(tpnc: $tpnc) {
    title
    brandName
    aisleName
    price {
      actual
      unitPrice
      unitOfMeasure
    }
    details {
      nutritionInfo: nutrition {
        ...Nutrition
      }
    }
  }
}

fragment Nutrition on NutritionalInfoItemType {
  name
  perComp: value1
  perServing: value2
}
"""


def build_category_facet(label: str) -> str:
    # Mimic JS encodeURI, which Tesco uses: "Treats & Snacks" -> "Treats%20&%20Snacks"
    encoded = base64.b64encode(urllib.parse.quote(label, safe=";,/?:@&=+$!*'()#").encode()).decode()
    return f"b;{encoded}"


def post_graphql(query: str, variables: dict) -> dict:
    response = requests.post(API_URL, json={"query": query, "variables": variables}, headers=HEADERS)
    response.raise_for_status()
    payload = response.json()
    if "errors" in payload:
        raise RuntimeError(f"GraphQL returned errors: {payload['errors']}")
    return payload.get("data", {})


def fetch_category_tpnc_list(facet: str, count: int = 100) -> list[dict]:
    results = []
    offset = 0

    while True:
        data = post_graphql(
            CATEGORY_TPNCS_QUERY,
            {"facet": facet, "offset": offset, "count": count},
        )
        category = data.get("category")
        if not category:
            break

        total = category.get("pageInformation", {}).get("totalCount")
        page_results = category.get("results", [])
        if not page_results:
            break

        for item in page_results:
            node = item.get("node") or {}
            tpnc = node.get("tpnc")
            title = node.get("title")
            if tpnc:
                results.append({"tpnc": tpnc, "title": title})

        offset += len(page_results)
        print(f"Fetching TPNC for {offset}/{total} products...")
        if total is None or offset >= total:
            break
        if len(page_results) == 0:
            break

    return results


def parse_float(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    cleaned = text.replace("g", "").replace("G", "").replace("mg", "").replace("µg", "").replace("kcal", "")
    cleaned = cleaned.replace("–", "-").replace("—", "-").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_protein(nutrition_info: list[dict]) -> tuple[float | None, str | None]:
    if not nutrition_info:
        return None, None

    protein_item = None
    typical_values_item = None
    for item in nutrition_info:
        name = (item.get("name") or "").strip().lower()
        if "protein" in name:
            protein_item = item
        if "typical values" in name:
            typical_values_item = item

    if protein_item is None:
        return None, None

    protein_value = parse_float(protein_item.get("perComp"))
    protein_units = parse_float(typical_values_item.get("perComp"))
    return protein_value, protein_units


def compute_price_per_kg(price_block: dict) -> tuple[float | None, str | None]:
    if not price_block:
        return None, None

    unit_price = parse_float(price_block.get("unitPrice"))
    unit_measure = (price_block.get("unitOfMeasure") or "").strip().lower()
    if unit_price is None:
        return None, unit_measure

    if unit_measure == "kg":
        return unit_price, "kg"
    if unit_measure == "g":
        return unit_price * 1000.0, "kg"
    if unit_measure in {"100g", "100 g", "per 100g"}:
        return unit_price * 10.0, "kg"
    return unit_price, unit_measure


def fetch_product_details(tpnc: str) -> dict:
    data = post_graphql(
        PRODUCT_NUTRITION_PRICE_QUERY,
        {"tpnc": tpnc},
    )
    product = data.get("product") or {}
    price_info = product.get("price") or {}
    nutrition_info = (product.get("details") or {}).get("nutritionInfo") or []

    protein_per_100g, protein_units = extract_protein(nutrition_info)
    price_per_kg, price_unit = compute_price_per_kg(price_info)

    return {
        "tpnc": tpnc,
        "title": product.get("title"),
        "brand": product.get("brandName"),
        "aisleName": product.get("aisleName"),
        "price_actual": parse_float(price_info.get("actual")),
        "price_unit_price": parse_float(price_info.get("unitPrice")),
        "price_unit_of_measure": price_info.get("unitOfMeasure"),
        "price_per_kg": price_per_kg,
        "price_per_kg_unit": price_unit,
        "protein_per_100g": protein_per_100g,
        "protein_units": protein_units,
    }


def save_to_csv(rows: list[dict], csv_path: str) -> None:
    df = pd.DataFrame(rows)
    df.to_csv(csv_path, index=False)
    print(f"Saved {len(df)} rows to {csv_path}")

def load_tpnc_file(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def main():
    parser = argparse.ArgumentParser(description="Scrape Tesco product nutrition and price data.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--category-label", nargs="+", help="Category label to encode for the Tesco facet, e.g. 'Fresh Food'.")
    parser.add_argument("--count", type=int, default=100, help="Page size for category pagination.")
    parser.add_argument("--csv", default="products.csv", help="Output CSV path.")
    args = parser.parse_args()

    tpnc_items = []
    for category in args.category_label:
        facet = build_category_facet(category)
        category_tpnc_items = fetch_category_tpnc_list(facet, count=args.count)
        tpnc_items.extend(category_tpnc_items)
        print(f"Found {len(category_tpnc_items)} products in category {category}.")

    rows = []
    for index, item in enumerate(tpnc_items, start=1):
        tpnc = item["tpnc"]
        try:
            details = fetch_product_details(tpnc)
            details["category"] = args.category_label
            rows.append(details)
            print(f"{index}/{len(tpnc_items)}: {tpnc} -> protein={details['protein_per_100g']} price_per_kg={details['price_per_kg']}")
        except Exception as exc:
            print(f"Error fetching {tpnc}: {exc}")

    if not rows:
        print("No product rows collected.")
        return

    save_to_csv(rows, args.csv)

if __name__ == "__main__":
    main()
