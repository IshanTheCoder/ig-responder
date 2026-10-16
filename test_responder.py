import json, os, shutil, tempfile, unittest
from datetime import datetime, timedelta, timezone
import responder as R
os.environ["RULES_JSON"] = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.example.json")).read()

SELF = "ishanexplanations"
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

class FakeIG:
    """Pretend Instagram. Can be told to fail specific comments."""
    def __init__(self, comments):
        self.comments = list(comments); self.dm = {}; self.script = {}; self.n = 0; self.calls = 0
    def get_comments(self, media_id): return list(self.comments)
    def private_reply(self, cid, text):
        self.calls += 1
        if self.script.get(cid):
            e = self.script[cid].pop(0)
            if e: return False, e
        if cid in self.dm: return False, "{'message': 'The comment you are trying to reply to, already has a reply.', 'error_subcode': 2534023}"
        self.dm[cid] = text; return True, None
    def public_reply(self, cid, text):
        self.n += 1
        self.comments.append({"id": f"r{self.n}", "parent_id": cid, "username": SELF, "text": text}); return None

def c(i, text="vocab", user=None): return {"id": str(i), "text": text, "username": user or f"user{i}"}
TRANSIENT = "API returned status 500: {'message': 'An unexpected error has occurred.', 'is_transient': True}"
HARD = "API returned status 400: {'message': 'thread owner has archived or deleted this conversation', 'error_subcode': 2534001}"

class T(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.clock = [NOW]
    def tearDown(self): shutil.rmtree(self.d)
    def engine(self, ig, **kw):
        return R.Engine(ig, state_dir=self.d, sleep=lambda s: None, clock=lambda: self.clock[0], force=True, **kw)
    def rule(self): return next(r for r in R.Engine(FakeIG([]), state_dir=self.d).rules if r["name"] == "vocab")
    def pubs(self, ig, cid): return [x["text"] for x in ig.comments if x.get("parent_id") == cid]

    def test_basic_and_idempotent(self):
        ig = FakeIG([c(1), c(2, "hello")]); e = self.engine(ig)
        self.assertEqual(e.run_rule(self.rule())["sent"], 1)
        self.assertIn("1", ig.dm); self.assertNotIn("2", ig.dm)
        self.assertEqual(e.run_rule(self.rule())["sent"], 0)
        self.assertEqual(len(self.pubs(ig, "1")), 1)

    def test_two_overlapping_runs_never_double_send(self):
        ig = FakeIG([c(i) for i in range(20)])
        d2 = tempfile.mkdtemp()
        a = self.engine(ig); b = R.Engine(ig, state_dir=d2, sleep=lambda s: None, clock=lambda: NOW, force=True)
        a.run_rule(self.rule()); b.run_rule(self.rule()); shutil.rmtree(d2)
        self.assertEqual(len(ig.dm), 20)
        for i in range(20): self.assertEqual(len(self.pubs(ig, str(i))), 1)

    def test_transient_error_retries_without_public_reply(self):
        ig = FakeIG([c(1)]); ig.script["1"] = [TRANSIENT]; e = self.engine(ig)
        s = e.run_rule(self.rule()); self.assertEqual(s["retry_later"], 1); self.assertEqual(self.pubs(ig, "1"), [])
        self.assertEqual(e.run_rule(self.rule())["sent"], 1); self.assertIn("1", ig.dm)

    def test_already_has_dm_gets_sent_reply_once_never_dm_first(self):
        ig = FakeIG([c(1)]); ig.dm["1"] = "x"; e = self.engine(ig); s = e.run_rule(self.rule())
        self.assertEqual(s["already"], 1); self.assertNotIn(R.ASK_DM_FIRST_REPLY, self.pubs(ig, "1"))
        self.assertEqual(len(self.pubs(ig, "1")), 1)
        before = ig.calls; e.run_rule(self.rule()); self.assertEqual(ig.calls, before)  # not re-checked

    def test_hard_block_gets_one_fallback_and_is_not_retried(self):
        ig = FakeIG([c(1)]); ig.script["1"] = [HARD]; e = self.engine(ig)
        self.assertEqual(e.run_rule(self.rule())["blocked"], 1)
        self.assertEqual(self.pubs(ig, "1"), [R.ASK_DM_FIRST_REPLY])
        before = ig.calls; e.run_rule(self.rule()); self.assertEqual(ig.calls, before)

    def test_commenter_with_any_reply_is_left_alone(self):
        ig = FakeIG([c(1), {"id": "x", "parent_id": "1", "username": SELF, "text": R.ASK_DM_FIRST_REPLY}])
        s = self.engine(ig).run_rule(self.rule()); self.assertEqual(s["sent"], 0); self.assertEqual(ig.calls, 0)

    def test_burst_of_500_in_one_run(self):
        ig = FakeIG([c(i) for i in range(500)]); s = self.engine(ig).run_rule(self.rule())
        self.assertEqual(s["sent"], 500)

    def test_hourly_cap_leaves_rest_for_next_cycle(self):
        ig = FakeIG([c(i) for i in range(30)]); s = self.engine(ig, max_per_hour=10).run_rule(self.rule())
        self.assertEqual((s["sent"], s["retry_later"]), (10, 20))

    def test_skips_own_comments_and_replies(self):
        ig = FakeIG([c(1, "vocab", SELF), {"id": "9", "parent_id": "1", "text": "vocab", "username": "bob"}])
        self.assertEqual(self.engine(ig).run_rule(self.rule())["sent"], 0)

    def test_keyword_matching(self):
        for t in ["Vocab", "VOCAB!!", "vocabulary pls", "vocabs", "voacb", "send me the vocab 🙏"]:
            self.assertTrue(R.matches(t, "vocab"), t)
        for t in ["hello", "vocal", "coach"]:
            self.assertFalse(R.matches(t, "vocab"), t)

    def test_schedule_phases(self):
        self.assertEqual([R.interval_minutes(h) for h in (1, 9.9, 10, 47, 48, 167, 168)], [5, 5, 15, 15, 60, 60, None])

    def test_scheduled_run_uses_whole_hourly_budget(self):
        ig = FakeIG([c(i) for i in range(700)])
        e = R.Engine(ig, state_dir=self.d, sleep=lambda s: None, clock=lambda: NOW, slot=True, max_per_hour=600)
        s = e.run_rule(self.rule()); self.assertEqual((s["sent"], s["retry_later"]), (600, 100))

    def test_not_due_until_interval_passes(self):
        ig = FakeIG([c(1)]); e = R.Engine(ig, state_dir=self.d, sleep=lambda s: None, clock=lambda: self.clock[0])
        r = dict(self.rule(), posted_at=(NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(e.run_rule(r)["sent"], 1)
        self.clock[0] = NOW + timedelta(minutes=2); self.assertIn("skipped", e.run_rule(r))
        self.clock[0] = NOW + timedelta(minutes=6); self.assertNotIn("skipped", e.run_rule(r))

class Slot(unittest.TestCase):
    def test_scheduled_run_never_skips_at_odd_times(self):
        rule = next(r for r in R.Engine(FakeIG([]), state_dir=tempfile.mkdtemp()).rules if r["name"] == "vocab")
        rule = dict(rule, posted_at=(NOW - timedelta(hours=20)).isoformat())
        for minute in (0, 3, 7, 13, 41):  # GitHub fires the cron at arbitrary minutes
            t = datetime(2026, 10, 14, 12, minute, tzinfo=timezone.utc)
            e = R.Engine(FakeIG([]), state_dir=tempfile.mkdtemp(), clock=lambda t=t: t, slot=True)
            self.assertNotIn("skipped", e.run_rule(rule))

class Composio(unittest.TestCase):
    def test_discovers_account_when_ids_missing(self):
        import io
        b = R.ComposioBackend("k"); seen = []
        class Resp(io.BytesIO):
            def __enter__(s): return s
            def __exit__(s, *a): pass
        def fake_open(req, timeout=0):
            seen.append(req.full_url)
            body = {"items": [{"id": "ca_1", "user_id": "u_9"}]} if "connected_accounts" in req.full_url else {"successful": True, "data": {}}
            return Resp(json.dumps(body).encode())
        b.u = type("U", (), {"Request": staticmethod(__import__("urllib.request").request.Request), "urlopen": staticmethod(fake_open)})
        b.public_reply("1", "x")
        self.assertEqual(b.extra, {"connected_account_id": "ca_1", "user_id": "u_9"})
        self.assertEqual(len(seen), 2)

    def backend(self, replies):
        b = R.ComposioBackend("k"); b.calls = []
        def fake(url, body): b.calls.append((url, body)); return replies.pop(0)
        b._post = fake; return b
    def test_private_reply_ok_and_error(self):
        b = self.backend([({"status": 200, "data": {"message_id": "m"}}, None),
                          ({"status": 500, "data": {"error": {"message": "already has a reply"}}}, None)])
        self.assertEqual(b.private_reply("1", "hi"), (True, None))
        ok, err = b.private_reply("2", "hi"); self.assertFalse(ok); self.assertEqual(R.classify_error(err), "already")
        self.assertTrue(b.calls[0][0].endswith("/proxy"))
    def test_comments_paginate_and_tool_failure(self):
        b = self.backend([({"successful": True, "data": {"data": [{"id": "1"}], "paging": {"cursors": {"after": "x"}}}}, None),
                          ({"successful": True, "data": {"data": [{"id": "2"}]}}, None)])
        self.assertEqual([c["id"] for c in b.get_comments("m")], ["1", "2"])
        b = self.backend([({"successful": True, "data": {"data": {"data": [{"id": "9"}]}}}, None)])  # nested shape
        self.assertEqual([c["id"] for c in b.get_comments("m")], ["9"])
        b = self.backend([({"successful": False, "error": "boom"}, None)])
        self.assertEqual(b.public_reply("1", "x"), "boom")

if __name__ == "__main__":
    unittest.main()
