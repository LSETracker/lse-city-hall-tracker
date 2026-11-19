#!/usr/bin/env python3
"""Collector for the La Crosse Council Transparency App.

Pulls public records from the Legistar Web API (client "cityoflacrosse"),
caches every raw JSON response on disk, and writes a normalized SQLite
database. Standard library only.

Usage:
    python3 collector/collect.py --year 2026
    python3 collector/collect.py --year 2026 --refresh-days 45   # nightly
"""
import argparse, datetime as dt, hashlib, json, os, re, sqlite3, sys, time
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://webapi.legistar.com/v1/cityoflacrosse"
SITE = "https://cityoflacrosse.legistar.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache")
DB = os.environ.get("COUNCIL_DB") or os.path.join(ROOT, "data", "council.sqlite")   # COUNCIL_DB: another year's file
COUNCIL = 138
DEFAULT_BODIES = [138, 139]          # Common Council, Finance & Personnel
WORKERS = 4                          # throttle: at most 4 requests at once
PAGE = 1000                          # API page size limit

STATS = {"fetched": 0, "cached": 0, "errors": []}


def _cache_path(url):
    return os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest()[:2],
                        hashlib.sha1(url.encode()).hexdigest() + ".json")


def fetch(path, refresh=False, **params):
    """GET one API URL, using the on-disk cache unless refresh is set."""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    p = _cache_path(url)
    if not refresh and os.path.exists(p):
        STATS["cached"] += 1
        with open(p, encoding="utf-8") as f:
            return json.load(f)["data"]
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json",
                                                       "User-Agent": "council-transparency-collector"})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"url": url, "fetched_at": now(), "data": data}, f)
            os.replace(p + ".tmp", p)
            STATS["fetched"] += 1
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as e:
            last = e
            time.sleep(2 ** attempt)
    STATS["errors"].append((url, repr(last)))
    return None


def fetch_all(path, refresh=False, **params):
    """Fetch every page of a list endpoint."""
    out, skip = [], 0
    while True:
        page = fetch(path, refresh, **dict(params, **{"$top": PAGE, "$skip": skip}))
        if page is None:
            return out
        out += page
        if len(page) < PAGE:
            return out
        skip += PAGE


def now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pmap(fn, items):
    with ThreadPoolExecutor(WORKERS) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------- amounts
MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?(?:\s*(million|billion))?", re.I)


def find_amounts(text):
    """Return every dollar figure in text, as floats, in order of appearance."""
    out = []
    for m in MONEY.finditer(text or ""):
        v = float(m.group(1).replace(",", "") + (m.group(2) or ""))
        if m.group(3):
            v *= 1e6 if m.group(3).lower() == "million" else 1e9
        out.append(v)
    return out


CATEGORIES = [  # first match wins; order matters
    ("TIF districts", r"tax increment|\bTIF\b|\bTID\b"),
    ("Borrowing", r"\bbonds?\b|promissory note|borrow|general obligation|\bnotes?\b.*\bissu|State Trust Fund Loan"),
    ("Grants", r"\bgrants?\b|CDBG|ARPA"),
    ("Budget", r"\bbudget\b|capital improvement|tax levy|bills and estimates|appropriat"),
    ("Contracts and purchases", r"contract|agreement|\bbids?\b|purchase|procure|award|lease|engineering services|professional services"),
]


def categorize(title, mtype):
    for name, pat in CATEGORIES:
        if re.search(pat, title or "", re.I):
            return name
    if mtype == "Budget":
        return "Budget"
    if mtype == "Report of Bids":
        return "Contracts and purchases"
    return "Other"


# ---------------------------------------------------------------- schema
SCHEMA = """
CREATE TABLE meetings (
  id INTEGER PRIMARY KEY, body_id INTEGER, body TEXT, date TEXT, time TEXT,
  location TEXT, comment TEXT, agenda_status TEXT, minutes_status TEXT,
  agenda_url TEXT, minutes_url TEXT, source_url TEXT, results_pending INTEGER);
CREATE TABLE items (
  id INTEGER PRIMARY KEY, file_number TEXT, name TEXT, title TEXT, type TEXT,
  status TEXT, category TEXT, amount REAL, amount_source TEXT, amounts_json TEXT,
  amends_prior INTEGER, plain_summary TEXT, summary_model TEXT, summary_prompt_version TEXT, source_url TEXT,
  staff_summary TEXT, fiscal_impact TEXT, staff_report_url TEXT);
CREATE TABLE actions (               -- one row per Legistar history row (EventItemId)
  history_id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id),
  meeting_id INTEGER, body_id INTEGER, body TEXT, date TEXT,
  agenda_sequence INTEGER, action TEXT, result TEXT, action_text TEXT,
  minutes_note TEXT, mover_id INTEGER, mover TEXT, seconder_id INTEGER, seconder TEXT,
  consent INTEGER, has_roll_call INTEGER, yes INTEGER, no INTEGER, abstain INTEGER,
  excused INTEGER, source_url TEXT);
CREATE TABLE votes (
  action_id INTEGER REFERENCES actions(history_id), person_id INTEGER,
  person TEXT, vote TEXT, PRIMARY KEY (action_id, person_id));
CREATE TABLE attendance (
  meeting_id INTEGER, person_id INTEGER, person TEXT, status TEXT,
  PRIMARY KEY (meeting_id, person_id));
CREATE TABLE people (
  id INTEGER PRIMARY KEY, name TEXT, role TEXT, district TEXT,
  term_start TEXT, term_end TEXT, email TEXT);
CREATE TABLE sponsors (item_id INTEGER, person_id INTEGER, person TEXT,
  PRIMARY KEY (item_id, person));
CREATE TABLE documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER, meeting_id INTEGER,
  kind TEXT, name TEXT, url TEXT, UNIQUE(item_id, meeting_id, url));
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_actions_item ON actions(item_id);
CREATE INDEX idx_actions_meeting ON actions(meeting_id);
CREATE INDEX idx_votes_person ON votes(person_id);
"""


def norm_vote(v):
    """Map Legistar vote names onto Yes / No / Abstain / Excused / other."""
    v = (v or "").strip()
    low = v.lower()
    if low in ("yes", "aye", "yea"):
        return "Yes"
    if low in ("no", "nay"):
        return "No"
    if low.startswith("abstain") or low.startswith("recus"):
        return "Abstain"
    if low in ("excused", "absent"):
        return "Excused"
    return v or "Not recorded"


def date(s):
    return (s or "")[:10] or None


def meeting_url(ev):
    return ev.get("EventInSiteURL") or f"{SITE}/MeetingDetail.aspx?LEGID={ev['EventId']}&GID=253"


def item_url(matter_id, guid):
    return f"{SITE}/LegislationDetail.aspx?ID={matter_id}&GUID={guid}"


def action_url(history_id, guid):
    return f"{SITE}/HistoryDetail.aspx?ID={history_id}&GUID={guid}"


def collect(year, bodies, refresh_days):
    today = dt.date.today()
    cutoff = (today - dt.timedelta(days=refresh_days)).isoformat()

    all_bodies = {b["BodyId"]: b["BodyName"] for b in fetch_all("/bodies")}
    events = []
    for b in bodies:
        flt = (f"EventBodyId eq {b} and EventDate ge datetime'{year}-01-01' "
               f"and EventDate lt datetime'{year + 1}-01-01'")
        events += fetch_all("/events", True, **{"$filter": flt, "$orderby": "EventDate"})
    events = [e for e in events if "TEST MEETING" not in (e["EventComment"] or "").upper()]
    print(f"{len(events)} meetings", flush=True)

    def is_fresh(ev):
        # Refetch anything recent or whose minutes are not final yet (minutes lag).
        return date(ev["EventDate"]) >= cutoff or ev["EventMinutesStatusName"] != "Final"

    def get_items(ev):
        return fetch(f"/events/{ev['EventId']}/eventitems", is_fresh(ev),
                     AgendaNote=1, MinutesNote=1, Attachments=1) or []
    ev_items = pmap(get_items, events)

    jobs = []   # (kind, event item, refresh)
    for ev, items in zip(events, ev_items):
        for it in items:
            if it["EventItemMoverId"] or it["EventItemPassedFlag"] is not None:
                jobs.append(("votes", it, is_fresh(ev)))
            if it["EventItemRollCallFlag"]:
                jobs.append(("rollcalls", it, is_fresh(ev)))
    print(f"{len(jobs)} vote/roll-call lookups", flush=True)
    results = pmap(lambda j: fetch(f"/eventitems/{j[1]['EventItemId']}/{j[0]}", j[2]) or [], jobs)

    matters = {}
    for ev, items in zip(events, ev_items):
        for it in items:
            if it["EventItemMatterId"]:
                m = matters.setdefault(it["EventItemMatterId"], {"it": it, "fresh": False})
                m["fresh"] = m["fresh"] or is_fresh(ev)
                m["it"] = it       # last seen wins (latest status)
    print(f"{len(matters)} legislative items", flush=True)
    mids = list(matters)
    hist = pmap(lambda m: fetch(f"/matters/{m}/histories", matters[m]["fresh"]) or [], mids)
    spons = pmap(lambda m: fetch(f"/matters/{m}/sponsors", matters[m]["fresh"]) or [], mids)
    # Votes for history rows in bodies we do not collect meetings for (other committees).
    # A history row id is the same number as its event item id.
    seen = {it["EventItemId"] for items in ev_items for it in items}
    extra = [(h, matters[m]["fresh"]) for m, rows in zip(mids, hist) for h in rows
             if h["MatterHistoryId"] not in seen
             and (h["MatterHistoryMoverId"] or h["MatterHistoryPassedFlag"] is not None)]
    print(f"{len(extra)} committee vote lookups", flush=True)
    extra_votes = pmap(lambda x: fetch(f"/eventitems/{x[0]['MatterHistoryId']}/votes", x[1]) or [], extra)
    officers = fetch_all(f"/bodies/{COUNCIL}/officerecords", True)

    # ------------------------------------------------------------ write db
    tmp = DB + ".new"
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)

    for ev in events:
        its = ev_items[events.index(ev)]
        # "Results pending": the meeting has happened and has agenda items, but no
        # results are recorded and the minutes are not final yet.
        pending = int(date(ev["EventDate"]) <= today.isoformat()
                      and ev["EventMinutesStatusName"] != "Final"
                      and any(i["EventItemMatterId"] for i in its)
                      and not any(i["EventItemActionName"] for i in its))
        db.execute("INSERT INTO meetings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            ev["EventId"], ev["EventBodyId"], ev["EventBodyName"], date(ev["EventDate"]),
            ev["EventTime"], ev["EventLocation"], ev["EventComment"],
            ev["EventAgendaStatusName"], ev["EventMinutesStatusName"],
            ev["EventAgendaFile"], ev["EventMinutesFile"], meeting_url(ev), pending))
        for kind, url in (("agenda", ev["EventAgendaFile"]), ("minutes", ev["EventMinutesFile"])):
            if url:
                db.execute("INSERT OR IGNORE INTO documents(item_id,meeting_id,kind,name,url) VALUES (NULL,?,?,?,?)",
                           (ev["EventId"], kind, kind.title(), url))

    for mid, m in matters.items():
        it = m["it"]
        title = it["EventItemTitle"] or it["EventItemMatterName"] or ""
        amts = find_amounts(title) or find_amounts(it["EventItemMatterName"])
        db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)", (
            mid, it["EventItemMatterFile"], it["EventItemMatterName"], title,
            it["EventItemMatterType"], it["EventItemMatterStatus"],
            categorize(title + " " + (it["EventItemMatterName"] or ""), it["EventItemMatterType"]),
            max(amts) if amts else None, "from title" if amts else None,
            json.dumps(amts),
            # Amending an earlier resolution repeats its dollar figure; flag it so
            # money totals do not count the same dollars twice.
            int(bool(re.match(r"\s*(amended\s+)?resolution amending\b", title, re.I))),
            None, None, None, item_url(mid, it["EventItemMatterGuid"])))
        for a in it.get("EventItemMatterAttachments") or []:
            if a.get("MatterAttachmentHyperlink"):
                db.execute("INSERT OR IGNORE INTO documents(item_id,meeting_id,kind,name,url) VALUES (?,NULL,'attachment',?,?)",
                           (mid, a["MatterAttachmentName"], a["MatterAttachmentHyperlink"]))

    event_by_id = {e["EventId"]: e for e in events}
    # History rows from the full matter history (covers every body, e.g. other committees).
    for mid, rows in zip(mids, hist):
        for h in rows:
            ev = event_by_id.get(h["MatterHistoryEventId"])
            db.execute("INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                h["MatterHistoryId"], mid, h["MatterHistoryEventId"], h["MatterHistoryActionBodyId"],
                h["MatterHistoryActionBodyName"], date(h["MatterHistoryActionDate"]),
                h["MatterHistoryAgendaSequence"], h["MatterHistoryActionName"],
                h["MatterHistoryPassedFlagName"], h["MatterHistoryActionText"],
                h["MatterHistoryMinutesNote"], h["MatterHistoryMoverId"], h["MatterHistoryMoverName"],
                h["MatterHistorySeconderId"], h["MatterHistorySeconderName"],
                h["MatterHistoryConsent"], 0, None, None, None, None,
                action_url(h["MatterHistoryId"], h["MatterHistoryGuid"])))
    # Event items are authoritative for the collected bodies (keyed on the history row id,
    # so two action rows for one file on one date stay separate and are never double counted).
    for ev, items in zip(events, ev_items):
        for it in items:
            if not it["EventItemMatterId"]:
                continue
            db.execute("INSERT OR REPLACE INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                it["EventItemId"], it["EventItemMatterId"], ev["EventId"], ev["EventBodyId"],
                ev["EventBodyName"], date(ev["EventDate"]), it["EventItemAgendaSequence"],
                it["EventItemActionName"], it["EventItemPassedFlagName"], it["EventItemActionText"],
                it["EventItemMinutesNote"], it["EventItemMoverId"], it["EventItemMover"],
                it["EventItemSeconderId"], it["EventItemSeconder"], it["EventItemConsent"],
                0, None, None, None, None, action_url(it["EventItemId"], it["EventItemGuid"])))

    for (kind, it, _), rows in zip(jobs, results):
        if kind == "votes" and it["EventItemMatterId"]:
            for v in rows:
                db.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?,?)", (
                    it["EventItemId"], v["VotePersonId"], v["VotePersonName"], norm_vote(v["VoteValueName"])))
        elif kind == "rollcalls":
            for r in rows:
                db.execute("INSERT OR REPLACE INTO attendance VALUES (?,?,?,?)", (
                    it["EventItemEventId"], r["RollCallPersonId"], r["RollCallPersonName"], r["RollCallValueName"]))
    for (h, _), rows in zip(extra, extra_votes):
        for v in rows:
            db.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?,?)", (
                h["MatterHistoryId"], v["VotePersonId"], v["VotePersonName"], norm_vote(v["VoteValueName"])))
    db.execute("""UPDATE actions SET
        yes=(SELECT COUNT(*) FROM votes WHERE action_id=history_id AND vote='Yes'),
        no=(SELECT COUNT(*) FROM votes WHERE action_id=history_id AND vote='No'),
        abstain=(SELECT COUNT(*) FROM votes WHERE action_id=history_id AND vote='Abstain'),
        excused=(SELECT COUNT(*) FROM votes WHERE action_id=history_id AND vote='Excused'),
        has_roll_call=1
        WHERE history_id IN (SELECT DISTINCT action_id FROM votes)""")

    for mid, rows in zip(mids, spons):
        for s in rows:
            if s.get("MatterSponsorName"):
                db.execute("INSERT OR IGNORE INTO sponsors VALUES (?,?,?)",
                           (mid, s["MatterSponsorNameId"], s["MatterSponsorName"]))

    # People: latest office record per person who served on the council during the year.
    latest = {}
    for o in officers:
        start, end = date(o["OfficeRecordStartDate"]), date(o["OfficeRecordEndDate"]) or "9999"
        if start <= f"{year}-12-31" and end >= f"{year}-01-01":
            cur = latest.get(o["OfficeRecordPersonId"])
            if not cur or start > date(cur["OfficeRecordStartDate"]):
                latest[o["OfficeRecordPersonId"]] = o
    for pid, o in latest.items():
        title = o["OfficeRecordTitle"] or ""
        d = re.search(r"District\s*0*(\d+)", title)
        db.execute("INSERT INTO people VALUES (?,?,?,?,?,?,?)", (
            pid, o["OfficeRecordFullName"], "Mayor" if "mayor" in title.lower() else "Council Member",
            d.group(1) if d else None, date(o["OfficeRecordStartDate"]),
            date(o["OfficeRecordEndDate"]), o["OfficeRecordEmail"]))

    for k, v in (("last_updated", now()), ("year", str(year)),
                 ("bodies", json.dumps([all_bodies.get(b, str(b)) for b in bodies])),
                 ("source", SITE), ("api", API), ("fetch_errors", str(len(STATS["errors"])))):
        db.execute("INSERT INTO meta VALUES (?,?)", (k, v))
    db.commit()
    db.close()
    os.replace(tmp, DB)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=dt.date.today().year)
    ap.add_argument("--bodies", type=int, nargs="*", default=DEFAULT_BODIES)
    ap.add_argument("--refresh-days", type=int, default=45,
                    help="refetch meetings newer than this many days (older final ones use the cache)")
    a = ap.parse_args()
    collect(a.year, a.bodies, a.refresh_days)
    print(f"fetched {STATS['fetched']}, from cache {STATS['cached']}, errors {len(STATS['errors'])}")
    for url, err in STATS["errors"][:20]:
        print("  ERROR", url, err)
    sys.exit(1 if STATS["errors"] else 0)


if __name__ == "__main__":
    main()
