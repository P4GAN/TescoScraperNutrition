# Plan

Goal: a GitHub Pages site that ranks Tesco food by protein per £ (shelf or Clubcard price). Its filters remove impractical sources (low protein share of calories, high salt/sugar/saturates) and flag bad data. The decisions and API facts are in [CLAUDE.md](CLAUDE.md).

## 1. Repo setup
- [x] Git repo, `.gitignore`, API key moved to `.env`, folders reorganised
- [x] Fix `build_category_facet` so `&` is not encoded (Treats & Snacks now works)
- [ ] User: push to GitHub; later enable Pages (`main` / `/docs`)

## 2. Scraper: split fetching from parsing
- [ ] `scraper/fetch.py`
  - [ ] For each category, page through `category.products`. Skip ads (`adId`) and advance the offset by the organic count. Collect `tpnc`, department/aisle/shelf, shelf price and promotions from the listing.
  - [ ] Write a listing index file (e.g. `data/raw/_listing.json`) with each product's categories.
  - [ ] Fetch `product(tpnc)` for each product with the full query (all nutrition rows, `packSize`, promotions, image) into `data/raw/{tpnc}.json`. Resumable: skip existing files unless `--refresh`.
  - [ ] Run 3–4 worker threads with retries and exponential backoff. Log failures to a file for re-running.
  - [ ] CLI: `--category` (repeatable, default all five), `--limit N` for test runs.
- [ ] Test on ~200 Fresh Food products before the full run
- [ ] User: full scrape of Fresh Food, Bakery, Frozen Food, Food Cupboard, Treats & Snacks (~18k listed, fewer unique; roughly 30–60 min)

## 3. Build: clean, validate and derive (`scraper/build.py`)
- [ ] **Nutrition parser:**
  - [ ] Choose the per-100g/100ml column from the "Typical Values" header.
  - [ ] Handle both energy formats, the name variants, `<0.5g`/`trace` and footer rows.
- [ ] **Price/kg:** compute from `price.actual` ÷ `packSize`.
  - [ ] Fall back to `unitPrice`/`unitOfMeasure` (`100g` ×10, DR.WT as-is).
  - [ ] "Each" items without a usable weight are marked unranked. Treat 1 L ≈ 1 kg.
- [ ] **Clubcard price/kg:** parse `unitSellingInfo` when present, otherwise the single price in `description`. For multibuys ("4 for £3") use the per-item price.
- [ ] **Sanity flags:**
  - [ ] protein > 100 g
  - [ ] 4P + 4C + 9F more than ~25% away from the listed kcal
  - [ ] computed price/kg differs from Tesco's `unitPrice` by more than ~20%
  - [ ] missing nutrition
- [ ] Dedupe by `tpnc`, keeping the list of categories.
- [ ] **Output:** `docs/data.json` in a compact format (column arrays or arrays of rows, not verbose objects), with a `generated` date.

## 4. Metrics (computed in build or in the browser)
- [ ] g protein per £ (main sort) and £ per 100 g protein, each for both shelf and Clubcard prices
- [ ] Protein share of calories = 4 × protein ÷ kcal. This is the main filter for impractical foods, default minimum ~25%. Rough values: flour ~12%, potatoes ~11%, bread ~13%, peanuts ~18%, milk ~29%, lentils ~30%, eggs ~38%, chicken breast ~90%.
- [ ] Fibre, sugars, saturates and salt per 100 g, for filters
- [ ] Best-value set: products that no other product beats on both protein per £ and protein share of calories
- [ ] Optional: an edible-yield adjustment for bone-in meat (drumsticks, wings, whole chicken), which currently rank high partly because of the bone weight

## 5. Website (`docs/index.html` + `app.js` + `data.json`)
- [ ] **Table:** sortable columns with a thumbnail, name linking to the Tesco page, price, metrics and a warning badge for flagged data
- [ ] **Controls:**
  - [ ] shelf/Clubcard toggle
  - [ ] category, department and aisle filters
  - [ ] text search
  - [ ] slider for minimum protein share of calories
  - [ ] maximum salt, saturates and sugar
  - [ ] hide flagged products, hide unweighed "each" products
- [ ] **Chart:** protein share of calories (x) against protein per £ (y), with the best-value set highlighted and the product shown on hover (canvas-based library from a CDN)
- [ ] **Performance and footer:** filter and sort in memory, render the first ~200 rows with "show more", and show "Data as of {date}"
- [ ] Can be prototyped now against `data/FreshFood.csv` (converted) before the new scrape exists

## 6. Later
- [ ] Dated snapshots for price history
- [ ] Micronutrients from UK CoFID for the best-value products
- [ ] "Cheapest basket" optimiser: the lowest-cost list that hits X g protein under Y kcal (linear programming in the browser)

## Notes
- Tesco's terms very likely forbid automated scraping. Republishing price data in a public repo is the most exposed part, so keep the request rate low and don't commit raw dumps. This is the user's call.
