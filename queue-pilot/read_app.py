"""Scope-authorized GitHub App reader for the configured private queue.

Only the installation-token exchange uses POST. Queue and job requests are
GET-only. The caller owns the fixed GitHub TLS implementation and signer.
"""
from datetime import datetime
import json
import re

from github_queue import STATES, check, complete_page, repo_path, same_repo, workflow_runs_path
from runner_profile import Profile, positive_id

REPOS = "/installation/repositories?per_page=100&page=1"
READ_PERMS = {"actions": "read"}


def clear(buffer):
    if isinstance(buffer, bytearray):
        buffer[:] = b"\0" * len(buffer)


def strict_obj(pairs):
    out = dict(pairs)
    check(len(out) == len(pairs), "duplicate GitHub response key")
    return out


def allowed(path, profile):
    if type(path) is not str:
        return False
    base = repo_path(profile)
    if path == base or any(path == workflow_runs_path(profile, state) for state in STATES):
        return True
    route = re.fullmatch(
        re.escape(base) + r"/actions/runs/([1-9][0-9]{0,18})(?:/attempts/1/jobs\?per_page=100)?",
        path,
    )
    return route is not None and positive_id(int(route[1]))


class Reader:
    def __init__(self, profile, request, signer, clock):
        if type(profile) is not Profile:
            raise ValueError("a validated profile is required")
        self._profile = profile
        self.request, self.signer, self.clock = request, signer, clock
        self.token = None
        self.expires = 0

    def obj(self, method, path, key, data, expected):
        payload = None if data is None else json.dumps(data,separators=(",",":")).encode("ascii")
        status, body = self.request(method, path, key, payload, 1024**2, 10)
        try:
            check(status == expected and type(body) is bytearray and len(body) <= 1024**2,
                  "GitHub request not established")
            v = json.loads(body, object_pairs_hook=strict_obj)
            check(type(v) is dict, "GitHub response is not an object")
            return v
        except (UnicodeError, ValueError, RecursionError):
            raise ValueError("invalid GitHub response") from None
        finally:
            clear(body)

    def refresh(self):
        self.close()
        profile = self._profile
        install = f"/app/installations/{profile.installation_id}"
        jwt = self.signer(int(self.clock()))
        token = None
        try:
            check(type(jwt) is bytearray and 100 <= len(jwt) <= 8192, "invalid local signer output")
            info = self.obj("GET", install, jwt, None, 200)
            account = info.get("account")
            check(positive_id(info.get("id")) and info["id"] == profile.installation_id
                  and positive_id(info.get("app_id")) and info["app_id"] == profile.app_id
                  and info.get("target_type") == "Organization"
                  and info.get("repository_selection") == "selected"
                  and "suspended_at" in info and info["suspended_at"] is None
                  and type(account) is dict and positive_id(account.get("id"))
                  and positive_id(info.get("target_id")) and info["target_id"] == account["id"]
                  and account.get("login") == profile.owner, "wrong GitHub App installation")
            mint = self.obj("POST", install + "/access_tokens", jwt,
                            {"repository_ids": [profile.repository_id], "permissions": READ_PERMS}, 201)
            check(mint.get("repository_selection")=="selected"
                  and mint.get("permissions") in (READ_PERMS, READ_PERMS|{"metadata":"read"}),
                  "installation token was not narrowed to Actions read")
            token = bytearray(mint.pop("token"),"ascii")
            check(20<=len(token)<=8192 and re.fullmatch(rb"[A-Za-z0-9_.-]+",token) is not None,"invalid installation token")
            stamp=datetime.fromisoformat(mint["expires_at"].replace("Z","+00:00"))
            now=self.clock()
            check(stamp.utcoffset() is not None and 45<=stamp.timestamp()-now<=3700,"invalid token expiry")
            repos = complete_page(self.obj("GET", REPOS, token, None, 200), "repositories")
            check(len(repos) == 1 and same_repo(repos[0], profile)
                  and type(repos[0].get("owner")) is dict
                  and positive_id(repos[0]["owner"].get("id"))
                  and repos[0]["owner"]["id"] == account["id"]
                  and repos[0]["owner"].get("login") == profile.owner,
                  "installation token repository differs")
            self.token, self.expires = token, stamp.timestamp()
            token=None
        except (KeyError,TypeError,AttributeError,UnicodeError,ValueError):
            raise ValueError("read-only App identity or token not established") from None
        finally:
            clear(jwt)
            clear(token)

    def get(self,path):
        check(allowed(path, self._profile),"GitHub route not in read-only pilot allowlist")
        if self.token is None or self.clock()+30 >= self.expires:
            self.refresh()
        return self.obj("GET",path,self.token,None,200)

    def close(self):
        clear(self.token)
        self.token=None
        self.expires=0
