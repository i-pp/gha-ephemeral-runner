# GitHub Actions Ephemeral Runner

This repository is an early prototype for giving each trusted GitHub Actions job
a fresh virtual machine. The current code validates snapshots of a private
repository's Actions queue and makes local SQLite reservations. It does **not**
register runners, launch VMs, dispatch workflows, or run a background service.
The live controller and hypervisor integration are not included.

## Quick start

Use Linux with Python 3.12 or later. No extra Python packages or administrator access
are required. From the source checkout, run:

```sh
python3 -B setup.py
```

The script writes a new `~/.config/gha-ephemeral-runner/profile.json` with mode
`0600`, outside the source checkout. Use `--output /path/to/new/profile.json`
for a different path. It will not replace an existing profile.

Have these details ready:

- **Private repository and workflow:** An organization-owned private repository
  and a numeric workflow ID or workflow filename. For optional lookup, the
  script shows the [fine-grained token creation page](https://github.com/settings/personal-access-tokens/new)
  and prompts for the token with echo disabled. Choose that organization as the
  resource owner, grant access to only that repository, set **Actions: Read-only**
  and a short expiration. GitHub grants Metadata read access automatically.
  The token is used only for two read-only lookups on GitHub.com and is not
  written to disk. Skip the lookup to enter the two numeric IDs manually.
- **Trusted execution inputs:** A branch, the reviewed commit's 40-character
  SHA, and a dedicated runner label. The current pilot accepts only
  `workflow_dispatch`, attempt 1, and exactly four jobs named
  `slot-1` through `slot-4`, each with only the configured runner label.
  It models a global limit of four reservations, each requesting 8 vCPUs.
- **GitHub App identifiers:** The numeric App ID and installation ID for an App
  installed on the organization and scoped to the selected repository. They
  are not secrets and are not the lookup token. The script shows where to find
  them. See [GitHub App authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).
  Setup does not ask for an App private key or verify the installation. Keep any
  key outside the checkout.

A successful setup means the profile was written, not that live runners are
ready. The optional lookup validates repository and workflow identity, not the
commit contents, runner label, App installation, or VM settings. Do not point
production workflows at this prototype.

## Run the offline checks

```sh
python3 -B -m unittest discover -s tests -v
(cd queue-pilot && python3 -B -m unittest discover -v)
```

The tests use synthetic records and do not need a GitHub token or a hypervisor.

## Keep local configuration local

Do not commit generated profiles, personal access tokens, App private keys,
runner registration tokens, database files, or machine-specific settings.
The queue reader and admission logic are in [queue-pilot](queue-pilot/). The App
reader accepts a caller-supplied signer and transport. No production signer or
transport is provided.
