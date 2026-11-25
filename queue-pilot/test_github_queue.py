import unittest

from fixture_profile import PROFILE
from github_queue import discover, repo_path, snapshot

BASE = repo_path(PROFILE)

NOW = 1735732800
PAST = "2025-01-01T01:00:00Z"

class GitHub(unittest.TestCase):
    def setUp(self):
        self.repo = {"id": PROFILE.repository_id, "full_name": PROFILE.full_name, "private": True}
        self.run = {"id": 3, "workflow_id": PROFILE.workflow_id, "run_attempt": 1,
                    "event": "workflow_dispatch", "head_branch": PROFILE.branch, "head_sha": PROFILE.head_sha,
                    "repository": self.repo, "head_repository": self.repo,
                    "created_at": PAST, "status": "in_progress"}
        self.jobs = [{"id": 40+i, "run_id": 3, "head_sha": PROFILE.head_sha, "name": f"slot-{i}",
                      "status": "queued", "labels": [PROFILE.runner_label], "runner_id": 0}
                     for i in range(1, 5)]
        self.requests = []
    def get(self, path):
        self.requests.append(path)
        if path == BASE:
            return self.repo
        if "/workflows/" in path:
            return {"total_count": 1, "workflow_runs": [self.run]}
        if path.endswith("/runs/3"):
            return self.run
        if path.endswith("/runs/3/attempts/1/jobs?per_page=100"):
            return {"total_count": len(self.jobs), "jobs": self.jobs}
        raise AssertionError(path)
    def read(self):
        return snapshot(self.get, 3, profile=PROFILE, not_before=1, clock=lambda:NOW)
    def test_exact_snapshot_and_fixed_read_paths(self):
        self.assertEqual(discover(self.get, profile=PROFILE, not_before=1, clock=lambda:NOW), (3,))
        self.assertEqual(len(self.read().jobs), 4)
        self.assertEqual(len(self.requests), 5)
        self.assertTrue(all(p.startswith(BASE) for p in self.requests))
    def test_cutover_ignores_historical_run(self):
        self.assertEqual(discover(self.get, profile=PROFILE, not_before=NOW-1, clock=lambda:NOW), ())
    def test_wrong_source_or_incomplete_jobs_refused(self):
        self.run["head_repository"] = dict(self.repo, private=False)
        with self.assertRaises(ValueError): self.read()
        self.run["head_repository"] = self.repo
        self.jobs.pop()
        with self.assertRaises(ValueError): self.read()
    def test_unrecognized_queue_job_fails_closed(self):
        self.jobs[0]["labels"] = ["self-hosted"]
        with self.assertRaises(ValueError): self.read()
    def test_cancelled_and_unassigned_contract(self):
        self.run["status"] = "completed"
        with self.assertRaises(ValueError): self.read()
        for j in self.jobs:
            j.update(status="completed", runner_id=None)
        self.assertEqual({j.state for j in self.read().jobs}, {"completed"})
