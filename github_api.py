"""Bounded, repository-scoped GitHub client; never log tokens or API payloads."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


REPOSITORY = "YuriiSokolenko/recursive-coder"
REQUIRED = frozenset({"self-hosted", "n150", "recursive-coder-ci"})
SUPPORTED = REQUIRED | {"linux", "x64"}
WATCHED_WORKFLOWS = frozenset({"ci.yml"})


class GitHubError(Exception):
    """Safe reason code only; upstream response text is never propagated."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise GitHubError("github_redirect_refused")


class GitHubClient:
    def __init__(self, token: str, timeout: int = 15):
        if not token or any(ch in token for ch in "\r\n"):
            raise ValueError("missing or invalid GitHub token")
        self.token = token
        self.timeout = timeout
        self.base = f"https://api.github.com/repos/{REPOSITORY}"
        self.opener = urllib.request.build_opener(_RejectRedirects)

    def request(self, path: str, method: str = "GET") -> dict:
        if not path.startswith("/") or "//" in path or not path.startswith("/actions/"):
            raise ValueError("unexpected GitHub API path")
        request = urllib.request.Request(
            self.base + path,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "Authorization": f"Bearer {self.token}",
            },
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if method == "DELETE":
                    return {}
                payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise GitHubError("github_response_oversize")
            result = json.loads(payload)
            if not isinstance(result, dict):
                raise GitHubError("github_response_invalid")
            return result
        except GitHubError:
            raise
        except urllib.error.HTTPError as error:
            raise GitHubError(f"github_http_{error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GitHubError("github_network_error") from None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubError("github_response_invalid") from None

    def pages(self, path: str, key: str) -> list[dict]:
        output: list[dict] = []
        total = None
        for page in range(1, 11):
            separator = "&" if "?" in path else "?"
            response = self.request(f"{path}{separator}per_page=100&page={page}")
            items = response.get(key)
            count = response.get("total_count")
            if not isinstance(items, list) or type(count) is not int or count < 0 or count > 1000:
                raise GitHubError("github_pagination_invalid")
            if total is not None and total != count:
                raise GitHubError("github_pagination_changed")
            total = count
            if len(items) > 100:
                raise GitHubError("github_pagination_invalid")
            output.extend(items)
            if len(output) == total:
                return output
            if len(items) != 100 or len(output) > total:
                raise GitHubError("github_pagination_incomplete")
        raise GitHubError("github_pagination_limit")

    def queued_jobs(self) -> int:
        seen: set[int] = set()
        count = 0
        for status in ("queued", "pending", "in_progress"):
            runs = self.pages(f"/actions/runs?status={status}", "workflow_runs")
            for run in runs:
                if not isinstance(run, dict):
                    raise GitHubError("github_workflow_invalid")
                path = run.get("path", "")
                if not isinstance(path, str) or path.rsplit("/", 1)[-1] not in WATCHED_WORKFLOWS:
                    continue
                if run.get("event") == "pull_request":
                    head = run.get("head_repository")
                    if not isinstance(head, dict) or head.get("full_name") != REPOSITORY:
                        continue
                run_id = run.get("id")
                if type(run_id) is not int or run_id < 1:
                    raise GitHubError("github_workflow_invalid")
                if run_id in seen:
                    continue
                seen.add(run_id)
                jobs = self.pages(f"/actions/runs/{run_id}/jobs?filter=latest", "jobs")
                for job in jobs:
                    if not isinstance(job, dict):
                        raise GitHubError("github_job_invalid")
                    if job.get("status") != "queued":
                        continue
                    labels = job.get("labels")
                    if not isinstance(labels, list) or not labels or any(not isinstance(v, str) for v in labels):
                        raise GitHubError("github_job_invalid")
                    requested = {label.lower() for label in labels}
                    if REQUIRED <= requested and requested <= SUPPORTED:
                        count += 1
        return count

    def runners(self) -> list[dict]:
        runners = self.pages("/actions/runners", "runners")
        if any(not isinstance(item, dict) for item in runners):
            raise GitHubError("github_runner_invalid")
        return runners

    def registration_token(self) -> str:
        token = self.request("/actions/runners/registration-token", method="POST").get("token")
        if not isinstance(token, str) or not 20 <= len(token) <= 512 or not all(
            c.isascii() and (c.isalnum() or c in "_-.") for c in token
        ):
            raise GitHubError("registration_token_invalid")
        return token

    def delete_runner(self, runner_id: int) -> None:
        if type(runner_id) is not int or runner_id < 1:
            raise GitHubError("github_runner_invalid")
        self.request(f"/actions/runners/{runner_id}", method="DELETE")
