import json
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from biography.history_sources import collect_ethereum, collect_local_git, collect_github, _safe_remote

TEST_ADDRESS = "0x" + "0" * 40


class HistorySourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, path, *arguments):
        return subprocess.run(["git", "-C", str(path), *arguments], check=True,
                              capture_output=True).stdout.decode().strip()

    def repository(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, "init", "-q")
        self.git(path, "config", "user.name", "Example Contributor")
        self.git(path, "config", "user.email", "contributor@example.test")
        (path / "tracked").write_text("first")
        self.git(path, "add", "tracked")
        self.git(path, "commit", "-qm", "A personal commit\n\nBody stays intact")
        return path

    def test_git_deduplicates_shared_commit_with_repository_evidence_and_author_scope(self):
        first = self.repository("one")
        second = self.root / "two"
        subprocess.run(["git", "clone", "-q", str(first), str(second)], check=True)
        self.git(first, "-c", "user.name=Upstream Person", "-c", "user.email=upstream@example.test",
                 "commit", "--allow-empty", "-qm", "Upstream context")
        report = collect_local_git(self.root / "out", repositories=[
            {"path": str(first), "repository": "example-owner/project"},
            {"path": str(second), "repository": "example-owner/project"},
            {"path": "/missing/should-not-read", "repository": "example-owner/excluded-project"}],
            excluded_repositories=["example-owner/excluded-project"],
            author_aliases={"emails": ["contributor@example.test"]}, infer_author_aliases=False)
        records = [json.loads(line) for line in (self.root / "out/commits.jsonl").read_text().splitlines()]
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["unique_commits"], 2)
        self.assertEqual(report["personal_author_commits"], 1)
        personal = next(row for row in records if row["personal_attribution"])
        self.assertEqual(len(personal["repositories"]), 2)
        self.assertIn("Body stays intact", personal["body"])
        self.assertEqual(len(report["excluded"]), 1)

    def test_git_failure_and_limit_are_partial_not_empty_complete(self):
        repo = self.repository("one")
        self.git(repo, "commit", "--allow-empty", "-qm", "Second")
        report = collect_local_git(self.root / "out", repositories=[
            {"path": str(repo)}, {"path": str(self.root / "missing")}],
            max_commits_per_repository=1, infer_author_aliases=False)
        self.assertEqual(report["status"], "partial")
        self.assertTrue(report["repositories"][0]["truncated"])
        self.assertEqual(len(report["errors"]), 1)

    def test_ethereum_cursor_dedup_and_distinct_variant_preservation(self):
        tx = {"hash": "0xAbC", "timestamp": "2020-01-01T00:00:00Z", "value": "1"}
        calls = []
        def fetch(url):
            calls.append(url)
            if "/transactions" in url:
                if "?" not in url:
                    return {"items": [tx], "next_page_params": {"block_number": 10, "index": 2}}
                return {"items": [tx, {**tx, "value": "2"}], "next_page_params": None}
            if "/token-transfers" in url:
                return {"items": [{"transaction_hash": "0xAbC", "log_index": 3}], "next_page_params": None}
            return {"items": [{"transaction_hash": "0xAbC", "index": 4}], "next_page_params": None}
        report = collect_ethereum(self.root / "out", TEST_ADDRESS, fetcher=fetch)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["coverage"]["transactions"]["records"], 2)
        self.assertEqual(report["coverage"]["transactions"]["duplicate_records"], 1)
        self.assertTrue(any("block_number=10" in url for url in calls))
        rows = [json.loads(line) for line in (self.root / "out/token-transfers.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["stable_id"], "1:log:0xabc:3")
        raw = json.loads((self.root / "out/raw/transactions/page-000001.json").read_text())
        self.assertEqual(raw["response"]["items"], [tx])

    def test_ethereum_failed_later_page_is_partial_with_resume_cursor(self):
        def fetch(url):
            if "/transactions" in url:
                if "?" in url:
                    raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
                return {"items": [{"hash": "0x1"}], "next_page_params": {"index": 5}}
            raise urllib.error.HTTPError(url, 404, "Unavailable", {}, None)
        report = collect_ethereum(self.root / "out", TEST_ADDRESS, fetcher=fetch)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["coverage"]["transactions"]["status"], "partial")
        self.assertEqual(report["coverage"]["transactions"]["next_page_params"], {"index": 5})
        self.assertEqual(report["coverage"]["token-transfers"]["status"], "failed")
        self.assertEqual(report["coverage"]["token-transfers"]["records"], 0)
        self.assertEqual(len(report["errors"]), 3)

    def test_ethereum_repeated_cursor_and_page_cap_never_claim_completeness(self):
        report = collect_ethereum(self.root / "out", TEST_ADDRESS, max_pages=3, fetcher=lambda _: {
            "items": [{"hash": "0x1"}], "next_page_params": {"index": 1}})
        self.assertEqual(report["status"], "partial")
        self.assertTrue(all(row["reason"] == "repeated_cursor" for row in report["coverage"].values()))

    def test_ethereum_resume_replays_raw_pages_without_refetching_or_losing_rows(self):
        def first(url):
            if "?" in url:
                raise urllib.error.HTTPError(url, 503, "Temporary", {}, None)
            return {"items": [{"hash": "0xold"}], "next_page_params": {"index": 8}}
        output = self.root / "out"
        collect_ethereum(output, TEST_ADDRESS, fetcher=first)
        original_raw = (output / "raw/transactions/page-000001.json").read_bytes()
        called = []
        def second(url):
            called.append(url)
            self.assertIn("index=8", url)
            return {"items": [{"hash": "0xnew"}], "next_page_params": None}
        report = collect_ethereum(output, TEST_ADDRESS, fetcher=second, resume=True)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["coverage"]["transactions"]["records"], 2)
        self.assertEqual(len(called), 3)
        self.assertEqual((output / "raw/transactions/page-000001.json").read_bytes(), original_raw)

    def test_remote_url_never_retains_userinfo_or_query_tokens(self):
        self.assertEqual(_safe_remote("https://token:secret@github.com/example-owner/repo.git?access_token=secret"),
                         "https://github.com/example-owner/repo.git")
        self.assertEqual(_safe_remote("git@github.com:example-owner/repo.git"), "github.com:example-owner/repo.git")

    def test_github_retains_shared_commit_evidence_and_requires_linked_author(self):
        def fetch(repository, page):
            return [{"sha": "abc", "commit": {"message": "Known commit", "author": {"date": "2020-01-01"}},
                     "author": {"login": "example-owner"}},
                    {"sha": "unknown", "commit": {"message": "Unlinked author"}, "author": None}]
        report = collect_github(self.root / "github", fetcher=fetch, author_login="example-owner", repositories=[
            {"name": "example-owner/one"}, {"name": "example-owner/two"}, {"name": "example-owner/excluded-project"}],
            excluded_repositories=["example-owner/excluded-project"])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["unique_commits"], 2)
        self.assertEqual(report["personal_author_commits"], 1)
        rows = [json.loads(line) for line in (self.root / "github/commits.jsonl").read_text().splitlines()]
        self.assertTrue(all(len(row["repositories"]) == 2 for row in rows))
        self.assertEqual(len(report["excluded"]), 1)

    def test_unconfigured_sources_do_not_discover_personal_defaults(self):
        with patch("biography.history_sources._git") as git:
            report = collect_local_git(self.root / "git")
            self.assertEqual(report["status"], "not_configured")
            git.assert_not_called()
        with patch("biography.history_sources.subprocess.run") as run:
            report = collect_github(self.root / "github")
            self.assertEqual(report["status"], "not_configured")
            run.assert_not_called()
        with self.assertRaises(TypeError):
            collect_ethereum(self.root / "ethereum")

    def test_github_without_configured_author_retains_context_only(self):
        report = collect_github(self.root / "github", repositories=[{"name": "example-owner/project"}], fetcher=lambda *_: [{"sha": "abc", "commit": {"message": "Project change"}, "author": {"login": "example-owner"}}])
        self.assertEqual(report["personal_author_commits"], 0)
        self.assertIsNone(report["author_filter"])

    def test_explicit_author_aliases_and_provider_configuration(self):
        repo = self.repository("one")
        report = collect_local_git(self.root / "git", repositories=[{"path": str(repo)}], infer_author_aliases=False, author_aliases={"emails": ["contributor@example.test"]})
        self.assertEqual(report["personal_author_commits"], 1)
        calls = []
        def fetch(url):
            calls.append(url)
            return {"items": [], "next_page_params": None}
        report = collect_ethereum(self.root / "ethereum", TEST_ADDRESS, ens_name="example.eth", provider_root="https://explorer.example.invalid/api/v2", fetcher=fetch)
        self.assertEqual(report["ens_name"], "example.eth")
        self.assertTrue(all(url.startswith("https://explorer.example.invalid/api/v2/") for url in calls))


if __name__ == "__main__":
    unittest.main()
