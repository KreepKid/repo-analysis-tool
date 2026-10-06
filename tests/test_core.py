import csv
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rat_core import _directories_for, export_csv, is_valid_remote_url


class CoreTests(unittest.TestCase):
    def test_directory_ancestors_include_root(self):
        self.assertEqual(_directories_for("src/core/main.py"), ["/", "src/", "src/core/"])

    def test_remote_url_validation(self):
        self.assertTrue(is_valid_remote_url("https://github.com/example/repo.git"))
        self.assertTrue(is_valid_remote_url("git@github.com:example/repo.git"))
        self.assertFalse(is_valid_remote_url("not a repository"))

    def test_csv_contains_repository_and_author_rows(self):
        result = {
            "repo": {"name": "demo"}, "ref_sha": "a" * 40, "commit_count": 2,
            "repository": {"object_type": "repository", "path": "/", "added": 2, "removed": 0, "growth": 2, "churn": 2, "modifications": 2, "modification_frequency": 1, "churn_rate": 1},
            "objects": [], "author_metrics": [{"path": "/", "author": "A <a@example.com>", "added": 2, "removed": 0, "growth": 2, "churn": 2, "modifications": 2, "ownership": 1}],
        }
        rows = list(csv.DictReader(export_csv(result).splitlines()))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["author"], "ALL")
        self.assertEqual(rows[1]["ownership"], "1")


if __name__ == "__main__":
    unittest.main()
