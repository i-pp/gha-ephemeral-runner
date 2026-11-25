from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from admission import Admission
from fixture_profile import PROFILE, PROFILE_VALUES
from runner_profile import Profile, load_profile


class ProfileTest(unittest.TestCase):
    def test_load_exact_profile(self):
        with TemporaryDirectory() as directory:
            file = Path(directory) / "profile.json"
            file.write_text(json.dumps(PROFILE_VALUES), encoding="utf-8")
            self.assertEqual(load_profile(file), PROFILE)
        self.assertEqual(Profile.from_json(json.dumps(PROFILE_VALUES)), PROFILE)
        self.assertEqual(Profile(**dict(PROFILE_VALUES, head_sha=PROFILE.head_sha.upper())), PROFILE)

    def test_missing_unknown_and_duplicate_fields(self):
        missing = dict(PROFILE_VALUES)
        del missing["installation_id"]
        duplicate = json.dumps(PROFILE_VALUES)[:-1] + ', "app_id": 303}'
        for raw in (json.dumps(missing), json.dumps(dict(PROFILE_VALUES, token="fake")), duplicate, "[]"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    Profile.from_json(raw)

    def test_invalid_typed_or_route_breaking_fields(self):
        for field, value in (
            ("owner", "../team"), ("repository", ".."), ("repository", "repo?q=1"),
            ("repository_id", True), ("workflow_id", 0), ("app_id", "303"),
            ("installation_id", 2**63), ("branch", "refs/test\n"), ("head_sha", "G" * 40),
            ("runner_label", "bad\tlabel"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    replace(PROFILE, **{field: value})

    def test_state_database_rejects_another_profile(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            with_profile = Admission(path, PROFILE)
            with_profile.db.close()
            with self.assertRaises(ValueError):
                Admission(path, replace(PROFILE, runner_label="different-label"))
            reopened = Admission(path, PROFILE)
            reopened.db.close()
