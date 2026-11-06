import unittest
from unittest.mock import patch
import latest_input_audit as audit


class ActiveViirsTests(unittest.TestCase):
    def test_four_main_rows_no_legacy_or_duplicate_candidates(self):
        rows = [a for a in audit.ADAPTERS if a.product in {"02", "05"} and "viirs" in a.name]
        self.assertEqual({a.name for a in rows}, {"viirs_n20_sst", "viirs_n21_sst", "viirs_n20_snow", "viirs_n21_snow"})
        self.assertTrue(all(a.section == "main" for a in rows))

    @patch("latest_input_audit.run_viirs_snow")
    @patch("latest_input_audit.run_cmr_latest")
    def test_exact_datasets_and_downloads(self, cmr, snow):
        for adapter in audit.ADAPTERS:
            if adapter.name.startswith("viirs_n"):
                adapter.runner(adapter)
        self.assertEqual([c.args[1] for c in cmr.call_args_list], ["VIIRS_N20-STAR-L2P-v2.80", "N21-VIIRS-L2P-ACSPO-v2.80"])
        self.assertTrue(all(c.kwargs["download_sample"] for c in cmr.call_args_list))
        self.assertEqual([c.args[1] for c in snow.call_args_list], ["VJ110A1F", "VJ210A1F"])
