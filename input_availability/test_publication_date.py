import unittest
from unittest.mock import patch
from build_dashboard import render_latest_table
from latest_input_audit import FIELDS, run_cdse_query, Adapter


class PublicationTests(unittest.TestCase):
    def test_old_results_remain_renderable(self):
        html = render_latest_table([{"product": "07", "input_name": "PAL", "latest_date": "2026-09-01"}])
        self.assertIn("Publication date", html)
        self.assertIn("Not reported", html)

    def test_source_and_value_are_escaped(self):
        html = render_latest_table([{"publication_date": "<date>", "publication_source": '"source"'}])
        self.assertIn("&lt;date&gt;", html)
        self.assertIn("&quot;source&quot;", html)
        self.assertIn("publication_date", FIELDS)
        self.assertIn("publication_source", FIELDS)

    @patch("latest_input_audit.json_get")
    def test_cdse_uses_publication_not_content_date(self, get):
        get.return_value = {"value": [{"Name": "sample", "PublicationDate": "2026-11-05T10:00:00Z"}]}
        adapter = Adapter("test", "01", "test", (), lambda a: {})
        result = run_cdse_query(adapter, "SL_2_LST___", download_sample=False)
        self.assertEqual(result["publication_date"], "2026-11-05T10:00:00Z")
