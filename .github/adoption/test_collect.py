import datetime as dt
import unittest
from unittest.mock import patch

import collect


META = {"full_name": "ni/LabVIEW-CI-CD-Toolkit", "private": False, "created_at": "2026-06-15T18:07:36Z",
        "stargazers_count": 1, "forks_count": 0, "subscribers_count": 0}
NOW = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc)
ARCHIVE = {"repo": META["full_name"], "schema": 1, "traffic": {"clones": {"2026-10-01": [5, 3]},
           "views": {"2026-11-11": [2, 1], "2026-11-12": [4, 2]}}, "clients": {"2026-10-01": 51}}


class CollectorTests(unittest.TestCase):
    def test_preserves_gaps_and_older_rows_replaces_overlap_and_ignores_incomplete_day(self):
        payload = {"count": 99, "uniques": 30, "views": [
            {"timestamp": f"2026-11-{day}T00:00:00Z", "count": n, "uniques": n}
            for day, n in [(11, 3), (14, 0), (16, 30)]
        ]}
        updated = collect.combine(META, [], [], [], [], ARCHIVE, {"views": payload}, NOW, clients=0)
        self.assertEqual(updated["traffic"]["clones"], {"2026-10-01": [5, 3]})
        self.assertEqual(updated["traffic"]["views"], {"2026-11-11": [3, 3], "2026-11-12": [4, 2],
                                                       "2026-11-14": [0, 0]})
        self.assertNotIn("views", updated["traffic_windows"])
        self.assertEqual(updated["traffic_checked"], {"views": "2026-11-16T00:00:00Z"})
        self.assertEqual(updated["clients"], {"2026-10-01": 51, "2026-11-16": 0})

    def test_event_dates_and_downloads_are_separate_and_drafts_excluded(self):
        tag = {"published_at": "2026-11-15T21:00:00Z", "tag_name": "v1.0.0",
               "draft": False, "assets": [{"download_count": 14}]}
        draft = {**tag, "draft": True, "published_at": "2026-11-16T00:00:00Z"}
        result = collect.combine(META, [{"starred_at": "2026-06-23T10:00:00Z"}], [], [tag, draft],
                                 [{"merged_at": None}, {"merged_at": "2026-11-14T10:00:00Z"}], {}, {}, NOW)
        self.assertEqual(result["events"], {"stars": {"2026-06-23": 1}, "forks": {},
                                             "releases": {"2026-11-15": 1}, "merges": {"2026-11-14": 1}})
        self.assertEqual(result["current"]["downloads"], 14)
        self.assertEqual(result["latest_release"], {"tag": "v1.0.0", "date": "2026-11-15"})

    def test_refuses_to_publish_private_projects_or_mix_repository_histories(self):
        for meta, archive in [({**META, "private": True}, {}), (META, {**ARCHIVE, "repo": "someone/else"})]:
            with self.assertRaises(ValueError):
                collect.combine(meta, [], [], [], [], archive, {}, NOW)

    def test_discovery_does_not_count_toolkit_forks_or_mismatched_catalogs(self):
        results = {"total_count": 3, "incomplete_results": False, "items": [
            {"id": i, "full_name": f"someone/{name}", "private": False}
            for i, name in enumerate(["client", "fork", "other"])
        ]}
        with patch.object(collect, "request", side_effect=[
            (results, False), ({"source": {"repo": collect.LEGACY}}, False), ({"source": {"repo": "elsewhere/a"}}, False)
        ]) as api:
            self.assertEqual(collect.verified_public_clients("token", {"someone/fork"}), 1)
            self.assertEqual(api.call_count, 3)


if __name__ == "__main__":
    unittest.main()
