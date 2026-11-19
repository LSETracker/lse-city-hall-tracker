#!/bin/sh
# Nightly job: collect two years, read packets and budgets, test, rebuild the site.
# Needs pdftotext (poppler-utils). Exits non-zero if anything fails.
set -e
cd "$(dirname "$0")"
YEAR="${YEAR:-$(date +%Y)}"
PREV=$((YEAR - 1))
PREVDB="data/council-$PREV.sqlite"

# Collect the previous year first, so the current year's pages can compare against it.
COUNCIL_DB="$PREVDB" python3 collector/collect.py --year "$PREV" --refresh-days 0
COUNCIL_DB="$PREVDB" python3 collector/packets.py
COUNCIL_DB="$PREVDB" python3 collector/budget.py

python3 collector/collect.py --year "$YEAR" --refresh-days 45
python3 collector/packets.py
python3 collector/budget.py
python3 -m unittest discover tests

# Current year at site/, previous year as an archive at site/<year>/
PREV_DB="$PREVDB" SITE_YEAR_LINKS="$PREV=$PREV/index.html" python3 site_builder/build.py
COUNCIL_DB="$PREVDB" SITE_OUT="site/$PREV" SITE_YEAR_LINKS="$YEAR=../index.html" python3 site_builder/build.py
