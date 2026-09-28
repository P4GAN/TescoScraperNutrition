# Tesco Protein Finder

Which Tesco foods give the most protein per £, without counting "cheap protein" like flour or potatoes?

This project scrapes price and nutrition-label data from Tesco UK's product API and publishes a static site that ranks foods by protein per £. You can filter by protein share of calories, salt, sugar and saturated fat, and switch between shelf and Clubcard prices.

Status: early. See [PLAN.md](PLAN.md) for the roadmap.

## Setup
```sh
pip install -r requirements.txt
cp .env.example .env   # then add the API key
```

## Usage (v0 scraper)
```sh
python scraper/scraping.py --category-label "Fresh Food" --count 100 --csv data/FreshFood.csv
```
Categories: `Fresh Food`, `Bakery`, `Frozen Food`, `Food Cupboard`, `Treats & Snacks`.
