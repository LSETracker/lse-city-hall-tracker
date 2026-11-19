"""Tests built from the pilot facts in the build plan (section 7) plus the
pitfalls in section 2. Run after the collector:  python3 -m unittest discover tests
"""
import os, sqlite3, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))
from collect import categorize, find_amounts, norm_vote  # noqa: E402

DB = os.path.join(ROOT, "data", "council.sqlite")


class Parsing(unittest.TestCase):
    def test_amounts(self):
        self.assertEqual(find_amounts("contract in the amount of $766,662.00 for the park"), [766662.0])
        self.assertEqual(find_amounts("not to exceed $13,805,000 aggregate"), [13805000.0])
        self.assertEqual(find_amounts("from $1.5 million to $2 million"), [1500000.0, 2000000.0])
        self.assertEqual(find_amounts("Section 18-64(b) of the Code"), [])

    def test_votes(self):
        self.assertEqual(norm_vote("Yes"), "Yes")
        self.assertEqual(norm_vote("Excused"), "Excused")
        self.assertEqual(norm_vote(None), "Not recorded")

    def test_categories(self):
        self.assertEqual(categorize("Resolution authorizing General Obligation Promissory Notes", "Resolution"), "Borrowing")
        self.assertEqual(categorize("Report of Bids and Resolution awarding contract to X", "Report of Bids"), "Contracts and purchases")
        self.assertEqual(categorize("Amending Tax Incremental District 17", "Resolution"), "TIF districts")


@unittest.skipUnless(os.path.exists(DB), "run the collector first")
class PilotFacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = sqlite3.connect(DB)

    def one(self, sql, *a):
        return self.db.execute(sql, a).fetchone()

    def council_action(self, file_no, date, action):
        return self.one("""SELECT a.result, a.yes, a.no, a.excused, a.has_roll_call FROM actions a
            JOIN items i ON i.id = a.item_id WHERE i.file_number=? AND a.date=? AND a.action=?
            AND a.body_id=138""", file_no, date, action)

    def test_sept_10_has_34_item_rows(self):
        n = self.one("""SELECT COUNT(*) FROM actions a JOIN meetings m ON m.id=a.meeting_id
            WHERE m.date='2026-09-10' AND m.body_id=138 AND m.time='6:00 PM'""")[0]
        self.assertEqual(n, 34)

    def test_bills_and_estimates_13_to_0(self):
        self.assertEqual(self.council_action("26-0950", "2026-09-10", "ADOPTED"), ("Pass", 13, 0, 0, 1))

    def test_administrator_charter_ordinance_failed_with_five_no(self):
        r = self.council_action("26-0741", "2026-08-13", "ADOPTED AS AMENDED")
        self.assertEqual(r[0], "Fail")
        self.assertEqual(r[2], 5)

    def test_two_actions_same_night_stay_separate(self):
        # Pitfall 1: 26-0879 has two action rows on 9/10; keyed on history id, neither is lost or doubled.
        n = self.one("""SELECT COUNT(*), COUNT(DISTINCT a.history_id) FROM actions a JOIN items i ON i.id=a.item_id
            WHERE i.file_number='26-0879' AND a.date='2026-09-10' AND a.body_id=138""")
        self.assertEqual(n, (2, 2))
        self.assertEqual(self.one("SELECT COUNT(*) FROM votes GROUP BY action_id, person_id HAVING COUNT(*)>1"), None)

    def test_budget_has_a_council_action_on_or_after_nov_16(self):
        # The city can move the budget meeting by a day (it went from 11/16 to 11/17), so this checks
        # that the item is found with a Common Council action in mid-November or later, not one exact date.
        r = self.one("""SELECT a.date FROM actions a JOIN items i ON i.id=a.item_id
            WHERE i.file_number='26-0588' AND a.body_id=138 ORDER BY a.date DESC""")
        self.assertIsNotNone(r, "budget item 26-0588 has no Common Council action")
        self.assertGreaterEqual(r[0], "2026-11-16")

    def test_excused_is_not_a_vote(self):
        # Pitfall 6: tallies must add up, with Excused kept out of yes and no.
        bad = self.one("""SELECT COUNT(*) FROM actions a WHERE has_roll_call=1 AND
            yes + no != (SELECT COUNT(*) FROM votes v WHERE v.action_id=a.history_id AND v.vote IN ('Yes','No'))""")[0]
        self.assertEqual(bad, 0)

    def test_thirteen_council_members_and_a_mayor(self):
        self.assertEqual(self.one("SELECT COUNT(*) FROM people WHERE role='Council Member'")[0], 13)
        self.assertEqual(self.one("SELECT COUNT(*) FROM people WHERE role='Mayor'")[0], 1)

    def test_every_record_has_a_source_link(self):
        for t in ("items", "actions", "meetings"):
            self.assertEqual(self.one(f"SELECT COUNT(*) FROM {t} WHERE source_url IS NULL OR source_url=''")[0], 0, t)

    def test_no_fetch_errors(self):
        self.assertEqual(self.one("SELECT value FROM meta WHERE key='fetch_errors'")[0], "0")


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, os.path.join(ROOT, "collector"))
import packets  # noqa: E402

WARRANTS = """CITY OF LA CROSSE
WARRANT PUBLICATION LIST
ORG
  VENDOR NAME                                 AMOUNT
GEN ADMIN TID#18
  BOARDMAN & CLARK LLP                      3,025.46
  CITY OF LA CROSSE TREASURER               3,992.51
  ENTERPRISE FM TRUST                            .07
                                            7,018.04

PARKS
  ZORO TOOLS INC                             -651.56
                                             -651.56
                               TOTAL             6,366.48
"""


class PacketParsing(unittest.TestCase):
    def test_warrant_list(self):
        rows, total = packets.parse_warrants(WARRANTS)
        self.assertEqual(total, 6366.48)
        self.assertEqual(len(rows), 4)
        self.assertAlmostEqual(sum(r[2] for r in rows), total, places=2)
        # A payee whose name starts like the page header must not be dropped.
        self.assertIn(("GEN ADMIN TID#18", "CITY OF LA CROSSE TREASURER", 3992.51), rows)

    def test_estimates_row(self):
        text = ("7 T2 Contracting        Badger Hickey Park Restrooms & Shelter     PARK-1620     11,598.54   500,562.36   75,084.35\n"
                "9 WSP USA               Green Bay St                               AGRE-25-01       910.10    95,800.00           -\n"
                "                                                                                 12,508.64   596,362.36   75,084.35\n")
        rows, total = packets.parse_estimates(text)
        self.assertEqual([r[2] for r in rows], ["PARK-1620", "AGRE-25-01"])
        self.assertEqual(rows[1][5], 0.0)
        self.assertEqual(total, 12508.64)

    def test_staff_report_sections(self):
        text = "LEGISLATION STAFF REPORT FOR COUNCIL\nSummary/Purpose\n Buy a truck.\n\nBackground\nOld one broke.\nFiscal Impact\n$50,000 from fleet fund\n\nStaff Recommendation\nApprove.\n"
        sec = packets.parse_staff_report(text)
        self.assertEqual(sec["Summary/Purpose"], "Buy a truck")
        self.assertEqual(sec["Fiscal Impact"], "$50,000 from fleet fund")


@unittest.skipUnless(os.path.exists(DB), "run the collector first")
class PacketData(unittest.TestCase):
    def test_every_document_adds_up_to_its_printed_total(self):
        db = sqlite3.connect(DB)
        if not db.execute("SELECT name FROM sqlite_master WHERE name='packet_checks'").fetchone():
            self.skipTest("run packets.py first")
        n, ok = db.execute("SELECT COUNT(*), SUM(ok) FROM packet_checks").fetchone()
        self.assertGreater(n, 0)
        bad = db.execute("SELECT kind, stated_total, parsed_total, source_url FROM packet_checks WHERE ok=0").fetchall()
        self.assertEqual(bad, [])


import budget  # noqa: E402


class BudgetParsing(unittest.TestCase):
    PAGE = """PROPOSED OPERATING BUDGET FOR
FOR THE YEAR 2027
DEPARTMENTS                      Actual           Actual               Actual     Original Budget        Proposed          Inc/(Dec)
LEGAL                               642,080          747,736              865,215         886,437            885,117             (1,320)
LA CROSSE CENTER                  3,661,957        4,415,889                  -               -                  -                    -
Total Operating Expenses     $    4,304,037   $    5,163,625       $      865,215   $     886,437   $       885,117   $        (1,320)
"""

    def test_summary_page(self):
        year, cols, rows, ok = budget.parse_page(self.PAGE, "Total Operating Expenses")
        self.assertEqual(year, 2027)
        self.assertTrue(ok)
        self.assertEqual(rows[0], ("LEGAL", 0, [642080.0, 747736.0, 865215.0, 886437.0, 885117.0, -1320.0]))
        self.assertEqual(rows[1][2][2:], [0.0, 0.0, 0.0, 0.0])
        self.assertEqual(rows[2][1], 1)

    def test_wrong_total_is_caught(self):
        bad = self.PAGE.replace("885,117   $        (1,320)", "999,999   $        (1,320)")
        self.assertFalse(budget.parse_page(bad, "Total Operating Expenses")[3])


class InterleavedCardSection(unittest.TestCase):
    TEXT = """WARRANT PUBLICATION LIST
PARKS
  ACME SUPPLY                               1,000.00
PCARD DETAIL - APRIL 2025
ORG          VENDOR #   VENDOR NAME                  AMOUNT
       100       20,768 MENARDS LA CROSSE WI                    28.88
                        TOTAL                            $28.88
CITY OF LA CROSSE
WARRANT PUBLICATION LIST
  BETA CO                                   2,000.00
                                            3,000.00
                               TOTAL             3,000.00
"""

    def test_card_section_in_the_middle_of_the_warrant_list(self):
        rows, total = packets.parse_warrants(self.TEXT)
        self.assertEqual(total, 3000.0)
        self.assertEqual([r[1] for r in rows], ["ACME SUPPLY", "BETA CO"])
        self.assertEqual(rows[1][0], "PARKS")           # the department carries across the card section
        crows, ctotal = packets.parse_pcard(self.TEXT)
        self.assertEqual((len(crows), ctotal), (1, 28.88))
