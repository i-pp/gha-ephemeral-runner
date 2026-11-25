from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest

from admission import Admission, Rejected
from fixture_profile import PROFILE
from github_queue import repo_path
from tick import poll_tick

NOW = 1735732800
BASE = repo_path(PROFILE)
RUNS = f"{BASE}/actions/workflows/{PROFILE.workflow_id}/runs"


class Tick(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.admission = Admission(Path(directory.name) / "state.db", PROFILE)
        self.addCleanup(self.admission.db.close)
        self.repo = {"id": PROFILE.repository_id, "full_name": PROFILE.full_name, "private": True}
        self.runs = {
            i: {"id": i, "workflow_id": PROFILE.workflow_id, "run_attempt": 1,
                "event": "workflow_dispatch", "head_branch": PROFILE.branch, "head_sha": PROFILE.head_sha,
                "repository": self.repo, "head_repository": self.repo,
                "created_at": "2025-01-01T01:00:00Z", "status": "in_progress"}
            for i in (3, 4)
        }
        self.jobs = {
            i: [{"id": i * 10 + j, "run_id": i, "head_sha": PROFILE.head_sha, "name": f"slot-{j}",
                 "status": "queued", "labels": [PROFILE.runner_label], "runner_id": 0}
                for j in range(1, 5)]
            for i in self.runs
        }
        self.requests = []
        self.clock_value = NOW

    def get(self, path):
        self.requests.append(path)
        if path == BASE:
            return self.repo
        if path.startswith(RUNS + "?"):
            return {"total_count": len(self.runs), "workflow_runs": list(self.runs.values())}
        for number, run in self.runs.items():
            if path == f"{BASE}/actions/runs/{number}":
                return run
            if path == f"{BASE}/actions/runs/{number}/attempts/1/jobs?per_page=100":
                jobs = self.jobs[number]
                return {"total_count": len(jobs), "jobs": jobs}
        raise AssertionError(path)

    def poll(self, get=None):
        return poll_tick(self.admission, get or self.get, not_before=NOW - 86400,
                         clock=lambda: self.clock_value)

    def test_duplicate_discovery_keeps_same_reservations_and_global_capacity(self):
        first = self.poll()
        self.assertEqual([offer.job_id for offer in first], [31, 32, 33, 34])
        self.assertEqual(sum("attempts/1" in req for req in self.requests), 2)
        self.assertTrue(all(offer.vcpu == 8 for offer in first))
        self.assertEqual(self.poll(), first)

    def test_later_invalid_snapshot_makes_no_partial_reservations(self):
        self.jobs[4].pop()
        with self.assertRaises(ValueError):
            self.poll()
        self.assertEqual(self.admission.db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))

    def test_store_error_rolls_back_entire_tick(self):
        for job in self.jobs[3][1:]:
            job.update(status="in_progress", runner_id=1)
        self.admission.db.executescript("""
            CREATE TRIGGER fail_second_run BEFORE INSERT ON jobs
            WHEN NEW.run = 4 BEGIN
                SELECT RAISE(ABORT, 'test write failure');
            END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.poll()
        self.assertEqual(self.admission.db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))

    def test_tick_revalidates_first_snapshot_after_other_reads(self):
        def slow_get(path):
            result = self.get(path)
            if "/runs/4/attempts/" in path:
                self.clock_value += 9
            elif path.endswith("/runs/4"):
                self.clock_value += 9
            return result
        with self.assertRaises(Rejected):
            self.poll(slow_get)
        self.assertEqual(self.admission.db.execute("SELECT count(*) FROM jobs").fetchone(), (0,))

    def test_assignment_cancels_unclaimed_reservation_and_reuses_capacity(self):
        first = self.poll()
        self.jobs[3][0].update(status="in_progress", runner_id=1)
        second = self.poll()
        self.assertEqual(second[:3], first[1:])
        self.assertEqual(second[3].job_id, 41)
        self.assertNotIn(first[0], second)

    def test_empty_discovery_does_not_infer_terminal_state(self):
        reserved = self.poll()
        self.runs.clear()
        self.assertEqual(self.poll(), ())
        active = self.admission.db.execute("SELECT ticket FROM jobs WHERE state='reserved'").fetchall()
        self.assertEqual({ticket for ticket, in active}, {offer.ticket for offer in reserved})


if __name__ == "__main__":
    unittest.main()
