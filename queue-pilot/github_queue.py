"""Verified projections from the fixed GitHub workflow's read-only API.

The transport is injectable. It must issue authenticated HTTPS GETs only to
api.github.com, with bounded responses, no redirects and no response logging.
This module does not issue credentials or send an Actions dispatch.
"""
from datetime import datetime
from urllib.parse import urlencode

from admission import Job, Snapshot, validate
from runner_profile import positive_id

STATES = ("queued", "in_progress")


def repo_path(profile):
    return f"/repos/{profile.full_name}"


def workflow_runs_path(profile, state):
    query = urlencode({"branch": profile.branch, "event": "workflow_dispatch",
                       "status": state, "per_page": 100})
    return f"{repo_path(profile)}/actions/workflows/{profile.workflow_id}/runs?{query}"


def check(ok, reason):
    if not ok:
        raise ValueError(reason)


def same_repo(value, profile):
    return (type(value) is dict and type(value.get("id")) is int
            and value["id"] == profile.repository_id and value.get("full_name") == profile.full_name
            and value.get("private") is True)


def complete_page(data, field):
    check(type(data) is dict and type(data.get("total_count")) is int
          and type(data.get(field)) is list and 0 <= data["total_count"] <= 100
          and data["total_count"] == len(data[field]), "incomplete GitHub result")
    check(all(type(x) is dict for x in data[field]), "invalid GitHub rows")
    return data[field]


def timestamp(value):
    check(type(value) is str and len(value) <= 35, "invalid run timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    check(dt.utcoffset() is not None, "run time has no offset")
    return int(dt.timestamp())


def matches(run, profile, not_before, now):
    return (positive_id(run.get("id")) and type(run.get("workflow_id")) is int
            and run["workflow_id"] == profile.workflow_id
            and type(run.get("run_attempt")) is int and run["run_attempt"] == 1
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == profile.branch and run.get("head_sha") == profile.head_sha
            and same_repo(run.get("repository"), profile) and same_repo(run.get("head_repository"), profile)
            and not_before <= timestamp(run.get("created_at")) <= now)


def snapshot(get, run_id, *, profile, not_before, clock):
    check(positive_id(run_id) and type(not_before) is int, "exact run required")
    base = repo_path(profile)
    seen = int(clock())
    data = get(f"{base}/actions/runs/{run_id}")
    check(type(data) is dict and data.get("id") == run_id and matches(data, profile, not_before, seen)
          and data.get("status") in (*STATES, "completed"), "run changed or not in pilot")
    jobs = complete_page(get(f"{base}/actions/runs/{run_id}/attempts/1/jobs?per_page=100"), "jobs")
    def job(row):
        check(type(row.get("labels")) is list and "runner_id" in row
              and row.get("head_sha") == profile.head_sha, "job projection not exact")
        rid = row["runner_id"]
        if type(rid) is int and rid == 0 and row.get("status") == "queued":
            rid = None
        return Job(row.get("id"), row.get("run_id"), row.get("name"), row.get("status"),
                   tuple(row["labels"]), rid)
    out = Snapshot(seen, profile.repository_id, True, run_id, 1, profile.workflow_id,
                   profile.branch, profile.head_sha,
                   "workflow_dispatch", tuple(job(row) for row in jobs), True)
    validate(out, int(clock()), profile)
    if data["status"] == "completed":
        check(all(j.state == "completed" for j in out.jobs), "terminal run and jobs are out of sync")
    return out


def discover(get, *, profile, not_before, clock):
    """Find exact candidates, but admit nothing until independently projected."""
    check(type(not_before) is int and not_before > 0, "activation watermark required")
    now = int(clock())
    check(not_before <= now and same_repo(get(repo_path(profile)), profile), "private repository not established")
    found = set()
    for state in STATES:
        for run in complete_page(get(workflow_runs_path(profile, state)), "workflow_runs"):
            if matches(run, profile, not_before, now):
                found.add(run["id"])
    return tuple(sorted(found))
