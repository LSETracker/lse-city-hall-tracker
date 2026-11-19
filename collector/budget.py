#!/usr/bin/env python3
"""Reads the operating budget summary tables from the city's budget document.

Run after collect.py. Looks at PDF attachments on items of type "Budget", finds the
"PROPOSED OPERATING BUDGET" and "PROPOSED REVENUE SOURCES" summary pages, and stores
each row. Every column is added up and compared with the total printed in the document.
"""
import os, re, sqlite3, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import DB  # noqa: E402
from packets import pdf_text, ERRORS  # noqa: E402

SCHEMA = """
DROP TABLE IF EXISTS budget_lines; DROP TABLE IF EXISTS budget_docs;
CREATE TABLE budget_docs (item_id INTEGER, budget_year INTEGER, kind TEXT, columns TEXT,
  source_url TEXT, doc_name TEXT, ok INTEGER);
CREATE TABLE budget_lines (budget_year INTEGER, kind TEXT, seq INTEGER, label TEXT, is_total INTEGER,
  v1 REAL, v2 REAL, v3 REAL, v4 REAL, v5 REAL, change REAL);
"""
NUM = r"(?:\(?-?[\d,]*\d\)?|-)"
ROW = re.compile(r"^\s*(.*?\S)\s+((?:\$?\s*" + NUM + r"\s+){5}\$?\s*" + NUM + r")\s*$")
PAGES = (("expense", "PROPOSED OPERATING BUDGET FOR", "Total Operating Expenses"),
         ("revenue", "PROPOSED REVENUE SOURCES", "Non-Levy Revenues"))


def num(s):
    s = s.replace("$", "").replace(",", "").strip()
    if s == "-":
        return 0.0
    return -float(s.strip("()")) if s.startswith("(") else float(s)


def parse_page(page, subtotal_label):
    """Return (year, column headings, rows, ok). rows = [(label, is_total, six numbers)]."""
    y = re.search(r"FOR THE YEAR (\d{4})", page)
    if not y:
        return None
    year = int(y.group(1))
    rows, running, ok = [], [0.0] * 6, False
    for line in page.split("\n"):
        m = ROW.match(line)
        if not m:
            continue
        label = m.group(1).replace("$", "").strip()
        vals = [num(x) for x in re.findall(NUM, m.group(2).replace("$", " "))]
        if len(vals) != 6 or re.fullmatch(r"[\d ,]*", label):
            continue
        is_total = bool(re.match(r"total|non-levy|operating budget tax levy|debt service", label, re.I))
        if label.lower().startswith(subtotal_label.lower()):
            ok = all(abs(a - b) < 1.5 for a, b in zip(running[:5], vals[:5]))
        elif not is_total:
            running = [a + b for a, b in zip(running, vals)]
        rows.append((label, int(is_total), vals))
    cols = [f"{year - 4} actual", f"{year - 3} actual", f"{year - 2} actual", f"{year - 1} original budget", f"{year} proposed"]
    return year, cols, rows, ok


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    docs = db.execute("""SELECT d.item_id, d.name, d.url FROM documents d JOIN items i ON i.id=d.item_id
        WHERE i.type='Budget' AND lower(d.url) LIKE '%.pdf' AND lower(d.name) LIKE '%budget%'
        AND lower(d.name) NOT LIKE '%packet%' AND lower(d.name) NOT LIKE '%meetings%' ORDER BY d.id""").fetchall()
    found = {}
    for d in docs:
        text = pdf_text(d["url"])
        for kind, marker, sub in PAGES:
            for page in text.split("\x0c"):
                if marker in page:
                    r = parse_page(page, sub)
                    if r and r[2]:
                        found[(r[0], kind)] = (d, r)      # a later document replaces an earlier one
                    break
    for (year, kind), (d, (_, cols, rows, ok)) in found.items():
        db.execute("INSERT INTO budget_docs VALUES (?,?,?,?,?,?,?)",
                   (d["item_id"], year, kind, "|".join(cols), d["url"], d["name"], int(ok)))
        for i, (label, tot, v) in enumerate(rows):
            db.execute("INSERT INTO budget_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)", (year, kind, i, label, tot, *v))
        print(f"  {'OK ' if ok else 'MISMATCH'} {year} {kind}: {len(rows)} rows from {d['name']}")
    if not found:
        print("  no budget summary tables found")
    db.commit()
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
