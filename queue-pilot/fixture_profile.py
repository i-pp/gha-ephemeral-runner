"""Synthetic values shared by the offline tests."""
from runner_profile import Profile

PROFILE_VALUES = {
    "owner": "example-ci-fixture",
    "repository": "demo-project",
    "repository_id": 101,
    "workflow_id": 202,
    "branch": "dev/sample-ci",
    "head_sha": "0a" * 20,
    "runner_label": "sample-runner",
    "app_id": 303,
    "installation_id": 404,
}
PROFILE = Profile(**PROFILE_VALUES)
