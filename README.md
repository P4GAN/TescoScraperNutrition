# Tesco Protein Finder

Which Tesco foods give the most protein per £, without counting "cheap protein" like flour or potatoes?

This project scrapes price and nutrition-label data from Tesco UK's product API and publishes a static site that ranks foods by protein per £. You can filter by protein share of calories, salt, sugar and saturated fat, and switch between shelf and Clubcard prices.

Status: data pipeline and site work end to end; see [PLAN.md](PLAN.md) for what's left.

## Setup
```sh
pip install -r requirements.txt
cp .env.example .env   # then add the API key
```

## Usage
```sh
python scraper/fetch.py                     # fetch all five categories into data/raw/ (~2.5 h; re-run to resume)
python scraper/build.py                     # parse data/raw/ into docs/data.json (a few seconds, no API calls)
python -m http.server -d docs               # preview the site at http://localhost:8000
```
`fetch.py` options: `--category "Fresh Food"` (repeatable), `--limit N` for a test run, `--skip-listing` to retry missing products, `--refresh` to re-fetch cached ones.
Categories: `Fresh Food`, `Bakery`, `Frozen Food`, `Food Cupboard`, `Treats & Snacks`.

The site in `docs/` is served by GitHub Pages (deploy from branch `main`, folder `/docs`). To refresh the data, re-run both scripts and commit `docs/data.json`.
