"""Deterministic GitHub queue/client behavior tests without network or secrets."""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from github_api import GitHubClient, GitHubError


class FakeGithub(GitHubClient):
    def __init__(self, runs, jobs):
        super().__init__("testonlytoken123456789012345678")
        self._runs = runs
        self._jobs = jobs

    def pages(self, path, key):
        if key == "workflow_runs":
            return self._runs if "status=queued" in path else []
        if key == "jobs":
            return self._jobs
        return []


def job(labels, status="queued"):
    return {"status": status, "labels": labels}


class GithubTests(unittest.TestCase):
    def test_jobs_are_scoped_to_repo_workflow_labels_and_trusted_head(self):
        runs = [
            {"path": ".github/workflows/ci.yml", "id": 11, "event": "push"},
            {"path": ".github/workflows/ci.yml", "id": 12, "event": "pull_request",
             "head_repository": {"full_name": "untrusted/fork"}},
            {"path": ".github/workflows/other.yml", "id": 13, "event": "push"},
        ]
        jobs = [
            job(["self-hosted", "Linux", "X64", "n150", "recursive-coder-ci"]),
            job(["self-hosted", "n150", "recursive-coder-ci"]),
            job(["self-hosted", "n150", "general"]),
            job(["self-hosted", "Linux", "X64", "n150", "recursive-coder-ci"], "completed"),
            job(["self-hosted", "Linux", "X64", "n150", "recursive-coder-ci", "extra"]),
        ]
        self.assertEqual(2, FakeGithub(runs, jobs).queued_jobs())

    def test_rejects_invalid_job_labels(self):
        with self.assertRaisesRegex(GitHubError, "github_job_invalid"):
            FakeGithub([{"path": "ci.yml", "id": 1, "event": "push"}], [job(None)]).queued_jobs()

    def test_paginated_results_cannot_be_truncated(self):
        class Broken(GitHubClient):
            def request(self, path, method="GET"):
                return {"total_count": 102, "workflow_runs": [{"id": 1}]}
        with self.assertRaisesRegex(GitHubError, "github_pagination_incomplete"):
            Broken("testonlytoken123456789012345678").pages("/actions/runs", "workflow_runs")

    def test_http_error_never_exposes_token_or_response_body(self):
        client = GitHubClient("github_pat_sensitive_test_only")
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url, 403, "github_pat_sensitive_response", {},
                    io.BytesIO(b"github_pat_sensitive_response")
                )
        client.opener = Opener()
        with self.assertRaises(GitHubError) as context:
            client.registration_token()
        self.assertEqual("github_http_403", str(context.exception))
        self.assertNotIn("github_pat_sensitive", str(context.exception))
