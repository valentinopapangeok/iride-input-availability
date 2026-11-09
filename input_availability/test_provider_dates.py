import unittest
from unittest.mock import Mock, patch
from provider_dates import nasa_dates
import weekly_input_audit as weekly
import datetime as dt


class DatesTests(unittest.TestCase):
    @patch("provider_dates.requests.get")
    def test_no_false_publication(self, get):
        get.return_value.json.return_value = {"items": [{"meta": {"revision-id": 1, "revision-date": "2026-11-06T18:00:00Z"}, "umm": {"DataGranule": {"ProductionDateTime": "2026-11-06T17:00:00Z"}}}]}
        result = nasa_dates("G123")
        self.assertNotIn("publication_date", result)
        self.assertEqual(result["catalogue_date_source"], "NASA CMR initial ingestion")
        self.assertEqual(result["production_date"], "2026-11-06T17:00:00Z")

    @patch("provider_dates.requests.get", side_effect=RuntimeError)
    def test_metadata_failure_does_not_fail_availability(self, get):
        self.assertIn("timestamp_note", nasa_dates("G123"))

    @patch("weekly_input_audit.previous_complete_days", return_value=[dt.date(2026, 11, 8)])
    def test_newer_day_is_inferred_not_confirmed_missing(self, days):
        row = {"adapter": "viirs_n20_snow", "latest_date": "2026-11-04", "found": "yes"}
        self.assertEqual(weekly.daily_rows([row], "2026-11-09T00:00:00Z", Mock())[0]["status"], "inferred_unavailable")

    @patch("weekly_input_audit.recent_months", return_value=[dt.date(2026, 11, 1)])
    def test_incomplete_month_not_due(self, months):
        row = {"adapter": "s5p_pal_ch4", "latest_date": "2026-10-01", "found": "yes"}
        self.assertEqual(weekly.monthly_rows([row], "2026-11-09T00:00:00Z", Mock())[0]["status"], "not_due")
