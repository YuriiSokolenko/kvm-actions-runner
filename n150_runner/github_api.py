"""Bounded, repository-scoped GitHub Actions client; never log response bodies or tokens."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable

from .config import Pool


class GitHubError(Exception):
    """Safe reason-code exception: no response body, token, or URL text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Runner:
    id: int
    name: str
    busy: bool
    status: str


class GitHub:
    def __init__(self, repository: str, token: str, *, timeout: int = 12,
                 opener=None, sleep=time.sleep):
        if not token:
            raise GitHubError("GITHUB_TOKEN_MISSING")
        self.repository = repository
        self.token = token
        self.timeout = timeout
        self.opener = opener or urllib.request.urlopen
        self.sleep = sleep
        self.url = "https://api.github.com/repos/" + repository

    def _request(self, path: str, *, method: str = "GET") -> object:
        if not path.startswith("/") or path.startswith("//"):
            raise GitHubError("GITHUB_INVALID_PATH")
        request = urllib.request.Request(self.url + path, method=method,
             headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "n150-unified-runner-manager",
             })
        for attempt in range(3):
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    data = response.read(4 * 1024 * 1024 + 1)
                    if len(data) > 4 * 1024 * 1024:
                        raise GitHubError("GITHUB_RESPONSE_OVERSIZED")
                    if method == "DELETE" and not data:
                        return None
                    try:
                        return json.loads(data)
                    except (ValueError, UnicodeError):
                        raise GitHubError("GITHUB_INVALID_JSON") from None
            except urllib.error.HTTPError as err:
                retryable = err.code in (429, 502, 503, 504) or (
                    err.code == 403 and err.headers.get("X-RateLimit-Remaining") == "0"
                )
                if retryable and attempt < 2:
                    value = err.headers.get("Retry-After", "")
                    try:
                        delay = min(6, max(1, int(value)))
                    except ValueError:
                        delay = 2 ** attempt
                    self.sleep(delay)
                    continue
                raise GitHubError("GITHUB_RATE_LIMIT" if retryable else "GITHUB_HTTP_ERROR") from None
            except (TimeoutError, urllib.error.URLError, OSError):
                if attempt < 2:
                    self.sleep(2 ** attempt)
                    continue
                raise GitHubError("GITHUB_TRANSPORT_ERROR") from None
        raise GitHubError("GITHUB_RETRY_EXHAUSTED")

    def _list(self, base: str, key: str) -> list[dict]:
        expected = None
        collected: list[dict] = []
        for page in range(1, 11):
            sep = "&" if "?" in base else "?"
            data = self._request(base + sep + f"per_page=100&page={page}")
            if not isinstance(data, dict):
                raise GitHubError("GITHUB_INVALID_PAGINATION")
            total = data.get("total_count")
            values = data.get(key)
            if type(total) is not int or total < 0 or not isinstance(values, list):
                raise GitHubError("GITHUB_INVALID_PAGINATION")
            if expected is None:
                expected = total
            if expected != total or len(values) > 100 or not all(isinstance(v, dict) for v in values):
                raise GitHubError("GITHUB_INCONSISTENT_PAGES")
            collected.extend(values)
            if len(collected) == expected:
                return collected
            if len(collected) > expected or len(values) != 100:
                raise GitHubError("GITHUB_INCOMPLETE_PAGES")
        raise GitHubError("GITHUB_PAGINATION_LIMIT")

    def runners(self) -> list[Runner]:
        raw = self._list("/actions/runners", "runners")
        results = []
        for r in raw:
            if (type(r.get("id")) is not int or not isinstance(r.get("name"), str)
                    or type(r.get("busy")) is not bool
                    or r.get("status") not in ("online", "offline")):
                raise GitHubError("GITHUB_INVALID_RUNNER")
            results.append(Runner(r["id"], r["name"], r["busy"], r["status"]))
        return results

    def registration_token(self) -> str:
        value = self._request("/actions/runners/registration-token", method="POST")
        if not isinstance(value, dict) or not isinstance(value.get("token"), str) or len(value["token"]) < 20:
            raise GitHubError("GITHUB_INVALID_REGISTRATION")
        return value["token"]

    def delete_runner(self, runner_id: int) -> None:
        if type(runner_id) is not int or runner_id < 1:
            raise GitHubError("GITHUB_INVALID_RUNNER_ID")
        self._request(f"/actions/runners/{runner_id}", method="DELETE")

    def queued_jobs_by_pool(self, pools: Iterable[Pool]) -> dict[str, int]:
        """Count eligible queued jobs across queued/pending/in-progress workflow runs.

        Admission is intentionally strict: every requested label must equal a
        builtin+pool label set. Fork PRs never reach self-hosted workers.
        """
        pool_list = [p for p in pools if p.mode == "ephemeral"]
        demand = {p.id: 0 for p in pool_list}
        if not pool_list:
            return demand
        runs = {}
        for status in ("queued", "pending", "in_progress"):
            for run in self._list("/actions/runs?status=" + status, "workflow_runs"):
                if type(run.get("id")) is not int or not isinstance(run.get("path"), str) or not isinstance(run.get("event"), str):
                    raise GitHubError("GITHUB_INVALID_WORKFLOW")
                runs[run["id"]] = run
        for run_id, run in runs.items():
            workflow = run["path"].split("/")[-1]
            matching = [p for p in pool_list if workflow in p.workflows and run["event"] in p.events]
            if not matching:
                continue
            if run["event"] in ("pull_request", "workflow_run"):
                head = run.get("head_repository")
                if not isinstance(head, dict) or head.get("full_name") != self.repository:
                    continue
            # On public repositories the authoritative job label selector must
            # be exact; a job cannot be silently assigned to a more powerful pool.
            for job in self._list(f"/actions/runs/{run_id}/jobs?filter=latest", "jobs"):
                if not isinstance(job.get("status"), str):
                    raise GitHubError("GITHUB_INVALID_JOB")
                if job["status"] != "queued":
                    continue
                labels = job.get("labels")
                if not isinstance(labels, list) or not labels or not all(isinstance(x, str) for x in labels):
                    raise GitHubError("GITHUB_INVALID_JOB_LABELS")
                labels_lower = {v.lower() for v in labels}
                for pool in matching:
                    supported = {"self-hosted", "linux", "x64"} | {v.lower() for v in pool.labels}
                    if labels_lower == supported:
                        demand[pool.id] += 1
        return demand
