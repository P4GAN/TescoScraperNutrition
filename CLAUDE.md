# Tesco Protein Finder

Scrape Tesco UK grocery products (price + nutrition label) and publish a GitHub Pages site that ranks food by protein per £. The site filters out cheap-but-impractical protein (flour, potatoes) using protein share of calories and other label macros.

**Roadmap and status live in [PLAN.md](PLAN.md).** Read it first, and tick items off as they're done.

## Decisions already made (don't re-ask)
- **Nutrients:** use Tesco label macros only (energy, fat, saturates, carbs, sugars, fibre, protein, salt). No micronutrient database for v1.
- **Price:** store both the shelf price and the Clubcard price; the site has a toggle to rank by either.
- **Refresh:** the user runs the scraper manually from their own machine and commits the output JSON. No GitHub Actions scraping (bot protection, request volume).
- **Frontend:** plain HTML + vanilla JS, no build step, in `docs/`, served by GitHub Pages ("deploy from branch", `main` / `/docs`). Chart libraries only from a CDN.

## Layout
```
scraper/scraping.py   v0 scraper (legacy; to be replaced by fetch.py + build.py, see PLAN.md)
data/FreshFood.csv    v0 output for Fresh Food, April 2026. Has known bugs (see below); OK for prototyping the site
data/raw/             per-product raw API JSON cache (gitignored)
notebooks/            exploration
docs/                 GitHub Pages site (not created yet)
.env                  TESCO_API_KEY (gitignored; see .env.example)
```
Python env: the user's anaconda Python 3.11 (`pandas`, `requests`, `python-dotenv`).

## Tesco API: verified facts (2026-09-28)
- **Request:** a GraphQL `POST` to `https://xapi.tesco.com/`. Headers are in `scraper/scraping.py` (`x-apikey` comes from `.env`, plus `region: UK` and `language: en-GB`).
- **Category facet:** `"b;" + base64(encodeURI(label))`. `&` is **not** percent-encoded (`Treats%20&%20Snacks`). `build_category_facet` handles this.
- **Top-level category sizes:**

  | Category | Products |
  |---|---|
  | Fresh Food | 4769 |
  | Bakery | 937 |
  | Frozen Food | 1144 |
  | Food Cupboard | 8729 |
  | Treats & Snacks | 2789 |

  Categories **overlap** (e.g. Muller Corner yogurts are in both Fresh Food and Treats & Snacks), so dedupe by `tpnc` and keep every category a product belongs to.
- **Listing query:** `category(facet, offset, count) { info { total } products { ... } }`. Verified `products` fields: `tpnc tpnb adId title brandName departmentName aisleName shelfName defaultImageUrl averageWeight price { actual unitPrice unitOfMeasure } promotions { ... }`.
  - Pages include **sponsored items** (non-null `adId`) *on top of* `count` organic items (asked 50, got 53). Skip ads, and advance `offset` by the number of **organic** items. The v0 scraper advanced by the total, so it skipped products: 4448 unique in the CSV vs 4769 listed.
  - The listing does **not** return nutrition. That needs one `product(tpnc)` call per product (~0.5 s each).
- **Batching:** putting several `product()` calls in one request with GraphQL aliases returns **HTTP 400**, so it's one request per product. Use 3–4 concurrent workers at most, plus retry/backoff.
- **Product query:** `product(tpnc: String)`. Verified fields: `title brandName departmentName aisleName shelfName defaultImageUrl price { actual unitPrice unitOfMeasure } promotions { description unitSellingInfo attributes } details { packSize { value units } nutrition { name value1 value2 value3 value4 } }`.
  - `details.packSize` gives the real pack weight/volume (e.g. `{"value": "90", "units": "G"}`). Use it to compute price/kg, and cross-check against `unitPrice`.
- **Nutrition rows:** a list of `{name, value1..value4}`. The first row, `name: "Typical Values"`, is the **header**. Pick the column whose header says per 100g/100ml. Don't assume `value1`.
  - Usually `value1 = "Per 100g"`, but e.g. The Gym Kitchen meals have `value1 = "(microwaved) Per pack"` and `value2 = "(microwaved) Per 100g"`. v0 read the per-pack value (59 g protein) as per 100g.
  - Energy comes either as one `Energy` row (`"541kJ / 129kcal"`) or as separate `Energy kJ` / `Energy kcal` rows.
  - Name variants: `Fat` / `Fat, total`; `Saturates` / `- saturates`; `Sugars` / `- sugars`.
  - Footer rows (`†Reference intake…`, `This pack provides 1 serving`) have values of `-`. Values can look like `0g`, `<0.5g` or `trace`.
- **Clubcard price:** a `promotions[]` entry whose `attributes` contains `"CLUBCARD_PRICING"`.
  - `unitSellingInfo` usually holds the Clubcard unit price (`"£4.03/kg"`). It's `null` for multibuys.
  - `description` is a string: `"£2.50 Clubcard Price"`, `"£3.00 Save 25% Clubcard Price"`, or multibuy `"4 for £3 or 8 for £5 Clubcard Price - …"`.
  - `price.afterDiscount` is unreliable (it equals the shelf price), so don't use it.
- **`unitOfMeasure` values seen:** `kg`, `litre`, `each`, `100g`, `kg DR.WT`, `litre DR.WT`.
- **Known bad data:** some `unitPrice` values are wrong (a £4.50 400 g meal listed at £0.01/kg), and some products have no nutrition panel (~7% in Fresh Food).
- **Product page URL:** `https://www.tesco.com/groceries/en-GB/products/{tpnc}`

## Conventions
- **Keep fetching and parsing separate.** Fetch raw JSON into `data/raw/{tpnc}.json` (resumable, skip cached), and do all parsing/cleaning in a separate build step that can be re-run without hitting the API.
- **Keep API traffic small.** Probe with a handful of requests. The user runs full-category scrapes themselves.
- **Never commit** `.env`, the API key or `data/raw/`.
