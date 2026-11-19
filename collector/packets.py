#!/usr/bin/env python3
"""Reads the documents attached to agenda items (step 5 of the build plan).

Run after collect.py. For every item it downloads, caches and reads:
  * the staff report PDF      -> the city's own summary and "Fiscal Impact" text, stored
                                 word for word (figures there are not added into totals,
                                 because they mix costs, revenues and property values)
  * monthly "Bills and Estimates" detail PDF  -> every payment, by department and vendor
  * monthly "Estimates" PDF   -> progress payments on construction and design contracts

Needs the `pdftotext` program (package poppler-utils). Every parsed total is checked
against the total printed in the document, and the result is stored in packet_checks.
"""
import datetime as dt, hashlib, os, re, sqlite3, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collect import DB, CACHE, WORKERS  # noqa: E402

FILES = os.path.join(CACHE, "files")
ERRORS = []
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}

SCHEMA = """
DROP TABLE IF EXISTS payments; DROP TABLE IF EXISTS pcard; DROP TABLE IF EXISTS estimates;
DROP TABLE IF EXISTS packet_checks;
CREATE TABLE payments (item_id INTEGER, bills_month TEXT, department TEXT, vendor TEXT,
  amount REAL, source_url TEXT);
CREATE TABLE pcard (item_id INTEGER, bills_month TEXT, org TEXT, merchant TEXT,
  amount REAL, source_url TEXT);
CREATE TABLE estimates (item_id INTEGER, bills_month TEXT, contractor TEXT, contract_name TEXT,
  job TEXT, estimate REAL, contract_amount REAL, contingency REAL, source_url TEXT);
CREATE TABLE packet_checks (item_id INTEGER, kind TEXT, stated_total REAL, parsed_total REAL,
  ok INTEGER, source_url TEXT);
CREATE INDEX idx_pay_vendor ON payments(vendor);
"""


def pdf_text(url):
    """Download a PDF (cached on disk) and return its text with layout kept."""
    h = hashlib.sha1(url.encode()).hexdigest()
    pdf, txt = os.path.join(FILES, h + ".pdf"), os.path.join(FILES, h + ".txt")
    os.makedirs(FILES, exist_ok=True)
    if not os.path.exists(txt):
        if not os.path.exists(pdf):
            for attempt in range(4):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "council-transparency-collector"})
                    with urllib.request.urlopen(req, timeout=120) as r, open(pdf + ".tmp", "wb") as f:
                        f.write(r.read())
                    os.replace(pdf + ".tmp", pdf)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e
                    time.sleep(2 ** attempt)
            else:
                ERRORS.append((url, repr(last)))
                return ""
        r = subprocess.run(["pdftotext", "-layout", pdf, txt], capture_output=True)
        if r.returncode:
            ERRORS.append((url, "pdftotext: " + r.stderr.decode(errors="replace")[:200]))
            return ""
    with open(txt, encoding="utf-8", errors="replace") as f:
        return f.read()


def num(s):
    """'1,234.56', '-651.56', '651.56-', '(651.56)' or '-' -> float."""
    s = s.strip().replace("$", "").replace(",", "")
    if s in ("-", ""):
        return 0.0
    neg = s.startswith("-") or s.endswith("-") or s.startswith("(")
    return (-1 if neg else 1) * float(s.strip("-()"))


AMT = r"\(?-?\$?[\d,]*\.\d\d\)?-?"

# ------------------------------------------------------------ staff reports
HEADINGS = ["Staff/Department Responsible for Legislation", "Requestor of Legislation",
            "Location, if applicable", "Summary/Purpose", "Background", "Fiscal Impact",
            "Staff Recommendation"]


def parse_staff_report(text):
    """Return {heading: text} for the city's standard staff report form."""
    if "STAFF REPORT" not in text.upper():
        return {}
    pos = []
    for h in HEADINGS:
        m = re.search(r"^\s*" + re.escape(h) + r"\s*$", text, re.M)
        if m:
            pos.append((m.start(), m.end(), h))
    pos.sort()
    out = {}
    for i, (_, end, h) in enumerate(pos):
        body = text[end:pos[i + 1][0] if i + 1 < len(pos) else len(text)]
        body = re.sub(r"\s+", " ", body.replace("\x0c", " ")).strip(" .")
        out[h] = body
    return out


# ------------------------------------------------------------ bills detail
SKIP = re.compile(r"^(CITY OF LA CROSSE$|WARRANT |ORG$|Report generated|User:|Program ID|\*\* END)")


def parse_warrants(text):
    """Department -> vendor -> amount lines of the warrant publication list.

    Returns (rows, stated_total). rows = [(department, vendor, amount)].
    """
    start = text.find("WARRANT PUBLICATION LIST")
    if start < 0:
        return [], None
    rows, dept, total, in_pcard = [], None, None, False
    for line in text[start:].replace("\x0c", "\n").split("\n"):
        s = line.strip()
        # Some months print the purchasing-card detail in the middle of the warrant list.
        if s.startswith("PCARD DETAIL"):
            in_pcard = True
        elif s.startswith("WARRANT PUBLICATION LIST") or s == "CITY OF LA CROSSE":
            in_pcard = False
        if in_pcard:
            continue
        if not s or SKIP.match(s) or s.startswith("VENDOR NAME"):
            continue
        m = re.match(r"^TOTAL\s+(" + AMT + r")$", s)
        if m:
            total = num(m.group(1))
            break
        if re.fullmatch(AMT, s):          # department subtotal
            continue
        m = re.match(r"^(.*?\S)\s{2,}(" + AMT + r")$", s)
        if m and line.startswith(" ") and dept:
            rows.append((dept, m.group(1).strip(), num(m.group(2))))
        elif not line.startswith(" "):
            dept = s
    return rows, total


def parse_pcard(text):
    start = text.find("PCARD DETAIL")
    if start < 0:
        return [], None
    rows, total = [], None
    for line in text[start:].split("\n"):
        m = re.match(r"^\s*(\d+)\s+[\d,]+\s+(.*?\S)\s{2,}(" + AMT + r")\s*$", line)
        if m:
            rows.append((m.group(1), m.group(2), num(m.group(3))))
            continue
        m = re.match(r"^\s*TOTAL\s+(" + AMT + r")\s*$", line)
        if m and rows and total is None:
            total = num(m.group(1))       # the first TOTAL after the card lines is the card total
    return rows, total


def parse_estimates(text):
    """Rows of the monthly estimates sheet: contractor, contract, job, three amounts."""
    rows, total = [], None
    a = r"(" + AMT + r"|-)"
    for line in text.split("\n"):
        m = re.match(r"^\s*\d+\s+(.*?\S)\s+(\S+)\s+" + a + r"\s+" + a + r"\s+" + a + r"\s*$", line)
        if m:
            left = re.split(r"\s{2,}", m.group(1), maxsplit=1)
            rows.append((left[0], left[1] if len(left) > 1 else "", m.group(2),
                         num(m.group(3)), num(m.group(4)), num(m.group(5))))
            continue
        m = re.match(r"^\s*" + a + r"\s+" + a + r"\s+" + a + r"\s*$", line)
        if m and rows:
            total = num(m.group(1))
    return rows, total


def bills_month(name, year):
    m = re.match(r"\s*(\w+)\s+(\d{4})\s+Bills", name or "", re.I)
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1).lower()]:02d}"
    return None


def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(items)")}
    for c in ("staff_summary", "fiscal_impact", "staff_report_url"):
        if c not in cols:
            db.execute(f"ALTER TABLE items ADD COLUMN {c} TEXT")
    docs = db.execute("""SELECT d.item_id, d.name, d.url, i.name AS item_name, i.amount, i.title
        FROM documents d JOIN items i ON i.id=d.item_id
        WHERE d.kind='attachment' AND lower(d.url) LIKE '%.pdf'""").fetchall()
    year = db.execute("SELECT value FROM meta WHERE key='year'").fetchone()[0]

    staff = [d for d in docs if "staff report" in d["name"].lower() and "cpc" not in d["name"].lower()]
    bills = [d for d in docs if bills_month(d["item_name"], year)
             and re.search(r"\b(detail|estimates)\b", d["name"], re.I)]
    wanted = staff + bills
    with ThreadPoolExecutor(WORKERS) as ex:
        texts = dict(zip([d["url"] for d in wanted], ex.map(pdf_text, [d["url"] for d in wanted])))

    n_staff = 0
    for d in staff:
        sec = parse_staff_report(texts[d["url"]])
        if not sec:
            continue
        n_staff += 1
        fiscal = sec.get("Fiscal Impact") or None
        db.execute("UPDATE items SET staff_summary=?, fiscal_impact=?, staff_report_url=? WHERE id=?",
                   (sec.get("Summary/Purpose") or None, fiscal, d["url"], d["item_id"]))

    for d in bills:
        month, text, kind = bills_month(d["item_name"], year), texts[d["url"]], d["name"].lower()
        if "detail" in kind:
            rows, total = parse_warrants(text)
            db.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                           [(d["item_id"], month, a, b, c, d["url"]) for a, b, c in rows])
            parsed = round(sum(r[2] for r in rows), 2)
            db.execute("INSERT INTO packet_checks VALUES (?,?,?,?,?,?)", (
                d["item_id"], "payments", total, parsed, int(total is not None and abs(total - parsed) < 0.01), d["url"]))
            rows, total = parse_pcard(text)
            if rows:
                db.executemany("INSERT INTO pcard VALUES (?,?,?,?,?,?)",
                               [(d["item_id"], month, a, b, c, d["url"]) for a, b, c in rows])
                parsed = round(sum(r[2] for r in rows), 2)
                db.execute("INSERT INTO packet_checks VALUES (?,?,?,?,?,?)", (
                    d["item_id"], "pcard", total, parsed, int(total is not None and abs(total - parsed) < 0.01), d["url"]))
        else:
            rows, total = parse_estimates(text)
            db.executemany("INSERT INTO estimates VALUES (?,?,?,?,?,?,?,?,?)",
                           [(d["item_id"], month) + r + (d["url"],) for r in rows])
            parsed = round(sum(r[3] for r in rows), 2)
            db.execute("INSERT INTO packet_checks VALUES (?,?,?,?,?,?)", (
                d["item_id"], "estimates", total, parsed, int(total is not None and abs(total - parsed) < 0.01), d["url"]))

    db.execute("INSERT OR REPLACE INTO meta VALUES ('packet_errors', ?)", (str(len(ERRORS)),))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('packets_read_at', ?)",
               (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
    db.commit()
    print(f"{n_staff} staff reports read of {len(staff)}")
    for r in db.execute("""SELECT i.name, c.kind, c.stated_total, c.parsed_total, c.ok FROM packet_checks c
            JOIN items i ON i.id=c.item_id ORDER BY i.file_number, c.kind"""):
        print(f"  {'OK ' if r['ok'] else 'MISMATCH'} {r['name'][:34]:34} {r['kind']:9} stated {r['stated_total']} parsed {r['parsed_total']}")
    for u, e in ERRORS:
        print("  ERROR", u, e)
    sys.exit(1 if ERRORS else 0)


if __name__ == "__main__":
    main()
