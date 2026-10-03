# Plan

Goal: a GitHub Pages site that ranks Tesco food by protein per £ (shelf or Clubcard price). Its filters remove impractical sources (low protein share of calories, high salt/sugar/saturates) and flag bad data. The decisions and API facts are in [CLAUDE.md](CLAUDE.md).

## 1. Repo setup
- [x] Git repo, `.gitignore`, API key moved to `.env`, folders reorganised
- [x] Fix `build_category_facet` so `&` is not encoded (Treats & Snacks now works)
- [ ] User: push to GitHub; later enable Pages (`main` / `/docs`)

## 2. Scraper: split fetching from parsing
- [x] `scraper/fetch.py`
  - [x] For each category, page through `category.products`. Skip ads (`adId`) and advance the offset by the organic count. Collect `tpnc`, department/aisle/shelf, shelf price and promotions from the listing.
  - [x] Write a listing index file (e.g. `data/raw/_listing.json`) with each product's categories.
  - [x] Fetch `product(tpnc)` for each product with the full query (all nutrition rows, `packSize`, promotions, image) into `data/raw/{tpnc}.json`. Resumable: skip existing files unless `--refresh`.
  - [x] Run 3–4 worker threads with retries and exponential backoff. Log failures to a file for re-running.
    - Done as 3 workers sharing one rate limit (`--rate`, default 2 req/s). Tesco returns 429 at about 2 req/s, and a 429 pauses every worker. Failures go to `data/raw/_failures.json`; `--skip-listing` re-runs just the missing products.
  - [x] CLI: `--category` (repeatable, default all five), `--limit N` for test runs.
- [x] Test on ~200 Fresh Food products before the full run
  - 200/200 fetched, 0 failures, ~1.9 products/s. 196 have nutrition, 192 have `packSize` (140 with a weight; 52 are "each" items with no weight), 58 have a Clubcard price (17 of them multibuys), 6 are `isForSale: false`.
- [x] User: full scrape of Fresh Food, Bakery, Frozen Food, Food Cupboard, Treats & Snacks (~18k listed, fewer unique; about 2–2.5 h at the ~2 req/s rate limit). Run `python scraper/fetch.py`; if interrupted, run it again (cached products are skipped).
  - Done 2026-10-03 (product files fetched 28 Sep – 3 Oct, listing 3 Oct): 15,329 unique products, all cached, 0 failures. Per category: Fresh Food 4,800, Bakery 960, Frozen Food 1,143, Food Cupboard 8,700, Treats & Snacks 2,818. 2,910 products are in more than one category. Some categories list a few more products than Tesco's total because the listing shifts while it's paged. 71 cached files from earlier runs are no longer listed; `build.py` ignores them.

## 3. Build: clean, validate and derive (`scraper/build.py`)
Run `python scraper/build.py` (about 5 s, no API calls). `--check TPNC` prints one parsed record. Price and promotions come from `_listing.json`, which is one consistent snapshot and fresher than product files fetched days earlier. Nutrition and pack size come from the product files.
- [x] **Nutrition parser:** 12,903 of 15,329 products parse. The other 2,426 have no table (2,350) or no protein row (mostly drinks), and are left out of `data.json`.
  - [x] Choose the per-100g/100ml column from the "Typical Values" header: the first per-100 column with values that isn't a %RI column. Prefer as-sold over cooked/prepared columns. With no per-100 column, scale a "Per 30g" column (4 products).
  - [x] Handle both energy formats, the name variants, `<0.5g`/`trace` and footer rows. Also handled: `2506/603 kJ/kcal`, `824 kJ (10%*)`, energy spilling into the next row (`Fat` = `(194kcal)`), unitless energy (kJ or kcal chosen by fit to the macros), `Protien` (sic), US labels (`Calories`, but not `Calories from Fat`), sodium → salt × 2.5.
  - [x] Cooked values: 1,801 labels give values for the cooked product (header like `(microwaved) Per 100g`, or a footer like `When cooked according to instructions.`). 848 also state a yield (`400g typically weighs 354g`) and are converted back to as-sold weight. The other 953 get a "cooked values" badge. Rice, pasta and pulse labels often state a yield that doesn't match their own cooked values (basmati would come out at 482 kcal/100g dry), so a conversion that gives impossible values is skipped.
- [x] **Price/kg:** compute from `price.actual` ÷ `packSize`.
  - [x] `packSize` agrees with Tesco's `unitPrice` within 5% for 99.9% of products, DR.WT included (`packSize` is then the drained weight). Unit slips (`400 KG` for a 400 g meal, `240 KG` for a can, `1 G` for a 1 kg bag, `200 MG`) are detected against the title weight and shifted by 1000 (10 products).
  - [x] Fall back to `unitPrice`/`unitOfMeasure` (`100g` ×10, `10g` ×100, DR.WT as-is) for loose produce (101 products). Treat 1 L ≈ 1 kg.
  - [x] "Each" items: the weight comes from the title, or is estimated from the label (`One typical egg (61g)` × 12 eggs, `Each pack` + kcal ratio for sandwiches, `1/16 of a cake (60g)`, "This pack contains 4 servings"). That covers 609 products, which get a "~weight" badge. The per-item rule applies only when the header's noun matches the title (`roll` ↔ `4 White Rolls`), so "2 slices of mango" doesn't count as 2 mangoes. 269 products are still unranked (e.g. Weetabix, "per 2-biscuit serving").
- [x] **Clubcard price/kg:** per-item Clubcard price from `description` (`£2.50 Clubcard Price`, `75p …`, `£3.00 Save 25% …`), then price/kg scaled by Clubcard ÷ shelf price. `£1.50 per kg Clubcard Price` is used directly. Multibuys use the best per-item price (`Any 3 for £10`, `4 for £3 or 8 for £5`, `Any 3 for 2` = ⅔ of the shelf price) and are flagged. Meal deals and cross-product offers are ignored. 4,281 products have a usable Clubcard price, 1,503 of them multibuys.
- [x] **Sanity flags** (bit flags in `data.json`; the site hides the first three by default):
  - [x] protein > 100 g, or protein + carbs + fat + fibre > 105 g (20 products)
  - [x] 4P + 4C + 9F + 2·fibre (polyols at 2.4) more than 25% and 20 kcal away from the listed kcal (78)
  - [x] computed price/kg differs from Tesco's `unitPrice` by more than 20%, or is outside £0.15–£20,000/kg (1)
  - [x] missing nutrition: these products are left out of `data.json` (count in `excluded`)
  - Informational flags: estimated weight, cooked values, yield-adjusted, scaled, not for sale (396), liquid, multibuy.
- [x] Dedupe by `tpnc`, keeping the list of categories (bitmask in `data.json`).
- [x] **Output:** `docs/data.json`, rows of arrays plus lookup tables (departments, aisles, promo texts) and `generated`/`scraped` dates. 3.0 MB, 1.3 MB gzipped (GitHub Pages serves it gzipped). Image URLs are stored without the common prefix.

## 4. Metrics (computed in build or in the browser)
All computed in the browser (`docs/app.js`), so they follow the price toggle and the filters.
- [x] g protein per £ (main sort) and £ per 100 g protein, each for both shelf and Clubcard prices
- [x] Protein share of calories = 4 × protein ÷ kcal. This is the main filter for impractical foods, default minimum 25%. Rough values: flour ~12%, potatoes ~11%, bread ~13%, peanuts ~18%, milk ~29%, lentils ~30%, eggs ~38%, chicken breast ~90%.
- [x] Fibre, sugars, saturates and salt per 100 g, for filters
- [x] Best-value set: products that no other product beats on both protein per £ and protein share of calories. It's recomputed for the current filters (12 products with the defaults).
- [ ] Optional: an edible-yield adjustment for bone-in meat (drumsticks, wings, whole chicken), which currently rank high partly because of the bone weight

## 5. Website (`docs/index.html` + `app.js` + `style.css` + `data.json`)
Test locally with `python -m http.server -d docs` and open http://localhost:8000. Tested in Chrome at desktop and phone widths, light and dark: no console errors, no horizontal page scroll.
- [x] **Table:** sortable columns with a thumbnail, name linking to the Tesco page, price, metrics and a warning badge for flagged data
  - Badges: "check data" (suspect, with the reason on hover), "~weight", "cooked values", "per serving", "multibuy", "unavailable". Best-value rows have a blue edge. On phones the product column is sticky and the thumbnail is hidden.
- [x] **Controls:**
  - [x] shelf/Clubcard toggle (plus "Count multibuy deals")
  - [x] category, department and aisle filters (cascading, with counts)
  - [x] text search (all words, matched against title, aisle and department)
  - [x] slider for minimum protein share of calories
  - [x] maximum salt, saturates and sugar
  - [x] hide flagged products, hide unweighed "each" products (also: hide estimated weights, hide cooked-value labels, hide unavailable, best value only, reset)
  - Filters, sort and price mode are kept in the URL query string, so a view can be shared.
- [x] **Chart:** protein share of calories (x) against protein per £ (y), with the best-value set highlighted and the product shown on hover (canvas-based library from a CDN)
  - Chart.js 4.4.1 from cdnjs. Other products are muted gray, the frontier is blue with a line, and hovering a table row marks that product in orange. Clicking a point opens the Tesco page.
- [x] **Performance and footer:** filter and sort in memory, render the first ~200 rows with "show more", and show "Data as of {date}"
- Tesco's image CDN (Akamai) returns 403 to the `HeadlessChrome` user agent, so thumbnails look broken in headless screenshots. Real browsers get them, including from a github.io page.

## 6. Later
- [ ] Remove the legacy `scraper/scraping.py` and `data/FreshFood.csv` (and update `notebooks/explore.ipynb`), now that `fetch.py` + `build.py` replace them
- [ ] Rank more "each" items: Weetabix-style packs (count × per-biscuit weight), loaves with a "per slice" column
- [ ] Better handling of cooked-only labels for rice, pasta and pulses (953 products still show cooked values)
- [ ] Dated snapshots for price history
- [ ] Micronutrients from UK CoFID for the best-value products
- [ ] "Cheapest basket" optimiser: the lowest-cost list that hits X g protein under Y kcal (linear programming in the browser)

## Notes
- Tesco's terms very likely forbid automated scraping. Republishing price data in a public repo is the most exposed part, so keep the request rate low and don't commit raw dumps. This is the user's call.
