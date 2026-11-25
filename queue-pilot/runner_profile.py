"""Validated settings. No defaults for installation- or repository-specific values."""
from dataclasses import asdict, dataclass, fields
import hashlib
import json
import re

MAX_PROFILE_BYTES = 8192


def positive_id(value):
    return type(value) is int and 0 < value < 2**63


def matches(pattern, value):
    return type(value) is str and re.fullmatch(pattern, value) is not None


def branch_name(value):
    if not matches(r"[A-Za-z0-9._/-]{1,255}", value):
        return False
    if value in ("@", "HEAD") or value.startswith(("-", "refs/")) or ".." in value:
        return False
    return all(part and not part.startswith(".") and not part.endswith((".", ".lock"))
               for part in value.split("/"))


def unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate profile field")
    return obj


@dataclass(frozen=True, kw_only=True)
class Profile:
    owner: str
    repository: str
    repository_id: int
    workflow_id: int
    branch: str
    head_sha: str
    runner_label: str
    app_id: int
    installation_id: int

    def __post_init__(self):
        if not (matches(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", self.owner)
                and len(self.owner) <= 39 and matches(r"[A-Za-z0-9._-]{1,100}", self.repository)
                and self.repository not in (".", "..") and branch_name(self.branch)
                and matches(r"[0-9A-Fa-f]{40}", self.head_sha)
                and matches(r"[A-Za-z0-9._-]{1,100}", self.runner_label)):
            raise ValueError("invalid profile name, branch, revision, or label")
        if not all(positive_id(value) for value in (
                self.repository_id, self.workflow_id, self.app_id, self.installation_id)):
            raise ValueError("profile IDs must be positive integers")
        object.__setattr__(self, "head_sha", self.head_sha.lower())

    @classmethod
    def from_json(cls, source):
        if type(source) is bytes:
            if len(source) > MAX_PROFILE_BYTES:
                raise ValueError("profile is too large")
            source = source.decode("utf-8")
        if type(source) is not str or len(source) > MAX_PROFILE_BYTES:
            raise ValueError("invalid profile input")
        try:
            obj = json.loads(source, object_pairs_hook=unique_object)
        except RecursionError:
            raise ValueError("profile is too deeply nested") from None
        if type(obj) is not dict or obj.keys() != {field.name for field in fields(cls)}:
            raise ValueError("profile must contain exactly the required fields")
        return cls(**obj)

    @property
    def full_name(self):
        return f"{self.owner}/{self.repository}"

    @property
    def db_identity(self):
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


def load_profile(path):
    with open(path, "rb") as file:
        return Profile.from_json(file.read(MAX_PROFILE_BYTES + 1))
