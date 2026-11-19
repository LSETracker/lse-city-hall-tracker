"""A small made-up council database for testing the review flags and the site build.

Nothing here is real. Names, vendors and amounts are invented so that each flag rule has
one case that must be flagged and at least one near-miss that must not be.
"""
import json, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))
import collect, packets  # noqa: E402

U = "https://example.org/"


def make_db(path):
    if os.path.exists(path):
        os.remove(path)
    db = sqlite3.connect(path)
    db.executescript(collect.SCHEMA)
    db.executescript(packets.SCHEMA)

    for k, v in (("year", "2026"), ("last_updated", "2026-11-14T00:00:00Z"), ("source", "https://example.org"),
                 ("bodies", json.dumps(["Common Council", "Finance & Personnel Committee"])), ("api", "x"), ("fetch_errors", "0")):
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))

    for pid, name, role, dist in ((1, "Pat Mayor", "Mayor", None), (2, "Alex Alder", "Council Member", "1"),
                                  (3, "Blair Brown", "Council Member", "2"), (4, "Casey Cole", "Council Member", "3")):
        db.execute("INSERT INTO people VALUES (?,?,?,?,?,?,?)", (pid, name, role, dist, "2025-04-15", None, None))

    db.execute("INSERT INTO meetings VALUES (100,138,'Common Council','2026-03-12','6:00 PM','Hall',NULL,'Final','Final',?,?,?,0)",
               (U + "a100.pdf", U + "m100.pdf", U + "meeting100"))
    for pid, name in ((2, "Alex Alder"), (3, "Blair Brown"), (4, "Casey Cole")):
        db.execute("INSERT INTO attendance VALUES (100,?,?,'Present')", (pid, name))

    def item(i, fn, name, title, typ, cat, amt, amends=0, staff=None, fiscal=None, report=None):
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,?)", (
            i, fn, name, title, typ, "Adopted", cat, amt, "from title" if amt else None,
            json.dumps([amt] if amt else []), amends, U + "file/" + fn, staff, fiscal, report))

    item(1, "26-0001", "Paving contract", "Resolution awarding contract to Acme Paving for street resurfacing in the amount of $1,200,000",
         "Resolution", "Contracts and purchases", 1200000.0)
    item(2, "26-0002", "Radio purchase", "Resolution approving the purchase of radios from Motorola as a sole source vendor not to exceed $85,000",
         "Resolution", "Contracts and purchases", 85000.0,
         staff="Staff recommends a sole-source purchase because the radios must match the existing system.",
         fiscal="$85,000 from the fleet fund", report=U + "sr2.pdf")
    item(3, "26-0003", "Water bonds", "Resolution authorizing issuance of General Obligation Bonds not to exceed $5,000,000",
         "Resolution", "Borrowing", 5000000.0, staff="Borrow for the water main project.", fiscal="", report=U + "sr3.pdf")
    item(4, "26-0004", "Paving amendment", "Resolution amending Resolution 26-0001 to increase the contract to $2,000,000",
         "Resolution", "Contracts and purchases", 2000000.0, amends=1)
    item(5, "26-0005", "Small contract", "Resolution approving a contract for $50,000", "Resolution", "Contracts and purchases", 50000.0)
    item(50, "26-0950", "March 2026 Bills and Estimates", "Bills and estimates for March", "Resolution", "Budget", None)
    item(51, "26-0951", "April 2026 Bills and Estimates", "Bills and estimates for April", "Resolution", "Budget", None)

    for i, p, n in ((1, 2, "Alex Alder"), (2, 3, "Blair Brown"), (3, 1, "Pat Mayor")):
        db.execute("INSERT INTO sponsors VALUES (?,?,?)", (i, p, n))

    def action(hid, item_id, mover, seconder, roll=0, yes=None, no=None, ab=None, consent=0):
        db.execute("INSERT INTO actions VALUES (?,?,100,138,'Common Council','2026-03-12',?,'ADOPTED','Pass',NULL,NULL,?,?,?,?,?,?,?,?,?,0,?)", (
            hid, item_id, hid, mover[0] if mover else None, mover[1] if mover else None,
            seconder[0] if seconder else None, seconder[1] if seconder else None, consent, roll, yes, no, ab, U + f"hist/{hid}"))

    alder, brown, cole = (2, "Alex Alder"), (3, "Blair Brown"), (4, "Casey Cole")
    action(1001, 1, alder, brown, consent=1)
    action(1002, 2, alder, cole)
    action(1003, 3, brown, alder, roll=1, yes=2, no=0, ab=1)
    action(1004, 4, alder, brown)
    action(1005, 5, brown, cole)
    action(1050, 50, alder, brown)
    action(1051, 51, alder, brown)
    for pid, name, vote in ((2, "Alex Alder", "Yes"), (3, "Blair Brown", "Yes"), (4, "Casey Cole", "Abstain")):
        db.execute("INSERT INTO votes VALUES (1003,?,?,?)", (pid, name, vote))

    # Contracts: PARK-1 is over its limit, OK-3 is fine, ZERO-2 shows $0 on the sheet.
    for item_id, ym, rows in (
            (50, "2026-03", [("T2 Contracting", "Hickey Park Restrooms", "PARK-1", 300000.0, 500000.0, 50000.0),
                             ("WSP USA", "Green Bay St", "OK-3", 10000.0, 95800.0, 0.0),
                             ("Mystery Co", "", "ZERO-2", 40129.32, 0.0, 0.0)]),
            (51, "2026-04", [("T2 Contracting (FINAL)", "Hickey Park Restrooms", "PARK-1", 280000.0, 500000.0, 50000.0)])):
        for c, n, j, est, amt, cont in rows:
            db.execute("INSERT INTO estimates VALUES (?,?,?,?,?,?,?,?,?)", (item_id, ym, c, n, j, est, amt, cont, U + f"est{ym}.pdf"))

    def pay(item_id, ym, dept, vendor, amount):
        db.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)", (item_id, ym, dept, vendor, amount, U + f"bills{ym}.pdf"))

    for _ in range(2):
        pay(50, "2026-03", "PARKS", "ACME SUPPLY", 4000.0)      # identical pair: flagged
        pay(50, "2026-03", "PARKS", "BETA CO", 3000.0)          # identical pair but credited back: not flagged
        pay(50, "2026-03", "PARKS", "GAMMA", 100.0)             # identical pair but tiny: not flagged
    pay(50, "2026-03", "PARKS", "BETA CO", -3000.0)
    pay(50, "2026-03", "GEN ADMIN", "CITY OF LA CROSSE TREASURER", 900000.0)       # pass-through payee
    pay(50, "2026-03", "REAL ESTATE AND PERSONAL PROPERTY TAX", "TAX COLLECTOR PAYEE", 700000.0)   # pass-through account
    pay(50, "2026-03", "STREETS", "DELTA PAVING LLC", 500000.0)
    pay(51, "2026-04", "STREETS", "DELTA PAVING", 100000.0)                         # spelling variant of the line above
    pay(51, "2026-04", "PARKS", "ACME SUPPLY INC", 10000.0)
    pay(51, "2026-04", "PARKS", "SMALL VENDOR", 1500.0)

    def card(item_id, ym, merchant, amount):
        db.execute("INSERT INTO pcard VALUES (?,?,'1234',?,?,?)", (item_id, ym, merchant, amount, U + f"bills{ym}.pdf"))

    card(50, "2026-03", "HOME DEPOT", 6200.0)      # large single charge
    card(50, "2026-03", "AMAZON", 300.0)
    card(50, "2026-03", "DELL", 3000.0)
    card(50, "2026-03", "DELL", 3000.0)            # identical card charges, under the large-charge line

    db.execute("INSERT INTO packet_checks VALUES (50,'payments',1.0,1.0,1,?)", (U + "bills2026-03.pdf",))
    db.execute("INSERT INTO packet_checks VALUES (51,'payments',100.0,90.0,0,?)", (U + "bills2026-04.pdf",))
    db.commit()
    db.close()
    return path
