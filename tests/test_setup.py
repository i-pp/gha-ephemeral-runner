import importlib.util
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "setup.py"
spec = importlib.util.spec_from_file_location("runner_setup", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

OWNER = "demo-organization"
REPO = "example-pilot"
SECRET = "github_pat_" + "syntheticTESTonly"
SHA = "A" * 40


class Setup(unittest.TestCase):
    def test_manual_cli_writes_profile_without_network_or_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private" / "profile.json"
            answers = [OWNER, REPO, "", "81", "82", "dev/pilot", SHA, "example-ci", "83", "84"]
            result = subprocess.run(
                [sys.executable, "-B", str(SCRIPT), "--output", str(path)],
                input="\n".join(answers) + "\n", text=True, capture_output=True, timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(setup.Profile.from_json(path.read_bytes()).full_name, f"{OWNER}/{REPO}")
            self.assertEqual(json.loads(path.read_text()), {
                "owner": OWNER, "repository": REPO, "repository_id": 81, "workflow_id": 82,
                "branch": "dev/pilot", "head_sha": SHA.lower(), "runner_label": "example-ci",
                "app_id": 83, "installation_id": 84,
            })

    def test_setup_and_runtime_use_the_same_branch_rules(self):
        for invalid in ("HEAD", "dev/a,b", "dev/demo./one", "refs/heads/main"):
            with self.subTest(branch=invalid), self.assertRaises(ValueError):
                setup.branch_name(invalid)
        self.assertEqual(setup.branch_name("dev/build-1.2"), "dev/build-1.2")

    def test_existing_regular_file_or_symlink_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "profile.json"
            other = Path(tmp) / "other.json"
            other.write_text("untouched")
            target.symlink_to(other)
            with self.assertRaises(FileExistsError):
                setup.save_profile(target, {"owner": OWNER})
            self.assertTrue(target.is_symlink())
            self.assertEqual(other.read_text(), "untouched")
            with self.assertRaises(FileExistsError):
                setup.save_profile(other, {"owner": OWNER})
            self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["other.json", "profile.json"])
            self.assertEqual(other.read_text(), "untouched")

    def test_cancelled_or_invalid_input_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "profile.json"
            for answers in ("", "not valid!\n"):
                with self.subTest(answers=answers):
                    result = subprocess.run(
                        [sys.executable, "-B", str(SCRIPT), "--output", str(path)],
                        input=answers, text=True, capture_output=True, timeout=5,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertFalse(path.parent.exists())

    def test_interactive_token_is_hidden_not_echoed_or_in_profile(self):
        answers = iter((OWNER, REPO, "yes", "pilot.yaml", "dev/pilot", SHA, "demo", "83", "84"))
        logs = []
        hidden = MagicMock(return_value=SECRET)
        lookup = MagicMock(return_value=(81, 82))
        profile = setup.prompt_profile(lambda _: next(answers), logs.append, hidden, lookup)
        lookup.assert_called_once_with(OWNER, REPO, "pilot.yaml", SECRET)
        self.assertIn("hidden", hidden.call_args.args[0])
        self.assertTrue(any(setup.PAT_SETTINGS in line for line in logs))
        self.assertNotIn(SECRET, "\n".join(logs) + json.dumps(profile))
        self.assertEqual(profile["workflow_id"], 82)

    def test_hidden_input_never_falls_back_to_visible_echo(self):
        with patch.object(setup.sys.stdin, "isatty", return_value=False), patch.object(setup.getpass, "getpass") as get:
            with self.assertRaisesRegex(ValueError, "manual ID"):
                setup.read_secret("Token: ")
            get.assert_not_called()

    def test_lookup_matches_private_org_and_requested_workflow(self):
        base = f"/repos/{OWNER}/{REPO}"
        repo = {"owner": {"login": OWNER, "type": "Organization"}, "full_name": f"{OWNER}/{REPO}",
                "private": True, "id": 81}
        workflow = {"path": ".github/workflows/pilot.yaml", "id": 82}
        get = MagicMock(side_effect=[repo, workflow])
        self.assertEqual(setup.lookup_ids(OWNER, REPO, "pilot.yaml", SECRET, get), (81, 82))
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [base, base + "/actions/workflows/pilot.yaml"])
        for wrong in (dict(repo, private=False), dict(repo, full_name=f"{OWNER}/other"),
                      dict(repo, owner={"login": OWNER, "type": "User"})):
            with self.subTest(wrong=wrong):
                get = MagicMock(return_value=wrong)
                with self.assertRaisesRegex(ValueError, "private organization repository"):
                    setup.lookup_ids(OWNER, REPO, "pilot.yaml", SECRET, get)
                self.assertEqual(get.call_count, 1)
        with self.assertRaisesRegex(ValueError, "requested workflow"):
            setup.lookup_ids(OWNER, REPO, "83", SECRET, MagicMock(side_effect=[repo, workflow]))

    def test_invalid_input_never_reaches_network(self):
        for owner, repo, selector, token in (
            ("https://example.test", REPO, "pilot.yaml", SECRET),
            (OWNER, "../../other", "pilot.yaml", SECRET),
            (OWNER, REPO, "pilot.yaml?to=other", SECRET),
            (OWNER, REPO, "pilot.yaml", SECRET + "\r\nHeader: x"),
        ):
            get = MagicMock()
            with self.subTest(owner=owner, repo=repo, selector=selector), self.assertRaises(ValueError):
                setup.lookup_ids(owner, repo, selector, token, get)
            get.assert_not_called()

    def test_https_get_uses_fixed_host_and_does_not_follow_redirects(self):
        fake = MagicMock()
        reply = fake.getresponse.return_value.__enter__.return_value
        reply.status = 302
        reply.getheader.return_value = "https://untrusted.example/"
        with patch.object(setup.http.client, "HTTPSConnection", return_value=fake) as connect:
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                setup.github_get("/repos/example/demo", SECRET)
            self.assertEqual(connect.call_args.args, ("api.github.com",))
            self.assertEqual(fake.request.call_count, 1)
            self.assertEqual(fake.request.call_args.args, ("GET", "/repos/example/demo"))
            reply.read.assert_not_called()
            fake.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
