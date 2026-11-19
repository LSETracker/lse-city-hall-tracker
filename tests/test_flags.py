"""Tests for the review flags (site_builder/review_flags.py) and the pages that show them.

The fixture is made up (tests/fixture.py). Each rule has a case that must be flagged and a
near-miss that must not be. A last test runs the flags on the real database when it exists.
"""
import json, os, shutil, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "site_builder"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build, fixture, review_flags  # noqa: E402

REAL_DB = os.path.join(ROOT, "data", "council.sqlite")


class FlagsOnFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db_path = fixture.make_db(os.path.join(cls.tmp, "council.sqlite"))
        cls._old = (build.DB, build.OUT, build.RESPONSES)
        build.DB, build.OUT = cls.db_path, os.path.join(cls.tmp, "site")
        build.RESPONSES = os.path.join(cls.tmp, "flag_responses.json")
        cls.site = build.Site()
        cls.site.responses = {}
        cls.rf = review_flags.compute(cls.site)
        cls.flags = cls.rf["flags"]

    @classmethod
    def tearDownClass(cls):
        build.DB, build.OUT, build.RESPONSES = cls._old
        cls.site.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def by_rule(self, rule):
        return [f for f in self.flags if f.rule == rule]

    # ---------------------------------------------------------------- one test per rule
    def test_contract_overrun(self):
        fl = self.by_rule("contract-overrun")
        self.assertEqual(len(fl), 1)      # OK-3 is under its limit; ZERO-2 has no amount to compare with
        f = fl[0]
        self.assertIn("T2 Contracting", f.headline)
        self.assertNotIn("FINAL", f.headline)
        self.assertAlmostEqual(f.amount, 30000.0)      # 580,000 paid - (500,000 + 50,000)
        self.assertEqual(len(f.links), 2)              # one link per monthly sheet

    def test_zero_contract_goes_to_records_check_not_flags(self):
        self.assertEqual([j["job"] for j in self.rf["records_check"]["zero_contract"]], ["ZERO-2"])
        self.assertFalse(any("ZERO-2" in f.key for f in self.flags))

    def test_bad_document_total_is_listed(self):
        self.assertEqual(len(self.rf["records_check"]["bad_checks"]), 1)

    def test_duplicate_lines(self):
        keys = [f.key for f in self.by_rule("duplicate-lines")]
        self.assertTrue(any("ACME SUPPLY" in k and "4000.00" in k for k in keys))
        self.assertFalse(any("BETA CO" in k for k in keys), "a credited-back pair must not be flagged")
        self.assertFalse(any("GAMMA" in k for k in keys), "tiny identical lines must not be flagged")
        self.assertTrue(any(k.startswith("duplicate-card:") and "DELL" in k for k in keys))
        self.assertEqual(len(keys), 2)

    def test_card_large_charge(self):
        fl = self.by_rule("card-large-charge")
        self.assertEqual(len(fl), 1)
        self.assertIn("HOME DEPOT", fl[0].headline)

    def test_bidding_language_quotes_the_citys_words(self):
        fl = self.by_rule("bidding-language")
        self.assertEqual([f.key for f in fl], ["bidding-language:26-0002"])
        text = " ".join(fl[0].facts)
        self.assertIn("sole", text.lower())
        self.assertIn("must match the existing system", text)

    def test_large_no_roll_call(self):
        keys = [f.key for f in self.by_rule("large-no-roll-call")]
        # 26-0001 ($1.2M, consent) is flagged. 26-0003 had a roll call, 26-0004 only amends an earlier
        # resolution, 26-0005 is under the threshold, 26-0002 is under the threshold.
        self.assertEqual(keys, ["large-no-roll-call:26-0001"])
        self.assertIn("consent", " ".join(self.by_rule("large-no-roll-call")[0].facts).lower())

    def test_documentation_gap(self):
        keys = sorted(f.key for f in self.by_rule("documentation-gap"))
        self.assertEqual(keys, ["documentation-gap:26-0001:no-report", "documentation-gap:26-0003:blank-fiscal"])
        self.assertFalse(any(f.people for f in self.by_rule("documentation-gap")),
                         "paperwork gaps belong to the city staff process, so they are not tied to sponsors")

    def test_abstention_is_tied_to_the_member_who_abstained(self):
        fl = self.by_rule("abstentions")
        self.assertEqual(len(fl), 1)
        self.assertEqual(fl[0].person_id, 4)
        self.assertIn("Casey Cole", fl[0].headline)

    # ---------------------------------------------------------------- tables and structure
    def test_concentration_leaves_out_pass_throughs_and_merges_spellings(self):
        c = self.rf["concentration"]
        names = [t["name"] for t in c["table"]]
        self.assertFalse(any("TREASURER" in n or "TAX COLLECTOR" in n for n in names))
        self.assertAlmostEqual(c["skipped_total"], 1600000.0)
        self.assertEqual(names[0], "DELTA PAVING LLC")
        self.assertAlmostEqual(c["table"][0]["total"], 600000.0)       # 500,000 + 100,000 under two spellings
        self.assertEqual(c["table"][0]["spellings"], 2)
        self.assertAlmostEqual(sum(t["share"] for t in c["table"]), 1.0, places=6)

    def test_every_flag_has_facts_and_a_source_link(self):
        for f in self.flags:
            self.assertTrue(f.facts, f.key)
            self.assertTrue(f.links, f.key)
            self.assertTrue(all(u and u.startswith("http") for _, u in f.links), f.key)

    def test_flag_keys_and_anchors_are_unique(self):
        self.assertEqual(len({f.key for f in self.flags}), len(self.flags))
        self.assertEqual(len({f.anchor for f in self.flags}), len(self.flags))

    def test_every_rule_is_published_even_with_no_results(self):
        self.assertEqual([r["id"] for r in self.rf["rules"]], [
            "contract-overrun", "duplicate-lines", "card-large-charge", "bidding-language",
            "large-no-roll-call", "documentation-gap", "abstentions"])
        for r in self.rf["rules"]:
            for field in ("title", "checks", "why", "innocent", "threshold", "records"):
                self.assertTrue(r[field], (r["id"], field))

    def test_member_involvement_names_only_recorded_roles(self):
        alder = {f.key: roles for f, roles, _ in review_flags.member_involvement(self.rf, 2)}
        self.assertEqual(alder["large-no-roll-call:26-0001"], ["sponsor", "mover"])
        self.assertEqual(alder["bidding-language:26-0002"], ["mover"])
        cole = review_flags.member_involvement(self.rf, 4)
        self.assertIn("abstained", [r for _, roles, _ in cole for r in roles])
        # Alex Alder did not abstain, so the abstention must not show on his row.
        self.assertFalse(any(f.rule == "abstentions" for f, _, _ in review_flags.member_involvement(self.rf, 2)))
        # Documentation gaps are never tied to people.
        self.assertFalse(any(f.rule == "documentation-gap" for f, _, _ in review_flags.member_involvement(self.rf, 2)))

    # ---------------------------------------------------------------- pages
    def test_site_builds_with_flag_pages(self):
        self.site.build()
        out = build.OUT

        def read(*parts):
            with open(os.path.join(out, *parts), encoding="utf-8") as fh:
                return fh.read()
        flags_html = read("flags.html")
        for f in self.flags:
            self.assertIn(f.anchor, flags_html)
        self.assertIn("A flag is not a finding", flags_html)
        self.assertNotIn("\u2014", flags_html)                      # no em dashes in site text
        item = read("item", "26-0002.html")
        self.assertIn("Flagged for review", item)
        plain = read("item", "26-0005.html")
        self.assertNotIn("Flagged for review", plain)
        member = read("member", "2.html")
        self.assertIn("Flagged items this person is recorded on", member)
        self.assertIn("of 1</b>", member)                           # denominator shown: 1 sponsored item
        mayor = read("member", "1.html")
        self.assertIn("Flagged items this person is recorded on", mayor)
        self.assertIn("Review flags", read("index.html"))

    def test_response_is_posted_word_for_word(self):
        resp = {self.flags[0].key: {"from": "Public Works Director", "date": "2026-11-20", "text": "Change order 3 raised the limit & was approved."}}
        old = self.site.responses
        self.site.responses = resp
        try:
            card = self.site.flag_card(self.flags[0])
        finally:
            self.site.responses = old
        self.assertIn("Response from Public Works Director (2026-11-20), posted word for word", card)
        self.assertIn("Change order 3 raised the limit &amp; was approved.", card)

    def test_load_responses_ignores_empty_entries(self):
        p = os.path.join(self.tmp, "r.json")
        with open(p, "w") as fh:
            json.dump({"a": {"text": "ok"}, "b": {"text": ""}, "c": "nope"}, fh)
        self.assertEqual(list(review_flags.load_responses(p)), ["a"])
        self.assertEqual(review_flags.load_responses(os.path.join(self.tmp, "missing.json")), {})


@unittest.skipUnless(os.path.exists(REAL_DB), "run the collector first")
class FlagsOnRealData(unittest.TestCase):
    def test_flags_run_and_each_one_links_to_a_source(self):
        site = build.Site()
        rf = review_flags.compute(site)
        keys = [f.key for f in rf["flags"]]
        self.assertEqual(len(keys), len(set(keys)), "flag IDs must be unique")
        for f in rf["flags"]:
            self.assertTrue(f.facts and f.links, f.key)
            self.assertTrue(all(u for _, u in f.links), f.key)


if __name__ == "__main__":
    unittest.main()
