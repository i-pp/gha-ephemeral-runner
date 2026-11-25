#!/usr/bin/env python3
"""Configure the GitHub Actions ephemeral runner queue and admission pilot."""

from __future__ import annotations

import argparse
from contextlib import suppress
import getpass
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import sys
import tempfile
from urllib.parse import quote
import warnings

PAT_SETTINGS = "https://github.com/settings/personal-access-tokens/new"
PAT_DOCS = "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens"
APP_DOCS = "https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation"
API_HOST = "api.github.com"
BODY_LIMIT = 2**20
CHECKOUT = Path(__file__).resolve().parent


# This checkout's profile schema is authoritative for both setup and runtime.
sys.path.insert(0, str(CHECKOUT / "queue-pilot"))
try:
    from runner_profile import Profile, branch_name as valid_branch_name, positive_id
finally:
    sys.path.pop(0)


def input_id(value):
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or not positive_id(int(value)):
        raise ValueError("Expected a positive numeric GitHub ID.")
    return int(value)


def owner_name(value):
    if len(value) > 39 or not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", value):
        raise ValueError("Expected a GitHub organization login, not a URL.")
    return value


def repo_name(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in (".", ".."):
        raise ValueError("Expected the short repository name, not a URL.")
    return value


def branch_name(value):
    if not valid_branch_name(value):
        raise ValueError("Expected a short Git branch name, not refs/heads/...")
    return value


def exact(value, pattern, message):
    if not re.fullmatch(pattern, value):
        raise ValueError(message)
    return value


def read_secret(prompt):
    if not sys.stdin.isatty():
        raise ValueError("Token lookup requires an interactive terminal; use manual ID entry otherwise.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError("Hidden input is unavailable; rerun with manual ID entry.") from None


def github_get(path, token):
    """One bounded, read-only request to GitHub.com; never follow redirects."""
    conn = http.client.HTTPSConnection(API_HOST, timeout=10, context=ssl.create_default_context())
    try:
        conn.request("GET", path, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "gha-ephemeral-runner-setup",
        })
        with conn.getresponse() as reply:
            if reply.status != 200:
                raise ValueError(f"GitHub lookup returned HTTP {reply.status}. Check access or use manual IDs.")
            body = reply.read(BODY_LIMIT + 1)
            if len(body) > BODY_LIMIT:
                raise ValueError("GitHub response exceeds the size limit.")
    except (OSError, http.client.HTTPException):
        raise ValueError("Could not reach the GitHub API; check connectivity or use manual IDs.") from None
    finally:
        conn.close()
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("GitHub returned invalid JSON.") from None
    if type(data) is not dict:
        raise ValueError("GitHub returned an unexpected response.")
    return data


def lookup_ids(owner, repository, selector, token, get=github_get):
    owner_name(owner)
    repo_name(repository)
    if len(token) > 1024 or not re.fullmatch(r"github_pat_[A-Za-z0-9_]{10,}", token):
        raise ValueError("Expected a fine-grained GitHub personal access token.")
    if re.fullmatch(r"[0-9]+", selector):
        requested_id = input_id(selector)
    else:
        exact(selector, r"[A-Za-z0-9_.-]{1,128}\.ya?ml", "Expected a workflow ID or YAML filename.")
        requested_id = None
    base = f"/repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
    repo = get(base, token)
    account = repo.get("owner")
    if (repo.get("private") is not True or not positive_id(repo.get("id"))
            or type(repo.get("full_name")) is not str
            or repo["full_name"].casefold() != f"{owner}/{repository}".casefold()
            or type(account) is not dict or account.get("type") != "Organization"
            or type(account.get("login")) is not str or account["login"].casefold() != owner.casefold()):
        raise ValueError("GitHub did not return the requested private organization repository.")
    workflow = get(f"{base}/actions/workflows/{quote(selector, safe='')}", token)
    workflow_id = workflow.get("id")
    if (not positive_id(workflow_id)
            or (requested_id is not None and requested_id != workflow_id)
            or (requested_id is None and workflow.get("path") != f".github/workflows/{selector}")):
        raise ValueError("GitHub did not return the requested workflow.")
    return repo["id"], workflow_id


def prompt_profile(ask=input, tell=print, secret=read_secret, lookup=lookup_ids):
    owner = owner_name(ask("GitHub organization login: ").strip())
    repository = repo_name(ask("Private repository name (without the organization): ").strip())
    permission = ask("Look up the repository/workflow IDs with a read-only token? [y/N]: ").strip().lower()
    if permission not in ("", "n", "no", "y", "yes"):
        raise ValueError("Answer yes or no.")
    if permission in ("y", "yes"):
        tell(f"Create a fine-grained token: {PAT_SETTINGS}")
        tell(f"Token scope and expiration help: {PAT_DOCS}")
        tell(f"Select only {owner}/{repository}, grant Actions: Read-only, and set a short expiration.")
        selector = ask("Workflow filename (for example, pilot.yml) or workflow ID: ").strip()
        token = secret("Fine-grained token (hidden; not saved): ").strip()
        try:
            repository_id, workflow_id = lookup(owner, repository, selector, token)
        finally:
            del token
    else:
        repository_id = input_id(ask("Numeric repository ID: ").strip())
        workflow_id = input_id(ask("Numeric workflow ID: ").strip())
    branch = branch_name(ask("Trusted workflow branch: ").strip())
    head_sha = exact(ask("Approved commit SHA (40 hex digits): ").strip(), r"[A-Fa-f0-9]{40}",
                     "Expected an exact 40-character commit SHA.").lower()
    runner_label = exact(ask("Dedicated runner label: ").strip(), r"[A-Za-z0-9_.-]{1,100}",
                         "Expected 1-100 letters, digits, dots, hyphens or underscores.")
    tell(f"GitHub App installations: https://github.com/organizations/{owner}/settings/installations")
    tell(f"How to find your GitHub App ID and installation ID: {APP_DOCS}")
    tell("These two IDs are non-secret identifiers. Do not enter a private key, client secret, or token here.")
    app_id = input_id(ask("GitHub App ID: ").strip())
    installation_id = input_id(ask("GitHub App installation ID: ").strip())
    return {"owner": owner, "repository": repository, "repository_id": repository_id,
            "workflow_id": workflow_id, "branch": branch, "head_sha": head_sha,
            "runner_label": runner_label, "app_id": app_id, "installation_id": installation_id}


def save_profile(path, profile):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(profile, output, indent=2)
            output.write("\n")
        os.link(temp_name, path)  # Publish atomically and never replace an existing file or link.
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)


def main(argv=None):
    default = Path.home() / ".config" / "gha-ephemeral-runner" / "profile.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=default,
                        help="new profile file outside the checkout (default: %(default)s)")
    args = parser.parse_args(argv)
    path = args.output.expanduser().absolute()
    try:
        if path.resolve().is_relative_to(CHECKOUT):
            raise ValueError("Choose a profile path outside the source checkout.")
        if path.exists() or path.is_symlink():
            raise FileExistsError
        print("Queue pilot setup only. This script neither registers runners nor starts VMs.")
        profile = prompt_profile()
        Profile(**profile)
        save_profile(path, profile)
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. No profile was written.", file=sys.stderr)
        return 1
    except FileExistsError:
        print("Setup stopped: the output file already exists. It was not overwritten.", file=sys.stderr)
        return 1
    except (ValueError, OSError) as error:
        print(f"Setup stopped: {error}", file=sys.stderr)
        return 1
    print(f"Private profile written to {path}. No token or key was saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
