import unittest
from unittest.mock import Mock, patch
import datetime as dt
import s5p_pal as pal


def feature(month, archive):
    return {"id": month, "properties": {
        "start_datetime": month + "-01T00:00:00+00:00",
        "end_datetime": "2026-10-01T00:00:00+00:00",
        "archive_date": archive, "l3:period": "month"}}


class PalTests(unittest.TestCase):
    def setUp(self):
        pal.monthly_ch4.cache_clear()

    def tearDown(self):
        pal.monthly_ch4.cache_clear()

    @patch("s5p_pal.requests.get")
    def test_latest_on_second_page_and_observation_order(self, get):
        old = feature("2026-08", "2026-10-20")
        new = feature("2026-09", "2026-10-03")
        get.side_effect = [
            Mock(json=lambda: {"features": [old], "links": [{"rel": "next", "href": pal.URL + "?offset=100"}]}),
            Mock(json=lambda: {"features": [new], "links": []}),
        ]
        self.assertEqual(pal.latest_monthly_ch4(), new)
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("verify", get.call_args.kwargs)

    @patch("s5p_pal.monthly_ch4")
    def test_month_is_not_end_or_publication(self, catalogue):
        from weekly_input_audit import s5p_pal_ch4_has_month
        catalogue.return_value = [feature("2026-09", "2026-10-03")]
        self.assertEqual(s5p_pal_ch4_has_month(dt.date(2026, 9, 1))[0], "present")
        self.assertEqual(s5p_pal_ch4_has_month(dt.date(2026, 10, 1))[0], "missing")

    @patch("s5p_pal.requests.get")
    def test_second_page_error_not_silently_truncated(self, get):
        get.side_effect = [
            Mock(json=lambda: {"features": [feature("2026-08", "")], "links": [{"rel": "next", "href": pal.URL + "?offset=100"}]}),
            RuntimeError("unavailable"),
        ]
        with self.assertRaises(RuntimeError):
            pal.monthly_ch4()

    @patch("s5p_pal.requests.get")
    def test_reject_external_next(self, get):
        get.return_value.json.return_value = {"features": [], "links": [{"rel": "next", "href": "https://example.com/"}]}
        with self.assertRaises(ValueError):
            pal.monthly_ch4()
