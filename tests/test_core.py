import csv
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rat_core import RepoRecord, _directories_for, analyze_repository, export_csv, is_valid_remote_url


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


class RepositoryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.tempdir.name)
        self._git("init")
        self._commit_file("src/alpha.txt", "one\n", "initial", "2024-01-01T00:00:00+00:00", "Legacy", "legacy@example.com")
        self._commit_file(".mailmap", "Canonical <canonical@example.com> Legacy <legacy@example.com>\n", "mailmap", "2024-01-02T00:00:00+00:00", "Canonical", "canonical@example.com")
        self._git("mv", "src/alpha.txt", "src/beta.txt")
        self._commit("rename only", "2024-01-03T00:00:00+00:00", "Legacy", "legacy@example.com")
        self._commit_file("src/beta.txt", "one\ntwo\n", "growth", "2024-01-04T00:00:00+00:00", "Legacy", "legacy@example.com")
        self.repo_id = f"fixture-{uuid.uuid4().hex}"
        self.record = RepoRecord(self.repo_id, "fixture", "local", "fixture", str(self.repo_path))

    def tearDown(self):
        self.tempdir.cleanup()

    def _git(self, *args, env=None):
        environment = {**os.environ, **(env or {})}
        return subprocess.run(["git", *args], cwd=self.repo_path, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.strip()

    def _commit(self, message, timestamp, name, email):
        env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp}
        self._git("commit", "-m", message, env=env)

    def _commit_file(self, relative_path, content, message, timestamp, name, email):
        target = self.repo_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self._git("add", relative_path)
        self._commit(message, timestamp, name, email)

    def test_rename_only_commit_does_not_increase_modifications(self):
        result = analyze_repository(self.record)
        self.assertEqual(result["commit_count"], 4)
        self.assertEqual(result["repository"]["churn"], 3)
        self.assertEqual(result["repository"]["modifications"], 3)

    def test_mailmap_merges_legacy_author(self):
        result = analyze_repository(RepoRecord(f"{self.repo_id}-mailmap", "fixture", "local", "fixture", str(self.repo_path)))
        authors = {item["author"] for item in result["authors"]}
        self.assertEqual(authors, {"Canonical <canonical@example.com>"})

    def test_date_range_and_manual_selection(self):
        filtered = analyze_repository(RepoRecord(f"{self.repo_id}-date", "fixture", "local", "fixture", str(self.repo_path)), start_ts=1704240000, end_ts=1704326400)
        self.assertEqual(filtered["commit_count"], 1)
        sha = self._git("rev-parse", "HEAD")
        selected = analyze_repository(RepoRecord(f"{self.repo_id}-selected", "fixture", "local", "fixture", str(self.repo_path)), start_ts=2000000000, selected_commits={sha})
        self.assertEqual(selected["commit_count"], 1)
        self.assertEqual(selected["repository"]["added"], 1)


if __name__ == "__main__":
    unittest.main()
