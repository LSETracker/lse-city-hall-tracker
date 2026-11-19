"""Tests for public-facing wording: failed-motion list, cancelled meetings, share tags, footer."""
import os, shutil, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "site_builder"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build, fixture  # noqa: E402


class PublicPages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        db = fixture.make_db(os.path.join(cls.tmp, "council.sqlite"))
        cls._old = (build.DB, build.OUT, build.RESPONSES)
        build.DB, build.OUT = db, os.path.join(cls.tmp, "site")
        build.RESPONSES = os.path.join(cls.tmp, "none.json")
        cls.site = build.Site()
        cls.site.build()

    @classmethod
    def tearDownClass(cls):
        build.DB, build.OUT, build.RESPONSES = cls._old
        cls.site.db.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def read(self, name):
        with open(os.path.join(build.OUT, name), encoding="utf-8") as f:
            return f.read()

    def test_share_tags_and_footer_email(self):
        h = self.read("index.html")
        self.assertIn('property="og:title"', h)
        self.assertIn('name="description"', h)
        self.assertIn("mailto:lsetracker@yahoo.com", h)

    def test_cancelled_detection(self):
        self.assertTrue(build.is_cancelled({"comment": "Meeting CANCELLED per clerk"}))
        self.assertFalse(build.is_cancelled({"comment": "Special meeting"}))
        self.assertFalse(build.is_cancelled({"comment": None}))

    def test_failed_line_lists_every_item_and_counts(self):
        ids = list(self.site.items)[:3]
        failed = [{"item_id": ids[0], "action": "amend"}, {"item_id": ids[0], "action": "amend"},
                  {"item_id": ids[1], "action": "approve"}]
        out = self.site.failed_line(failed)
        self.assertIn("<b>3</b> motions failed", out)
        self.assertIn("<b>2</b> items", out)
        self.assertIn("(2 motions)", out)
        self.assertEqual(self.site.failed_line([]), "No motions failed.")

    def test_no_em_dashes_on_home(self):
        self.assertNotIn("—", self.read("index.html"))


if __name__ == "__main__":
    unittest.main()
