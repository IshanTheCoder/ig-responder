"""Comment -> DM responder. One engine for every Reel (rules live in rules.json).

How it works, in plain words:
  1. Look at every comment on a Reel.
  2. If a comment says the keyword and we have not answered it yet, DM the person
     the doc (an Instagram "private reply") and reply publicly.
  3. Instagram itself is the record of who is done (our own replies under the comment),
     so two runs at the same time can never double-send.

Run:  python3 responder.py --backend composio        (needs COMPOSIO_API_KEY and RULES_JSON)
      python3 responder.py --backend meta --dry-run  (shows what it WOULD do)
"""
import argparse, json, os, random, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# Reply wording. Several variants so the account does not post the same line hundreds of times.
SENT_REPLIES = [
    "Sent! Check your DMs \U0001F4E9 (if you don't see it, check your message requests)",
    "Just sent it \U0001F4E9 Check your DMs, or your message requests if you don't follow me yet",
    "Done! It's in your DMs \U0001F4E9 (look in message requests if it's not in your inbox)",
    "Sent \U0001F4E9 Check your DMs and message requests",
]
ASK_DM_FIRST_REPLY = "Sorry, can't send it here! Can you DM me first and I'll send it right over?"

MAX_PER_HOUR = 600       # Meta allows 750 private replies/hour on post+Reel comments; stay under it
RUN_BUDGET_SECONDS = 420  # stop a scheduled run cleanly before the workflow timeout


# ---------- small helpers ----------
def now_utc():
    return datetime.now(timezone.utc)

def parse_time(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00"))

def interval_minutes(age_hours):
    """How often to check a Reel, by its age. None = stop (private replies expire after 7 days)."""
    if age_hours < 10: return 5
    if age_hours < 48: return 15
    if age_hours < 168: return 60
    return None

def who(c):
    return (c.get("username") or (c.get("from") or {}).get("username") or "").lower()

def _edit1(a, b):
    """Typo tolerance: one missing/extra letter, or two neighbouring letters swapped.
    A single changed letter is NOT allowed, so 'vocal' does not match 'vocab'."""
    if len(a) == len(b):
        return any(a[:i] + a[i+1] + a[i] + a[i+2:] == b for i in range(len(a) - 1))
    if abs(len(a) - len(b)) > 1: return False
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1; j += 1
        else:
            diff += 1
            if diff > 1: return False
            if len(a) > len(b): i += 1
            elif len(a) < len(b): j += 1
            else: i += 1; j += 1
    return diff + (len(a) - i) + (len(b) - j) <= 1

def matches(text, keyword):
    """'vocab', 'Vocab!', 'vocabulary', 'vocabs', 'vocav' all match 'vocab'."""
    text = (text or "").lower()
    if keyword in text:
        return True
    for tok in re.findall(r"[a-z]+", text):
        if len(keyword) >= 5 and tok[:1] == keyword[:1] and _edit1(tok, keyword):
            return True
    return False

def classify_error(err):
    """Turn an Instagram error into one of: already, transient, rate_limit, hard."""
    e = str(err or "").lower()
    if "already has a reply" in e or "2534023" in e: return "already"
    if "unexpected error" in e or "is_transient" in e or "timed out" in e or "timeout" in e: return "transient"
    if "rate limit" in e or "too many" in e or "'code': 4," in e or "'code': 17," in e or "'code': 613" in e: return "rate_limit"
    return "hard"


# ---------- backends: each has get_comments / private_reply / public_reply ----------
class MetaBackend:
    """Talks to Instagram directly (graph.instagram.com) with your access token."""
    def __init__(self, token, version="v21.0"):
        import urllib.request, urllib.parse, urllib.error
        self.u, self.p, self.ue = urllib.request, urllib.parse, urllib.error
        self.token, self.base = token, f"https://graph.instagram.com/{version}"

    def _call(self, method, path, params=None, body=None):
        params = dict(params or {}); params["access_token"] = self.token
        url = f"{self.base}{path}?{self.p.urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        req = self.u.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.u.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}"), None
        except self.ue.HTTPError as ex:
            return None, f"API returned status {ex.code}: {ex.read().decode(errors='replace')[:400]}"
        except Exception as ex:
            return None, f"timeout/network: {ex}"

    def get_comments(self, media_id):
        items, after = [], None
        for _ in range(60):
            params = {"fields": "id,text,username,timestamp,parent_id", "limit": 100}
            if after: params["after"] = after
            d, err = self._call("GET", f"/{media_id}/comments", params)
            if err: raise RuntimeError("comment fetch failed: " + err[:200])
            items += d.get("data", [])
            after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not d.get("data"): break
        return items

    def private_reply(self, comment_id, text):
        d, err = self._call("POST", "/me/messages", body={"recipient": {"comment_id": comment_id}, "message": {"text": text}})
        if err or not (d or {}).get("message_id"): return False, err or str(d)
        return True, None

    def public_reply(self, comment_id, text):
        d, err = self._call("POST", f"/{comment_id}/replies", params={"message": text})
        return err


class ComposioBackend:
    """Uses your existing Composio Instagram connection over its web API (no Meta app needed).
    Needs COMPOSIO_API_KEY. Optional: COMPOSIO_CONNECTED_ACCOUNT_ID, COMPOSIO_USER_ID."""
    BASE = "https://backend.composio.dev/api/v3.1/tools/execute"

    def __init__(self, api_key, account_id=None, user_id=None):
        import urllib.request, urllib.error
        self.u, self.ue, self.key = urllib.request, urllib.error, api_key
        self.extra = {k: v for k, v in (("connected_account_id", account_id), ("user_id", user_id)) if v}

    def _discover(self):
        """No account/user id given: ask Composio which Instagram connection this key owns."""
        req = self.u.Request("https://backend.composio.dev/api/v3/connected_accounts?toolkit_slugs=instagram&statuses=ACTIVE",
                             headers={"x-api-key": self.key})
        try:
            with self.u.urlopen(req, timeout=30) as r:
                items = json.loads(r.read() or b"{}").get("items") or []
        except Exception as ex:
            raise RuntimeError(f"could not list Composio connections: {str(ex)[:150]}")
        if not items:
            raise RuntimeError("this Composio API key has no active Instagram connection (wrong project?)")
        a = items[0]
        self.extra = {k: v for k, v in (("connected_account_id", a.get("id")), ("user_id", a.get("user_id"))) if v}

    def _post(self, url, body):
        if not self.extra: self._discover()
        req = self.u.Request(url, data=json.dumps(dict(body, **self.extra)).encode(), method="POST",
                             headers={"x-api-key": self.key, "Content-Type": "application/json"})
        try:
            with self.u.urlopen(req, timeout=40) as r:
                return json.loads(r.read() or b"{}"), None
        except self.ue.HTTPError as ex:
            return None, f"API returned status {ex.code}: {ex.read().decode(errors='replace')[:400]}"
        except Exception as ex:
            return None, f"timeout/network: {ex}"

    def _tool(self, slug, args):
        d, err = self._post(f"{self.BASE}/{slug}", {"arguments": args})
        if err: return None, err
        if not d.get("successful"): return None, str(d.get("error") or d)[:400]
        return d.get("data") or {}, None

    def get_comments(self, media_id):
        items, after = [], None
        for _ in range(60):
            args = {"ig_media_id": media_id, "limit": 100, "fields": "id,text,username,timestamp,parent_id"}
            if after: args["after"] = after
            d, err = self._tool("INSTAGRAM_GET_IG_MEDIA_COMMENTS", args)
            if err: raise RuntimeError("comment fetch failed: " + err[:200])
            page = d.get("data") if isinstance(d.get("data"), dict) else d  # Composio sometimes nests one level deeper
            items += page.get("data") or []
            after = ((page.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not page.get("data"): break
        return items

    def private_reply(self, comment_id, text):
        d, err = self._post(f"{self.BASE}/proxy", {"method": "POST", "endpoint": "/v21.0/me/messages",
                            "body": {"recipient": {"comment_id": comment_id}, "message": {"text": text}}})
        if err: return False, err
        status, data = d.get("status", 200), d.get("data")
        if status >= 400 or not (data or {}).get("message_id"):
            return False, f"API returned status {status}: {data}"
        return True, None

    def public_reply(self, comment_id, text):
        _, err = self._tool("INSTAGRAM_POST_IG_COMMENT_REPLIES", {"ig_comment_id": comment_id, "message": text})
        return err


class WorkbenchBackend:
    """Runs inside the Composio workbench, using its helper functions (kept so it still works there)."""
    def __init__(self, run_composio_tool, proxy_execute):
        self.run, self.proxy = run_composio_tool, proxy_execute

    def get_comments(self, media_id):
        items, after = [], None
        for _ in range(60):
            args = {"ig_media_id": media_id, "limit": 100, "fields": "id,text,username,timestamp,parent_id"}
            if after: args["after"] = after
            res, err = self.run("INSTAGRAM_GET_IG_MEDIA_COMMENTS", args)
            if err: raise RuntimeError("comment fetch failed: " + str(err)[:200])
            d = (res or {}).get("data") or {}
            items += d.get("data") or []
            after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            if not after or not d.get("data"): break
        return items

    def private_reply(self, comment_id, text):
        res, err = self.proxy(method="POST", endpoint="/v21.0/me/messages", toolkit="instagram",
                              body={"recipient": {"comment_id": comment_id}, "message": {"text": text}})
        if err or not (res or {}).get("message_id"): return False, err or str(res)
        return True, None

    def public_reply(self, comment_id, text):
        _, err = self.run("INSTAGRAM_POST_IG_COMMENT_REPLIES", {"ig_comment_id": comment_id, "message": text})
        return err


# ---------- the engine ----------
class Engine:
    def __init__(self, backend, rules_path=None, state_dir=None, dry_run=False, force=False,
                 sleep=time.sleep, clock=now_utc, max_per_hour=MAX_PER_HOUR, slot=False, workers=8, budget=None):
        raw = os.environ.get("RULES_JSON")
        if raw and not rules_path:
            cfg = json.loads(raw)                      # normal case: rules live in a GitHub secret, never in the repo
        else:
            with open(rules_path or os.path.join(HERE, "rules.json")) as f: cfg = json.load(f)
        self.self_user = cfg["self_username"].lower()
        self.rules = cfg["rules"]
        self.state_dir = state_dir or os.path.join(HERE, "state")
        self.b, self.dry, self.force, self.sleep, self.clock, self.max_per_hour, self.slot = backend, dry_run, force, sleep, clock, max_per_hour, slot
        self.workers = workers
        self.budget = budget or (RUN_BUDGET_SECONDS if slot else 900)
        os.makedirs(self.state_dir, exist_ok=True)

    # state is only a cache + audit log; Instagram's reply list is the real record
    def _state_path(self, name): return os.path.join(self.state_dir, f"{name}.json")

    def load_state(self, name):
        p = self._state_path(name)
        if os.path.exists(p):
            with open(p) as f: return json.load(f)
        return {"last_run": 0, "sent_times": [], "dm_ok": [], "blocked": [], "log": []}

    def save_state(self, name, st):
        st["log"] = st["log"][-300:]
        st["sent_times"] = st["sent_times"][-MAX_PER_HOUR * 2:]
        with open(self._state_path(name), "w") as f: json.dump(st, f, indent=1)

    def due(self, rule, st):
        age_h = (self.clock() - parse_time(rule["posted_at"])).total_seconds() / 3600
        iv = interval_minutes(age_h)
        if iv is None: return False, "past 7 days"
        if self.force: return True, ""
        if self.slot:  # scheduled run: GitHub fires the cron at unpredictable times, so never skip a tick
            return True, ""
        waited = (self.clock().timestamp() - st["last_run"]) / 60
        return (waited >= iv - 1), f"next check in {max(0, round(iv - waited))} min"

    def run_rule(self, rule):
        name, kw = rule["name"], rule["keyword"].lower()
        st = self.load_state(name)
        ok, why = self.due(rule, st)
        if not ok:
            return {"rule": name, "skipped": why}
        t0, stats = time.time(), {"rule": name, "sent": 0, "already": 0, "retry_later": 0, "blocked": 0, "dry": 0}
        comments = self.b.get_comments(rule["media_id"])

        # what has our account already said under each comment? (this is the source of truth)
        ours = {}
        for c in comments:
            if c.get("parent_id") and who(c) == self.self_user:
                ours.setdefault(c["parent_id"], []).append(c.get("text") or "")

        hour_ago = self.clock().timestamp() - 3600
        st["sent_times"] = [t for t in st["sent_times"] if t > hour_ago]

        todo = []
        for c in comments:
            if c.get("parent_id") or who(c) == self.self_user or not matches(c.get("text"), kw):
                continue
            cid = c["id"]
            replies = ours.get(cid, [])
            if replies:
                continue                                   # any reply from us = done (Instagram is the record)
            todo.append((c, replies))

        if self.dry:
            stats["dry"] = len(todo)
            return stats

        cap = max(0, self.max_per_hour - len(st["sent_times"]))   # a rate-limit error from Instagram stops the run cleanly
        batch, stats["retry_later"] = todo[:cap], max(0, len(todo) - cap)

        lock, stop = threading.Lock(), threading.Event()

        def work(item):
            c, replies = item
            if stop.is_set() or time.time() - t0 > self.budget:
                with lock: stats["retry_later"] += 1       # out of time: finish next cycle
                return
            cid = c["id"]
            ok_dm, err = self.b.private_reply(cid, rule["dm_text"])
            kind = "ok" if ok_dm else classify_error(err)
            entry = {"ts": time.time(), "comment_id": cid, "username": who(c), "result": kind,
                     "error": None if ok_dm else str(err)[:200]}
            pub = None
            if kind in ("ok", "already"):
                pub = random.choice(SENT_REPLIES)          # they have the DM, so "Sent!" is true; it also records them as done
            elif kind == "hard":
                pub = ASK_DM_FIRST_REPLY
            if pub: self.b.public_reply(cid, pub)
            with lock:
                if kind == "ok":
                    st["sent_times"].append(time.time()); st["dm_ok"].append(cid); stats["sent"] += 1
                elif kind == "already":
                    st["dm_ok"].append(cid); stats["already"] += 1   # they HAVE the DM; no "DM me first" reply
                elif kind in ("transient", "rate_limit"):
                    stats["retry_later"] += 1                         # no reply, try again next cycle
                    if kind == "rate_limit": stop.set()
                else:
                    st["blocked"].append(cid); stats["blocked"] += 1
                st["log"].append(entry)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(work, batch))

        st["last_run"] = self.clock().timestamp()
        self.save_state(name, st)
        return stats

    def run_all(self):
        return [self.run_rule(r) for r in self.rules]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["meta", "composio"], default="composio")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--slot", action="store_true", help="for a 5-minute cron: apply the phase-down by clock time")
    ap.add_argument("--force", action="store_true", help="ignore the schedule and check now")
    a = ap.parse_args()
    if a.backend == "composio":
        key = os.environ.get("COMPOSIO_API_KEY")
        if not key: sys.exit("Set COMPOSIO_API_KEY (see automation/README.md)")
        backend = ComposioBackend(key, os.environ.get("COMPOSIO_CONNECTED_ACCOUNT_ID"), os.environ.get("COMPOSIO_USER_ID"))
    else:
        token = os.environ.get("IG_ACCESS_TOKEN")
        if not token: sys.exit("Set IG_ACCESS_TOKEN (see automation/README.md)")
        backend = MetaBackend(token)
    for s in Engine(backend, dry_run=a.dry_run, force=a.force, slot=a.slot).run_all():
        print(json.dumps(s))

if __name__ == "__main__":
    main()
