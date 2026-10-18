"""Regression tests for the endpoint's report selection.

Run with:

    python3 -m unittest -v test_mh_endpoint
"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "upstream", "macless-haystack", "endpoint"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mh_endpoint  # noqa: E402

APPLE_EPOCH_OFFSET = 978307200


def report(report_id, timestamp, payload_suffix=b""):
    body = (timestamp - APPLE_EPOCH_OFFSET).to_bytes(4, "big") + payload_suffix
    return {"id": report_id, "payload": base64.b64encode(body).decode("ascii")}


class SelectReportsTest(unittest.TestCase):

    def test_keeps_all_ids_at_the_same_timestamp(self):
        now = 1750000000
        results = [
            report("tagA", now),
            report("tagB", now),
            report("tagC", now),
        ]
        selected = mh_endpoint.select_reports(results, now - 60)
        self.assertEqual({entry["id"] for entry in selected},
                         {"tagA", "tagB", "tagC"})

    def test_drops_old_reports(self):
        now = 1750000000
        results = [report("tagA", now - 100), report("tagA", now)]
        selected = mh_endpoint.select_reports(results, now - 60)
        self.assertEqual(len(selected), 1)

    def test_deduplicates_identical_id_and_timestamp(self):
        now = 1750000000
        results = [report("tagA", now), report("tagA", now)]
        selected = mh_endpoint.select_reports(results, now - 60)
        self.assertEqual(len(selected), 1)

    def test_sorted_newest_first(self):
        now = 1750000000
        results = [
            report("tagA", now - 30),
            report("tagB", now),
            report("tagA", now - 10),
        ]
        selected = mh_endpoint.select_reports(results, now - 60)
        timestamps = [entry["id"] for entry in selected]
        self.assertEqual(timestamps, ["tagB", "tagA", "tagA"])


if __name__ == "__main__":
    unittest.main()
