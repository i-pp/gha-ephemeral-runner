"""Offline admission for an exact trusted private workflow.

The host adapter must acquire a complete fresh authenticated GitHub snapshot.
This module never opens a provider connection or starts a VM.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3
from uuid import uuid4

from runner_profile import Profile, positive_id

CAPACITY = 4
ACTIVE = ("reserved", "claimed", "uncertain")
JOB_NAMES = {f"slot-{n}" for n in range(1, 5)}
MAX_SNAPSHOT_AGE = 15


class Rejected(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    fetched_at: int
    repo_id: int
    private: bool
    run_id: int
    attempt: int
    workflow_id: int
    branch: str
    head: str
    event: str
    jobs: tuple
    complete: bool


@dataclass(frozen=True)
class Job:
    job_id: int
    run_id: int
    name: str
    state: str
    labels: tuple
    runner_id: int | None


@dataclass(frozen=True)
class Offer:
    ticket: str
    run_id: int
    attempt: int
    job_id: int
    vcpu: int = 8


def validate(s, now, profile):
    if not (type(s) is Snapshot and type(now) is int and type(s.fetched_at) is int
            and 0 <= now - s.fetched_at <= MAX_SNAPSHOT_AGE
            and type(s.repo_id) is int and s.repo_id == profile.repository_id and s.private is True
            and positive_id(s.run_id) and type(s.attempt) is int and s.attempt == 1
            and type(s.workflow_id) is int and s.workflow_id == profile.workflow_id
            and (s.branch, s.head, s.event) == (profile.branch, profile.head_sha, "workflow_dispatch")
            and s.complete is True and type(s.jobs) is tuple and len(s.jobs) == 4):
        raise Rejected("unverified workflow snapshot")
    names, ids = set(), set()
    for j in s.jobs:
        if not (type(j) is Job and j.name in JOB_NAMES and j.name not in names
                and positive_id(j.job_id) and j.job_id not in ids and j.run_id == s.run_id
                and j.state in ("queued", "in_progress", "completed")
                and j.labels == (profile.runner_label,)
                and (j.runner_id is None or positive_id(j.runner_id))
                and not (j.state == "queued" and j.runner_id is not None)):
            raise Rejected("unverified job identity")
        names.add(j.name)
        ids.add(j.job_id)
    return {j.job_id: j for j in s.jobs}


class Admission:
    def __init__(self, path, profile):
        if type(profile) is not Profile:
            raise ValueError("a validated profile is required")
        self._profile = profile
        self.db = sqlite3.connect(path, isolation_level=None, timeout=3)
        try:
            self.db.executescript("""
              PRAGMA journal_mode=WAL;
              PRAGMA synchronous=FULL;
              CREATE TABLE IF NOT EXISTS jobs(
                run INTEGER NOT NULL, attempt INTEGER NOT NULL, job INTEGER NOT NULL,
                ticket TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('reserved','claimed','uncertain','closed','cancelled')),
                PRIMARY KEY (run,attempt,job));
              CREATE TABLE IF NOT EXISTS profile_binding(
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL);
            """)
            with self.txn():
                stored = self.db.execute("SELECT identity FROM profile_binding WHERE singleton=1").fetchone()
                if stored is None:
                    if self.db.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None:
                        raise Rejected("unbound database with existing reservations")
                    self.db.execute("INSERT INTO profile_binding VALUES(1, ?)", (profile.db_identity,))
                elif stored != (profile.db_identity,):
                    raise Rejected("database belongs to a different profile")
        except BaseException:
            self.db.close()
            raise

    @property
    def profile(self):
        return self._profile

    @contextmanager
    def txn(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def accept(self, s, now):
        return self.accept_batch((s,), now)

    def accept_batch(self, snapshots, now):
        """Atomically reserve capacity using complete, freshly projected runs."""
        runs = {}
        for s in snapshots:
            jobs = validate(s, now, self.profile)
            identity = (s.run_id, s.attempt)
            if identity in runs:
                raise Rejected("duplicate run snapshot")
            runs[identity] = jobs
        if not runs:
            return []

        with self.txn():
            for (run, attempt), jobs in runs.items():
                for j in jobs.values():
                    if j.state != "queued":
                        self.db.execute("UPDATE jobs SET state='cancelled' "
                                        "WHERE run=? AND attempt=? AND job=? AND state='reserved'",
                                        (run, attempt, j.job_id))
            held = self.db.execute("SELECT count(*) FROM jobs WHERE state IN (?,?,?)", ACTIVE).fetchone()[0]
            offers = []
            for (run, attempt), jobs in sorted(runs.items()):
                for j in sorted(jobs.values(), key=lambda row: row.job_id):
                    if held >= CAPACITY:
                        break
                    if j.state != "queued":
                        continue
                    wrote = self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?,?,?,?, 'reserved')",
                                            (run, attempt, j.job_id, uuid4().hex))
                    held += wrote.rowcount
                offers.extend(Offer(ticket, run, attempt, job) for job, ticket in self.db.execute(
                    "SELECT job,ticket FROM jobs WHERE run=? AND attempt=? AND state='reserved' ORDER BY job",
                    (run, attempt)) if jobs.get(job) and jobs[job].state == "queued")
            return offers

    def claim(self, offer, s, now):
        jobs = validate(s, now, self.profile)
        if (type(offer) is not Offer or offer.vcpu != 8 or
                (offer.run_id, offer.attempt) != (s.run_id, s.attempt) or
                offer.job_id not in jobs or jobs[offer.job_id].state != "queued"):
            return False
        with self.txn():
            return self.db.execute("UPDATE jobs SET state='claimed' WHERE run=? AND attempt=? "
                                   "AND job=? AND ticket=? AND state='reserved'",
                                   (offer.run_id, offer.attempt, offer.job_id, offer.ticket)).rowcount == 1

    def recover(self):
        # A crash after claim does not prove that the launch did not occur.
        with self.txn():
            self.db.execute("UPDATE jobs SET state='uncertain' WHERE state='claimed'")

    def close(self, offer, *, local_absent, provider_absent, independent_evidence):
        if type(offer) is not Offer or not all(x is True for x in
                                              (local_absent, provider_absent, independent_evidence)):
            return False
        with self.txn():
            return self.db.execute("UPDATE jobs SET state='closed' WHERE run=? AND attempt=? "
                                   "AND job=? AND ticket=? AND state IN ('claimed','uncertain')",
                                   (offer.run_id, offer.attempt, offer.job_id, offer.ticket)).rowcount == 1
