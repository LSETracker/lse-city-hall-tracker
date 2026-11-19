#!/usr/bin/env python3
"""Review flags for the LSE City Hall Tracker.

A review flag says that a public record meets a published, rule-based test of the kind
auditors use to decide where to look first. A flag is not a finding. It does not say that
anyone did anything wrong, and every rule comes with the ordinary explanations for it,
which the site prints next to the rule.

Design rules (they are also printed on the site):
  * Flags attach to records (an item, a payment line, a contract), never to a person's
    character. A member's page lists flagged items they sponsored, moved or seconded only
    where the public record names them in that role, always with the number checked.
  * Every rule runs the same way on every record, every build. Rules with no results are
    still listed.
  * Every flag carries the facts that triggered it and a link to the city's own record.
  * Thresholds below were chosen by this site. They are not city rules, and the page says so.

Standard library only. The input is a build.Site object (see site_builder/build.py).
"""
import collections, datetime as dt, hashlib, json, os, re

CONFIG = {
    "card_single_charge": 5000.0,       # one purchasing-card charge at or above this
    "duplicate_min_amount": 2500.0,     # identical lines are only checked at or above this
    "consent_min_amount": 250000.0,     # title amount at or above this, approved with no recorded vote
    "doc_gap_min_amount": 100000.0,     # title amount at or above this, thin paperwork
    "concentration_top_n": 25,
}

# Payees and accounts that mostly pass money along (taxes to other governments, payroll,
# benefits, debt payments, the purchasing-card bill). They are left out of the concentration
# table so that it shows spending choices. The page lists what was left out.
PASS_VENDOR = re.compile(
    r"TREASURER|SCHOOL DIST|SCHOOL BOARD|\bCOUNTY\b|TECHNICAL COLLEGE|DEPT OF REVENUE|"
    r"DEPARTMENT OF REVENUE|INTERNAL REVENUE|\bIRS\b|EFTPS|\bU\.? ?S\.? BANK\b|STATE OF WI|"
    r"WISCONSIN RETIREMENT|PAYING AGENT", re.I)
PASS_DEPT = re.compile(r"PROPERTY TAX|REAL ESTATE|PAYROLL|LIABILIT|DEBT SERVICE|PENSION|RETIREMENT|BENEFIT", re.I)
SUFFIXES = {"INC", "LLC", "LLP", "LTD", "CO", "CORP", "CORPORATION", "COMPANY", "INCORPORATED", "THE", "LP", "PC"}

BID_PATTERNS = [
    ("sole or single source", re.compile(r"\b(sole|single)[- ]source", re.I)),
    ("waiving bids or quotes", re.compile(r"\bwaiv\w*\b[^.;]{0,60}\b(bids?|bidding|quotes?|proposals?|RFP)\b", re.I)),
    ("without bids or competition", re.compile(r"\bwithout\b[^.;]{0,25}\b(bids?|bidding|competition|competitive)\b", re.I)),
    ("exempt from bidding", re.compile(r"\bexempt\w*\b[^.;]{0,40}\b(bids?|bidding)\b", re.I)),
    ("non-competitive", re.compile(r"\bnon-?competitive", re.I)),
    ("only one bid or proposal", re.compile(
        r"\bonly (one|1) (bid|bidder|proposal|quote)\b|\bsingle (bid|bidder)\b|\bone (bid|proposal) (was |were )?(received|submitted)", re.I)),
    ("emergency purchase", re.compile(r"\bemergency (purchase|procurement|repair|contract|work)", re.I)),
]


def money(v):
    if v is None:
        return ""
    return f"${v:,.0f}" if abs(v - round(v)) < 0.005 else f"${v:,.2f}"


def _month(ym):
    return dt.date(int(ym[:4]), int(ym[5:7]), 1).strftime("%B %Y")


def _nice(d):
    x = dt.date.fromisoformat(d)
    return f"{x.strftime('%b')} {x.day}, {x.year}"


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def snippet(text, m, pad=110):
    s, t = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return ("…" if s > 0 else "") + _clean(text[s:t]) + ("…" if t < len(text) else "")


def vendor_key(name):
    """Collapse spelling variants ('ACME SUPPLY INC', 'Acme Supply') into one key."""
    t = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace("&", " AND "))
    toks = [w for w in t.split() if w not in SUFFIXES]
    return " ".join(toks) or (name or "").upper()


class Flag:
    """One record that meets one published test.

    people: True when the public record names a person in a role on this item (sponsor,
    mover, seconder, abstaining member) and that role is part of what the rule is about.
    """

    def __init__(self, rule, key, headline, facts, links=(), item_id=None, person_id=None,
                 amount=None, people=False):
        self.rule, self.key, self.headline = rule, key, headline
        self.facts, self.links = list(facts), list(links)
        self.item_id, self.person_id, self.amount, self.people = item_id, person_id, amount, people
        slug = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")[:40]
        self.anchor = "f-" + slug + "-" + hashlib.sha1(key.encode()).hexdigest()[:6]


def rules(cfg):
    return [
        dict(id="contract-overrun", short="Paid past the contract amount",
             title="Paid this year is more than the contract amount plus contingency",
             checks="For each construction or design contract on the city's monthly estimates sheets, the site adds up the progress "
                    "payments listed this year and compares the total with the contract amount plus contingency printed on the latest sheet.",
             why="Payments that pass the approved amount can mean a change order, a mistake, or spending nobody approved. "
                 "Comparing payments with the contract is one of the first things an audit does.",
             innocent="A change order approved after the contract was signed raises the real limit, but the sheet may still show the old amount. "
                      "The amounts on the sheet can also be misread from the PDF.",
             threshold="Any amount over the line, with 50 cents of room for rounding.",
             records="Monthly estimates sheets"),
        dict(id="duplicate-lines", short="Identical payment lines",
             title="The same payee and the same amount listed more than once in one monthly list",
             checks="Looks for the same payee, the same department (or card account) and the same dollar amount listed two or more times "
                    "in one monthly list. A pair cancelled by a matching negative line is skipped.",
             why="Paying one bill twice is among the most common payment errors, and duplicate testing is a standard first step in an audit.",
             innocent="Identical amounts are often separate, legitimate bills, such as several leased vehicles or equal monthly fees paid together. "
                      "The documents show no invoice numbers, so the list alone cannot settle it.",
             threshold=f"Lines of {money(cfg['duplicate_min_amount'])} or more.",
             records="Monthly bills and estimates lists, including the purchasing-card detail"),
        dict(id="card-large-charge", short="Large card charges",
             title="A single purchasing-card charge at or above the threshold",
             checks="Lists single charges on the city's purchasing-card bill at or above the threshold.",
             why="Cards are meant for small, routine buys. Large single charges are where card policy and bidding rules matter most.",
             innocent="Some cards carry higher limits for travel, conferences, software or fuel, and some departments use cards for planned purchases.",
             threshold=f"Single charges of {money(cfg['card_single_charge'])} or more.",
             records="Purchasing-card detail in the monthly bills lists"),
        dict(id="bidding-language", short="Bidding language",
             title="The city's own wording mentions sole source, waived bids or no competition",
             checks="Searches the city's own wording on every item (title, staff summary, fiscal impact, and the action and minutes text) "
                    "for phrases such as sole source, waiving bids, or without competition. The matching words are quoted.",
             why="An award made without competition leaves less of a paper trail than a bid does, so auditors check that the reason is written down and approved.",
             innocent="Sole-source and waived-bid awards are allowed in many cases, such as proprietary equipment, emergencies, or work only one firm can do. "
                      "The flag shows that the language is in the record. It does not say the award was wrong.",
             threshold="No dollar threshold. Any item with a matching phrase.",
             records="Legistar item titles, action text, minutes notes and city staff reports"),
        dict(id="large-no-roll-call", short="Large items, no recorded vote",
             title="A large amount approved with no member-by-member vote on record",
             checks="Finds council-approved items whose title states a dollar amount at or above the threshold and whose approving action "
                    "has no recorded individual votes (a voice vote or consent agenda).",
             why="When an item passes on a voice or consent vote, the public record shows no one's position, so residents cannot hold anyone to it.",
             innocent="Routine items, including large ones, are often grouped and passed together, and any member can ask to pull an item for its own vote.",
             threshold=f"Title amount of {money(cfg['consent_min_amount'])} or more. Items that only amend an earlier resolution are left out.",
             records="Legistar action records"),
        dict(id="documentation-gap", short="Thin paperwork",
             title="A large item with no staff report found, or a blank fiscal impact section",
             checks="Finds council-approved items stating a large amount where the site found no attachment named “staff report”, "
                    "or where the staff report's Fiscal Impact section is blank.",
             why="Members decide on the paperwork they are given. A large item with no cost explanation is harder to review.",
             innocent="Some items are documented in differently named attachments or explained in the meeting, and the site finds staff reports by file name.",
             threshold=f"Title amount of {money(cfg['doc_gap_min_amount'])} or more. Items that only amend an earlier resolution are left out.",
             records="Legistar item attachments and staff reports"),
        dict(id="abstentions", short="Abstentions",
             title="A member abstained or recused on a recorded vote",
             checks="Lists every abstention or recusal recorded on a Common Council roll call. “Excused” means absent and is not included.",
             why="Stepping back from a vote is how a member avoids a conflict of interest. Auditors check that it happens when it should and that the reason is recorded.",
             innocent="Abstaining is often the right thing to do and is not a mark against the member. It is listed so residents can see the pattern and ask why.",
             threshold="No threshold. Every recorded abstention is listed.",
             records="Legistar roll-call votes"),
    ]


# ------------------------------------------------------------------ rules
def contract_jobs(site):
    """One row per contract on the estimates sheets, adding up payments in order."""
    jobs = collections.OrderedDict()
    for r in site.q("SELECT * FROM estimates ORDER BY bills_month, rowid"):
        j = jobs.setdefault(r["job"], dict(job=r["job"], contractor="", name="", paid=0.0, amt=0.0, cont=0.0, n=0, links=[]))
        j["contractor"] = re.sub(r"\s*\((PARTIAL )?FINAL\)", "", r["contractor"] or "")
        j["name"] = r["contract_name"] or j["name"]
        j["paid"] += r["estimate"] or 0.0
        j["amt"], j["cont"] = r["contract_amount"] or 0.0, r["contingency"] or 0.0
        j["n"] += 1
        link = (f"Estimates sheet, {_month(r['bills_month'])}", r["source_url"])
        if link not in j["links"]:
            j["links"].append(link)
    return list(jobs.values())


def contract_overrun(site, cfg):
    out = []
    for j in contract_jobs(site):
        limit = j["amt"] + j["cont"]
        if j["amt"] > 0 and j["paid"] > limit + 0.5:
            over = j["paid"] - limit
            label = j["name"] or f"job {j['job']}"
            out.append(Flag(
                "contract-overrun", f"contract-overrun:{j['job']}",
                f"{j['contractor']}, {label}: paid this year is {money(over)} more than the contract amount plus contingency",
                [f"Contract amount on the latest sheet: {money(j['amt'])}",
                 f"Contingency: {money(j['cont']) if j['cont'] else 'none listed'}",
                 f"Paid this year across {j['n']} payment{'s' if j['n'] != 1 else ''}: {money(j['paid'])}",
                 f"Over the limit by {money(over)} ({100 * over / limit:.1f}% of the limit). Payments made in earlier years are not counted, so the real total may be higher.",
                 f"Job number {j['job']}"],
                j["links"], amount=over))
    return out


def duplicate_lines(site, cfg):
    out = []
    neg = {(r["bills_month"], r["vendor"], round(-r["amount"], 2))
           for r in site.q("SELECT bills_month, vendor, amount FROM payments WHERE amount < 0")}
    for r in site.q("""SELECT bills_month, department, vendor, amount, COUNT(*) AS n, MIN(source_url) AS url FROM payments
                       WHERE amount >= ? GROUP BY bills_month, department, vendor, amount HAVING COUNT(*) >= 2""",
                    cfg["duplicate_min_amount"]):
        if (r["bills_month"], r["vendor"], round(r["amount"], 2)) in neg:
            continue
        out.append(Flag(
            "duplicate-lines", f"duplicate:{r['bills_month']}:{r['department']}:{r['vendor']}:{r['amount']:.2f}",
            f"{r['vendor']}: {r['n']} identical lines of {money(r['amount'])} in the {_month(r['bills_month'])} bills list",
            [f"{r['n']} lines of {money(r['amount'])} each, {money(r['n'] * r['amount'])} together",
             f"Listed under {r['department']}",
             "No matching negative line (credit or reversal) appears in the same list"],
            [(f"Bills list, {_month(r['bills_month'])} (PDF)", r["url"])], amount=r["n"] * r["amount"]))
    for r in site.q("""SELECT bills_month, org, merchant, amount, COUNT(*) AS n, MIN(source_url) AS url FROM pcard
                       WHERE amount >= ? GROUP BY bills_month, org, merchant, amount HAVING COUNT(*) >= 2""",
                    cfg["duplicate_min_amount"]):
        out.append(Flag(
            "duplicate-lines", f"duplicate-card:{r['bills_month']}:{r['org']}:{r['merchant']}:{r['amount']:.2f}",
            f"Purchasing card, {r['merchant']}: {r['n']} identical charges of {money(r['amount'])} in the {_month(r['bills_month'])} bills list",
            [f"{r['n']} charges of {money(r['amount'])} each, {money(r['n'] * r['amount'])} together", f"Card account code {r['org']}"],
            [(f"Bills list, {_month(r['bills_month'])} (PDF)", r["url"])], amount=r["n"] * r["amount"]))
    return out


def card_large_charge(site, cfg):
    out = []
    for r in site.q("""SELECT bills_month, org, merchant, amount, COUNT(*) AS n, MIN(source_url) AS url FROM pcard
                       WHERE amount >= ? GROUP BY bills_month, org, merchant, amount""", cfg["card_single_charge"]):
        n = f" ({r['n']} identical charges)" if r["n"] > 1 else ""
        out.append(Flag(
            "card-large-charge", f"card-large:{r['bills_month']}:{r['org']}:{r['merchant']}:{r['amount']:.2f}",
            f"Purchasing card: {money(r['amount'])} at {r['merchant']} in the {_month(r['bills_month'])} bills list{n}",
            [f"Single charge of {money(r['amount'])}", f"Card account code {r['org']}",
             "Merchant name is as printed on the card statement"],
            [(f"Bills list, {_month(r['bills_month'])} (PDF)", r["url"])], amount=r["amount"]))
    return out


def bidding_language(site, cfg):
    acts = collections.defaultdict(list)
    for a in site.actions:
        acts[a["item_id"]].append(a)
    out = []
    for it in site.items.values():
        fields = [("official title", it["title"]), ("short name", it["name"]),
                  ("city staff summary", it["staff_summary"]), ("city staff fiscal impact", it["fiscal_impact"])]
        for a in acts[it["id"]]:
            fields += [("council record", a["action_text"]), ("minutes note", a["minutes_note"])]
        hits, seen = [], set()
        for label, text in fields:
            if not text:
                continue
            for name, pat in BID_PATTERNS:
                m = pat.search(text)
                if m:
                    sn = snippet(text, m)
                    if sn not in seen:
                        seen.add(sn)
                        hits.append((label, name, sn))
        if not hits:
            continue
        appr = site.approved.get(it["id"])
        facts = [f"In the {label}, matching “{name}”: “{sn}”" for label, name, sn in hits[:3]]
        facts.append(f"Approved by the council on {_nice(appr['date'])}" if appr else f"Status on Legistar: {it['status']}")
        if it["amount"] is not None:
            facts.append(f"Amount in the title: {money(it['amount'])}")
        out.append(Flag(
            "bidding-language", f"bidding-language:{it['file_number']}",
            f"{it['name'] or it['file_number']}: the city's wording mentions {hits[0][1]}",
            facts, [(f"Legistar file {it['file_number']}", it["source_url"])],
            item_id=it["id"], amount=it["amount"], people=True))
    return out


def large_no_roll_call(site, cfg):
    out = []
    for item_id, a in site.approved.items():
        it = site.items[item_id]
        if it["amount"] is None or it["amends_prior"] or it["amount"] < cfg["consent_min_amount"] or a["has_roll_call"]:
            continue
        facts = [f"Amount in the title: {money(it['amount'])}",
                 f"Approved by the council on {_nice(a['date'])} ({(a['action'] or '').capitalize()})",
                 "The city recorded no member-by-member vote for this action"]
        if a["consent"]:
            facts.append("Marked as a consent item in Legistar")
        out.append(Flag(
            "large-no-roll-call", f"large-no-roll-call:{it['file_number']}",
            f"{it['name'] or it['file_number']}: {money(it['amount'])} approved with no recorded vote by member",
            facts, [("Legistar action record", a["source_url"]), (f"Legistar file {it['file_number']}", it["source_url"])],
            item_id=item_id, amount=it["amount"], people=True))
    return out


def documentation_gap(site, cfg):
    out = []
    for item_id, a in site.approved.items():
        it = site.items[item_id]
        if it["amount"] is None or it["amends_prior"] or it["amount"] < cfg["doc_gap_min_amount"]:
            continue
        if not it["staff_report_url"]:
            why, tag = "The site found no attachment named “staff report” on this item", "no-report"
        elif not _clean(it["fiscal_impact"]):
            why, tag = "The staff report's Fiscal Impact section is blank", "blank-fiscal"
        else:
            continue
        links = [(f"Legistar file {it['file_number']}", it["source_url"])]
        if it["staff_report_url"]:
            links.append(("Staff report (PDF)", it["staff_report_url"]))
        out.append(Flag(
            "documentation-gap", f"documentation-gap:{it['file_number']}:{tag}",
            f"{it['name'] or it['file_number']}: {money(it['amount'])} approved. {why}",
            [f"Amount in the title: {money(it['amount'])}", f"Approved by the council on {_nice(a['date'])}", why],
            links, item_id=item_id, amount=it["amount"], people=False))
    return out


def abstentions(site, cfg):
    out = []
    for a in site.rollcalls:
        if not a["abstain"] or a["item_id"] not in site.items:
            continue
        it = site.items[a["item_id"]]
        for v in site.votes[a["history_id"]]:
            if v["vote"] != "Abstain":
                continue
            facts = [f"{v['person']} abstained or recused on “{(a['action'] or '').capitalize()}” on {_nice(a['date'])}",
                     f"Tally: {a['yes']} yes, {a['no']} no, {a['abstain']} abstain"]
            if it["amount"] is not None:
                facts.append(f"Amount in the title: {money(it['amount'])}")
            facts.append("The city's record does not give a reason unless the minutes do")
            out.append(Flag(
                "abstentions", f"abstain:{a['history_id']}:{v['person_id']}",
                f"{v['person']} abstained on {it['name'] or it['file_number']}",
                facts, [("Legistar action record", a["source_url"]), (f"Legistar file {it['file_number']}", it["source_url"])],
                item_id=it["id"], person_id=v["person_id"], amount=it["amount"], people=True))
    return out


# ------------------------------------------------------------------ tables that are not flags
def concentration(site, cfg):
    rows = site.q("SELECT vendor, department, bills_month, amount FROM payments")
    total = sum(r["amount"] for r in rows)
    keep = collections.defaultdict(lambda: dict(t=0.0, names=collections.Counter(), months=set(), depts=set()))
    skipped, skipped_total, kept_total = collections.defaultdict(float), 0.0, 0.0
    for r in rows:
        vendor, dept = r["vendor"] or "", r["department"] or ""
        reason = ("payee looks like a government, bank or tax agency" if PASS_VENDOR.search(vendor)
                  else "charged to a tax, payroll, benefit or debt account" if PASS_DEPT.search(dept) else None)
        if reason:
            skipped[(vendor, reason)] += r["amount"]
            skipped_total += r["amount"]
            continue
        k = keep[vendor_key(vendor)]
        k["t"] += r["amount"]
        k["names"][vendor] += 1
        k["months"].add(r["bills_month"])
        k["depts"].add(dept)
        kept_total += r["amount"]
    ranked = sorted(keep.values(), key=lambda k: -k["t"])
    table, running, half_n = [], 0.0, None
    for i, k in enumerate(ranked):
        running += k["t"]
        if half_n is None and kept_total and running >= kept_total / 2:
            half_n = i + 1
    for k in ranked[:cfg["concentration_top_n"]]:
        table.append(dict(name=k["names"].most_common(1)[0][0], total=k["t"], share=k["t"] / kept_total if kept_total else 0,
                          months=len(k["months"]), depts=len(k["depts"]), spellings=len(k["names"])))
    excluded = sorted(((v, reason, amt) for (v, reason), amt in skipped.items()), key=lambda x: -x[2])[:10]
    return dict(total=total, kept_total=kept_total, skipped_total=skipped_total, n_payees=len(keep), half_n=half_n,
                table=table, excluded=excluded)


def records_check(site):
    zero = [j for j in contract_jobs(site) if j["amt"] == 0 and j["paid"] > 0]
    bad = site.q("SELECT kind, stated_total, parsed_total, source_url FROM packet_checks WHERE ok=0")
    return dict(zero_contract=zero, bad_checks=[dict(r) for r in bad])


# ------------------------------------------------------------------ assemble
def compute(site, config=None):
    cfg = dict(CONFIG, **(config or {}))
    flags = []
    for fn in (contract_overrun, duplicate_lines, card_large_charge, bidding_language,
               large_no_roll_call, documentation_gap, abstentions):
        found = fn(site, cfg)
        found.sort(key=lambda f: (-(f.amount or 0), f.key))
        flags += found

    # Who the public record names on each flagged item (only for rules where that is relevant).
    ids = {f.item_id for f in flags if f.item_id is not None and f.people and f.person_id is None}
    involve = collections.defaultdict(lambda: dict(sponsor=set(), mover=set(), seconder=set(), votes=collections.defaultdict(list)))
    for i in ids:
        for s in site.sponsors[i]:
            involve[i]["sponsor"].add(s["person_id"])
    for a in site.council:
        if a["item_id"] in ids:
            if a["mover_id"]:
                involve[a["item_id"]]["mover"].add(a["mover_id"])
            if a["seconder_id"]:
                involve[a["item_id"]]["seconder"].add(a["seconder_id"])
    for a in site.rollcalls:
        if a["item_id"] in ids:
            for v in site.votes[a["history_id"]]:
                involve[a["item_id"]]["votes"][v["person_id"]].append(
                    f"{v['vote']} on “{(a['action'] or '').capitalize()}” ({_nice(a['date'])})")
    return dict(flags=flags, rules=rules(cfg), config=cfg, concentration=concentration(site, cfg),
                records_check=records_check(site), involve=involve,
                by_item=_group(flags, lambda f: f.item_id))


def _group(flags, keyfn):
    d = collections.defaultdict(list)
    for f in flags:
        k = keyfn(f)
        if k is not None:
            d[k].append(f)
    return d


def member_involvement(rf, pid):
    """Flagged items where the public record names this person as sponsor, mover, seconder
    or abstaining member. Returns [(flag, roles, votes)]."""
    rows = []
    for f in rf["flags"]:
        if not f.people or f.item_id is None:
            continue
        if f.person_id is not None:
            if f.person_id == pid:
                rows.append((f, ["abstained"], []))
            continue
        inv = rf["involve"][f.item_id]
        roles = [r for r in ("sponsor", "mover", "seconder") if pid in inv[r]]
        if roles:
            rows.append((f, roles, inv["votes"].get(pid, [])))
    return rows


def load_responses(path):
    """Responses from officials or the city, posted word for word next to a flag.
    File format: {"<flag id>": {"from": "...", "date": "YYYY-MM-DD", "text": "..."}}"""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("text")}
