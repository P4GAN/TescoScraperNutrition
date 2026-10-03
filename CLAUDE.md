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
scraper/fetch.py      fetches raw listing + product JSON into data/raw/ (resumable; --limit, --skip-listing, --refresh)
scraper/build.py      parses data/raw/ into docs/data.json (no API calls; --check TPNC prints one parsed record)
scraper/scraping.py   v0 scraper (legacy, superseded by fetch.py + build.py)
data/FreshFood.csv    v0 output for Fresh Food, April 2026 (legacy, has known bugs)
data/raw/             raw API cache (gitignored): {tpnc}.json = {tpnc, fetched, product},
                      _listing.json = {categories: {label: {total, limit, tpncs}}, products: {tpnc: listing fields + categories}},
                      _failures.json = failures from the latest run
notebooks/            exploration
docs/                 GitHub Pages site: index.html, app.js (filter/rank/chart, vanilla JS), style.css,
                      data.json (built; committed). Test with `python -m http.server -d docs`
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
- **Rate limit:** at about 2 req/s some requests get HTTP 429 with `Retry-After: 1`, `X-RateLimit-Limit: 100`, `X-RateLimit-Reset: 1` (the limit is probably shared by everyone using the public key). `fetch.py` caps all workers together at 2 req/s: about 5% of requests get a 429 and all of them succeed on retry.
- **Extra fields** (verified on both listing and product): `gtin isForSale status` and promotion `id startDate endDate`. About 3% of products have `status: "ExcludedProduct"` / `isForSale: false`, but they still have a price.
- **Product query:** `product(tpnc: String)`. Verified fields: `title brandName departmentName aisleName shelfName defaultImageUrl price { actual unitPrice unitOfMeasure } promotions { description unitSellingInfo attributes } details { packSize { value units } nutrition { name value1 value2 value3 value4 } }`.
  - `details.packSize` is a **list** giving the real pack weight/volume (e.g. `[{"value": "90", "units": "G"}]`). Use it to compute price/kg, and cross-check against `unitPrice`. Units seen: `G KG ML L SNGL`.
    - "Each" items have `[{"value": null, "units": "SNGL"}]`, `averageWeight` null/0 and no weight in the title. For eggs, the nutrition header has one (`"One typical egg (61g)"`).
    - Loose items sold by weight (`Tesco Bananas Loose`) have an empty `packSize` and `unitOfMeasure: kg`.
- **Nutrition rows:** a list of `{name, value1..value4}`. The first row, `name: "Typical Values"`, is the **header**. Pick the column whose header says per 100g/100ml. Don't assume `value1`.
  - Usually `value1 = "Per 100g"`, but e.g. The Gym Kitchen meals have `value1 = "(microwaved) Per pack"` and `value2 = "(microwaved) Per 100g"`. v0 read the per-pack value (59 g protein) as per 100g.
  - Header wording varies: `100g contains`, `per 100 g:`, `As sold Per 100g`, `Per 100g (pan-fried)`, `100g of Sausage, as sold, contains`, `Per 100g 74g (2 tubes)`.
  - Some tables have **two** `Per 100g` columns where the second is empty (`"null / null"`, `""`), e.g. Tesco chicken breasts. Others have a `% per 100g` %RI column (Dairylea). So pick the first per-100g column that has values and doesn't start with `%`.
  - Some values are for the cooked product: `(pan-fried)` in the header, or a footer row like `When boiled according to instructions.`
  - Energy comes either as one `Energy` row (`"541kJ / 129kcal"`, `"705kJ/169kcal"`), as separate `Energy kJ` / `Energy (kcal)` rows, or **split over two rows**: `Energy` = `"1264kJ/"` then a row **named `-`** = `"304kcal"`. `-`-named rows also continue micronutrient rows (`"(20% RI***)"`).
  - Name variants: `Fat` / `Fat, total` / `Total Fat` / `Fat (g)`; `Saturates` / `- saturates` / `of which saturates` / `(of which saturates)` / `Saturated Fat` / `of which Sturates` (sic); `Sugars` / `of which sugars` / `Total Sugars`; `Carbohydrate` / `Carbohydrates` / `Available Carbohydrate`; `Fibre` / `Dietary Fiber (EC definition)`; `Salt` / `Equivalent As Salt *to Sodium`. Names can have leading spaces.
  - Footer rows (`†Reference intake…`, `This pack provides 1 serving`, `As sold`) have values of `-`. Values can look like `0g`, `<0.5g`, `< 0.01 g`, `10.00 g`, `trace` or `780mg (=98% NRV*)`.
- **Clubcard price:** a `promotions[]` entry whose `attributes` contains `"CLUBCARD_PRICING"`.
  - `unitSellingInfo` usually holds the Clubcard unit price (`"£4.03/kg"`). It's `null` for multibuys.
  - `description` is a string: `"£2.50 Clubcard Price"`, `"£3.00 Save 25% Clubcard Price"`, or multibuy `"4 for £3 or 8 for £5 Clubcard Price - …"`.
  - `price.afterDiscount` is unreliable (it equals the shelf price), so don't use it.
- **`unitOfMeasure` values seen:** `kg`, `litre`, `each`, `100g`, `kg DR.WT`, `litre DR.WT`.
- **Known bad data:** some `unitPrice` values are wrong (a £4.50 400 g meal listed at £0.01/kg), and some products have no nutrition panel (2,350 of 15,329 overall).
  - `packSize` unit slips: `400 KG` for a 400 g meal, `240 KG` for a can's drained weight, `1 G` for a 1 kg bag, `200 MG`. The title weight shows the factor of 1000.
  - Label errors are common enough to need flags: macros adding up to more than 100 g, kJ printed as kcal, two "Per 100g" columns where one is really per pack.
- **Full-scrape findings (2026-10-03):**
  - `packSize` matches `unitPrice` within 5% for 99.9% of products, including DR.WT (`packSize` is then the drained weight).
  - For `unitOfMeasure: each`, `unitPrice` is the price per item (12 eggs at £3.30 → `0.28`).
  - Loose produce: `price.actual` is the price of one average item (`averageWeight` kg × `unitPrice`).
  - Listing prices and promotions can be fresher than product files fetched days earlier (67 shelf prices differed), so `build.py` takes price and promotions from the listing.
  - Cooked values: many labels give per 100g of the *cooked* product (`(microwaved) Per 100g`, or a footer `When cooked according to instructions.`), sometimes with a yield footer (`** When grilled according to instructions 400g typically weighs 354g.`). Rice, pasta and pulse labels often state a yield that doesn't match their own cooked values.
  - "Each" items' labels often give the serving weight: `One typical egg (61g)`, `Per Serving (2 Large Eggs)`, `1/16 of a cake (60g)`, `Each pack` (weight derivable from the kcal ratio), footers like `This pack contains 2 servings` / `Number of servings: 6`.
  - More nutrition formats: `2506/603 kJ/kcal`, `824 kJ (10%*)`, `Fat` = `(194kcal)` (energy spilling into the next row), `Protien` (sic), US-style labels (`Calories`, `Calories from Fat`, per 14g serving), water analyses in mg/l.
  - More Clubcard formats: `75p Clubcard Price`, `£1.50 per kg Clubcard Price` (loose), `Save 25% £1.50 per kg Clubcard Price`, `Any 3 for 2 Clubcard Price - Cheapest Product Free`, `CC DFNS 3 FOR 2 C/F`, and meal deals (`£N Meal Deal Snack Clubcard Price …`, `STIR FRY Meal Deal for £N …`), which `build.py` ignores. Non-Clubcard promotions (`Save £1`) are already in `price.actual`.
  - Some titles have mojibake (`CrÃ¨me`); `build.py` repairs it.
  - Image CDN: `defaultImageUrl` takes `?h=&w=` resize parameters. Akamai returns 403 to the `HeadlessChrome` user agent (not to normal browsers or curl), so thumbnails look broken in headless screenshots.
- **Product page URL:** `https://www.tesco.com/groceries/en-GB/products/{tpnc}`

## Conventions
- **Keep fetching and parsing separate.** Fetch raw JSON into `data/raw/{tpnc}.json` (resumable, skip cached), and do all parsing/cleaning in a separate build step that can be re-run without hitting the API.
- **Keep API traffic small.** Probe with a handful of requests. The user runs full-category scrapes themselves.
- **Never commit** `.env`, the API key or `data/raw/`.
