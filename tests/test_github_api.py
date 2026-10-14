import json
import unittest
from urllib.parse import urlsplit, parse_qs

from n150_runner.config import load_config
from n150_runner.github_api import GitHub, GitHubError

CONFIG = load_config("config/pools.json")
PI, GENERAL = [p for p in CONFIG.pools if p.id in ("pi", "general")]


class Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return None

    def read(self, size):
        return self.value[:size]


def run(id, workflow, event="push", head="YuriiSokolenko/social-mcp"):
    return {"id": id, "path": ".github/workflows/" + workflow, "event": event,
            "head_repository": {"full_name": head}}


def job(labels, status="queued"):
    return {"status": status, "labels": labels}


class FixtureAPI:
    def __init__(self):
        self.calls = []
        self.invalid = False

    def __call__(self, request, timeout):
        path = urlsplit(request.full_url)
        query = parse_qs(path.query)
        self.calls.append((path.path, query))
        if "/actions/runs/" in path.path and "/jobs" in path.path:
            runid = int(path.path.split("/runs/")[1].split("/")[0])
            jobs = {
                10: [job(["self-hosted", "linux", "x64", "n150", "pi-agent"])],
                20: [job(["self-hosted", "linux", "x64", "n150", "general"])],
                30: [job(["self-hosted", "linux", "x64", "n150", "general"])],
                40: [job(["self-hosted", "linux", "x64", "n150", "general", "control"])],
            }[runid]
            return Response({"total_count": len(jobs), "jobs": jobs})
        if "/actions/runs" in path.path:
            if self.invalid:
                return Response({"total_count": 2, "workflow_runs": []})
            status = query["status"][0]
            runs = [run(10, "pi-issue-agent.yml"),
                    run(20, "ci.yml", "pull_request"),
                    run(30, "ci.yml", "pull_request", "malicious/fork"),
                    run(40, "ci.yml")] if status == "queued" else []
            return Response({"total_count": len(runs), "workflow_runs": runs})
        raise AssertionError("unexpected endpoint")


class GitHubDiscoveryTest(unittest.TestCase):
    def test_exact_labels_reject_forks_and_wrong_lane(self):
        fixture = FixtureAPI()
        client = GitHub("YuriiSokolenko/social-mcp", "test-token", opener=fixture)
        counts = client.queued_jobs_by_pool([PI, GENERAL])
        self.assertEqual({"pi": 1, "general": 1}, counts)
        self.assertGreaterEqual(len(fixture.calls), 3)

    def test_demand_cache_reduces_requests_and_expires(self):
        fixture = FixtureAPI()
        clock = [100.0]
        client = GitHub("YuriiSokolenko/social-mcp", "test-token",
                        opener=fixture, clock=lambda: clock[0],
                        demand_cache_seconds=30)
        expected = {"pi": 1, "general": 1}
        self.assertEqual(expected, client.queued_jobs_by_pool([PI, GENERAL]))
        initial_calls = len(fixture.calls)
        self.assertGreater(initial_calls, 3)
        clock[0] += 10
        self.assertEqual(expected, client.queued_jobs_by_pool([PI, GENERAL]))
        self.assertEqual(initial_calls, len(fixture.calls))
        clock[0] += 21
        self.assertEqual(expected, client.queued_jobs_by_pool([PI, GENERAL]))
        self.assertGreater(len(fixture.calls), initial_calls)

    def test_cached_demand_is_not_exposed_to_mutation(self):
        fixture = FixtureAPI()
        client = GitHub("YuriiSokolenko/social-mcp", "test-token", opener=fixture)
        first = client.queued_jobs_by_pool([PI, GENERAL])
        first["pi"] = 1000
        self.assertEqual(1, client.queued_jobs_by_pool([PI, GENERAL])["pi"])

    def test_rate_limited_token_stops_repeated_network_requests(self):
        from urllib.error import HTTPError

        current = [100.0]
        calls = []
        waits = []
        def throttled(request, timeout):
            calls.append(request.full_url)
            raise HTTPError(request.full_url, 429, "rate limit",
                            {"Retry-After": "1"}, None)

        client = GitHub("YuriiSokolenko/social-mcp", "test-token",
                        opener=throttled, sleep=waits.append,
                        clock=lambda: current[0])
        with self.assertRaises(GitHubError) as context:
            client.runners()
        self.assertEqual("GITHUB_RATE_LIMIT", context.exception.code)
        self.assertEqual(3, len(calls))
        with self.assertRaises(GitHubError):
            client.runners()
        self.assertEqual(3, len(calls))  # no new HTTP while cooldown active
        current[0] += 61
        with self.assertRaises(GitHubError):
            client.runners()
        self.assertEqual(6, len(calls))

    def test_general_workflow_run_requires_same_repo_head(self):
        class Stub(GitHub):
            def _list(self, path, key):
                if key == "workflow_runs":
                    if "status=queued" not in path:
                        return []
                    return [
                        run(71, "pi-usage.yml", "workflow_run", "untrusted/fork"),
                        run(72, "pi-usage.yml", "workflow_run", "YuriiSokolenko/social-mcp"),
                        {"id": 73, "path": ".github/workflows/pi-reconcile.yml",
                         "event": "workflow_run"},
                    ]
                if key == "jobs":
                    return [job(["self-hosted", "linux", "x64", "n150", "general"])]
                raise AssertionError(key)
        client = Stub("YuriiSokolenko/social-mcp", "test-token")
        self.assertEqual({"general": 1}, client.queued_jobs_by_pool([GENERAL]))

    def test_partial_pages_rejected_without_partial_demand(self):
        fixture = FixtureAPI()
        fixture.invalid = True
        client = GitHub("YuriiSokolenko/social-mcp", "test-token", opener=fixture)
        with self.assertRaises(GitHubError) as cm:
            client.queued_jobs_by_pool([PI, GENERAL])
        self.assertEqual("GITHUB_INCOMPLETE_PAGES", cm.exception.code)


if __name__ == "__main__":
    unittest.main()
