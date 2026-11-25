from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from admission import Admission, Job, Rejected, Snapshot
from fixture_profile import PROFILE

NOW = 100

def snap(run=1, base=10):
    return Snapshot(NOW, PROFILE.repository_id, True, run, 1, PROFILE.workflow_id, PROFILE.branch, PROFILE.head_sha, "workflow_dispatch",
                    tuple(Job(base+n, run, f"slot-{n}", "queued", (PROFILE.runner_label,), None)
                          for n in range(1, 5)), True)

class Queue(unittest.TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)/"state.db"
        self.db = Admission(self.path, PROFILE)
        self.addCleanup(self.db.db.close)

    def test_four_cap_and_same_snapshot_deduplicated(self):
        first = self.db.accept(snap(), NOW)
        self.assertEqual(len(first), 4)
        self.assertTrue(all(o.vcpu == 8 for o in first))
        self.assertEqual(first, self.db.accept(snap(), NOW))
        self.assertEqual(self.db.accept(snap(2, 20), NOW), [])

    def test_claim_once_even_after_restart(self):
        s = snap()
        o = self.db.accept(s, NOW)[0]
        self.assertTrue(self.db.claim(o, s, NOW))
        other = Admission(self.path, PROFILE)
        self.addCleanup(other.db.close)
        other.recover()
        self.assertFalse(other.claim(o, s, NOW))
        self.assertEqual(other.accept(snap(2, 20), NOW), [])
        self.assertFalse(other.close(o, local_absent=True, provider_absent=False, independent_evidence=True))
        self.assertTrue(other.close(o, local_absent=True, provider_absent=True, independent_evidence=True))
        self.assertEqual(len(other.accept(snap(2, 20), NOW)), 1)

    def test_cancel_before_claim_never_starts(self):
        s = snap()
        o = self.db.accept(s, NOW)[0]
        done = replace(s, jobs=(replace(s.jobs[0], state="completed"),*s.jobs[1:]))
        self.assertFalse(self.db.claim(o, done, NOW))
        self.assertEqual(len(self.db.accept(done, NOW)), 3)
        self.assertFalse(self.db.claim(o, s, NOW))

    def test_foreign_wrong_label_attempt_or_snapshot_rejected(self):
        s = snap()
        wrong_job = replace(s, jobs=(replace(s.jobs[0], labels=("self-hosted",)),*s.jobs[1:]))
        for bad in (wrong_job,replace(s, private=False),replace(s, attempt=2),replace(s, complete=False),
                    replace(s, head="a"*40),replace(s, event="pull_request"),replace(s, fetched_at=NOW-16)):
            with self.assertRaises(Rejected):
                self.db.accept(bad, NOW)
