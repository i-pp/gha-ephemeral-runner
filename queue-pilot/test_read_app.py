from datetime import datetime, timezone
import json
import unittest

from fixture_profile import PROFILE
from github_queue import repo_path
from read_app import REPOS, Reader

BASE = repo_path(PROFILE)
INSTALL = f"/app/installations/{PROFILE.installation_id}"

NOW=1735732800
class ReadApp(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        self.buffers=[]
        self.perms={"actions":"read"}
        self.account={"id":24,"login":PROFILE.owner}
        self.repo={"id":PROFILE.repository_id,"full_name":PROFILE.full_name,"private":True,"owner":self.account}
        self.install={"id":PROFILE.installation_id,"app_id":PROFILE.app_id,
                      "target_type":"Organization","repository_selection":"selected",
                      "suspended_at":None,"account":self.account,"target_id":24}
        self.jwt=bytearray(b"F"*120)
        self.actor=Reader(PROFILE,self.http,lambda _:self.jwt,lambda:NOW)
    def http(self,method,path,key,body,cap,timeout):
        self.calls.append((method,path,body))
        rows = {INSTALL:(200,self.install),INSTALL+"/access_tokens":(201,
                {"repository_selection":"selected","permissions":self.perms,
                 "token":"SYNTHETIC-TOKEN-NO-GITHUB-REQUEST",
                 "expires_at":datetime.fromtimestamp(NOW+300,timezone.utc).isoformat()}),
                REPOS:(200,{"total_count":1,"repositories":[self.repo]}), BASE:(200,self.repo)}
        s,d = rows[path]
        b=bytearray(json.dumps(d).encode())
        self.buffers.append(b)
        return s,b
    def test_exact_narrowed_token_and_get_only(self):
        self.assertEqual(self.actor.get(BASE),self.repo)
        self.actor.get(BASE)
        self.assertEqual([(m,p) for m,p,_ in self.calls],[
            ("GET",INSTALL),("POST",INSTALL+"/access_tokens"),("GET",REPOS),("GET",BASE),("GET",BASE)])
        self.assertEqual(json.loads(self.calls[1][2]),{"repository_ids":[PROFILE.repository_id],"permissions":{"actions":"read"}})
        self.assertFalse(any(self.jwt))
        self.assertTrue(all(not any(b) for b in self.buffers))
        t=self.actor.token
        self.actor.close()
        self.assertFalse(any(t))
    def test_cannot_dispatch_or_list_unrelated_repos(self):
        for p in (BASE+"/actions/workflows/1/dispatches","/repos/other/repo",BASE+"/actions/runs/1?x=y",
                  BASE+"/actions/runs/9999999999999999999"):
            with self.assertRaises(ValueError):self.actor.get(p)
        self.assertEqual(self.calls,[])
    def test_wrong_tenant_or_write_token_rejected(self):
        self.install["target_id"]=25
        with self.assertRaises(ValueError):self.actor.get(BASE)
        self.assertEqual(len(self.calls),1)
        self.install["target_id"]=24
        self.perms["actions"]="write"
        with self.assertRaises(ValueError):self.actor.get(BASE)
        self.assertEqual(len(self.calls),3)
        self.assertIsNone(self.actor.token)

    def test_wrong_install_identity_rejected_before_mint(self):
        for key, value in (
            ("account", dict(self.account, login="different-org")),
            ("app_id", 999), ("id", 999), ("suspended_at", "2025-01-01"),
            ("target_type", "User"), ("repository_selection", "all"),
        ):
            with self.subTest(key=key):
                original = self.install[key]
                self.install[key] = value
                self.calls.clear()
                self.jwt[:] = b"F" * 120
                with self.assertRaises(ValueError):
                    self.actor.get(BASE)
                self.assertEqual([(m, route) for m, route, _ in self.calls], [("GET", INSTALL)])
                self.assertFalse(any(self.jwt))
                self.assertIsNone(self.actor.token)
                self.install[key] = original

    def test_public_or_transferred_repo_rejected_before_queue_read(self):
        for key, value in (
            ("private", False), ("id", 999), ("owner", dict(self.account, id=999)),
            ("full_name", "different-org/demo-project"),
        ):
            with self.subTest(key=key):
                original = self.repo[key]
                self.repo[key] = value
                self.calls.clear()
                self.jwt[:] = b"F" * 120
                with self.assertRaises(ValueError):
                    self.actor.get(BASE)
                self.assertEqual([route for _, route, _ in self.calls],
                                 [INSTALL, INSTALL + "/access_tokens", REPOS])
                self.assertIsNone(self.actor.token)
                self.repo[key] = original
