"""DevBot's page search index: the pure parts, then a whole build and search against a
real Postgres (skipped where none is reachable, and where a real index already lives in
it -- the build owns its tables, and a test must not empty somebody's index).

Run with:  cd backend && DATABASE_URL=postgresql://... python -m pytest tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")

import httpx  # noqa: E402

from devbot import knowledge, llm  # noqa: E402


# ── the pure parts ───────────────────────────────────────────────────────────

def test_spaces_are_read_from_whatever_an_admin_typed():
    assert knowledge.parse_spaces("DEVOPS, OPS;~ruth\nDEVOPS  bad key!") == ["DEVOPS", "OPS", "~ruth", "bad"]
    assert knowledge.parse_spaces("") == []


def test_passages_carry_the_title_and_stay_within_size():
    text = "\n\n".join(["Paragraph %d. " % i + "word " * 60 for i in range(12)])
    parts = knowledge.passages("Restoring packages", text, size=500)
    assert len(parts) > 3
    assert all(p.startswith("Restoring packages\n") for p in parts)
    assert all(len(p) <= 500 + len("Restoring packages\n") + 5 for p in parts)
    assert knowledge.passages("Empty page", "") == ["Empty page\n"]


def test_the_search_ranks_by_meaning_on_packed_vectors():
    import array

    a, b, c = [1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 0.0, 1.0]
    vectors = [array.array("b", knowledge.pack(v)) for v in (c, b, a)]
    ranked = knowledge.rank([1.0, 0.0, 0.0], vectors)
    assert [i for _, i in ranked] == [2, 1, 0]
    assert ranked[0][0] == pytest.approx(1.0, abs=0.01)
    assert ranked[-1][0] == pytest.approx(0.0, abs=0.01)


def test_the_model_and_its_prefixes():
    models = ["nomic-ai/nomic-embed-text-v1.5", "intfloat/multilingual-e5-large-instruct", "Qwen/Qwen3-VL-Embedding-8B"]
    assert knowledge.pick_model(models) == "intfloat/multilingual-e5-large-instruct"
    assert knowledge.pick_model(models, "nomic-ai/nomic-embed-text-v1.5") == "nomic-ai/nomic-embed-text-v1.5"
    assert knowledge.pick_model([]) == ""
    instruct = "intfloat/multilingual-e5-large-instruct"
    assert knowledge.as_query(instruct, "x").startswith("Instruct: ") and knowledge.as_query(instruct, "x").endswith("Query: x")
    assert knowledge.as_passage(instruct, "x") == "x"
    assert knowledge.as_query("intfloat/multilingual-e5-large", "x") == "query: x"
    assert knowledge.as_passage("intfloat/multilingual-e5-large", "x") == "passage: x"
    assert knowledge.as_query("nomic-ai/nomic-embed-text-v1.5", "x") == "x"


def test_a_fix_is_kept_only_as_the_review_rewrote_it(monkeypatch):
    answers = iter(['<think>hm</think>{"usable": true, "problem": "Docker could not trust the registry.", '
                    '"fix": "Installed the corporate CA and restarted docker."}'])
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (next(answers), {}))
    item = {"title": "Cannot pull", "text": "x509", "fix_raw": "AGAIN the user, stupid. instaled teh CA"}
    got = knowledge.review_fix("k", "m", item, knowledge._Pacer(lambda s: None))
    assert got == {"usable": True, "problem": "Docker could not trust the registry.",
                   "fix": "Installed the corporate CA and restarted docker.", "held": "",
                   "audience": "anyone"}


def test_a_rewrite_that_adds_a_step_the_note_never_had_is_held_back(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (
        '{"usable": true, "problem": "p", "fix": "Copy the CA to /etc/pki/ca-trust/source/anchors and run update-ca-trust --force."}', {}))
    item = {"title": "Cannot pull", "text": "x509", "fix_raw": "put the CA in /etc/pki/ca-trust and ran update-ca-trust"}
    got = knowledge.review_fix("k", "m", item, knowledge._Pacer(lambda s: None))
    assert got["usable"] is False and got["fix"] == ""
    assert "/etc/pki/ca-trust/source/anchors" in got["held"] and "--force" in got["held"]


def test_rewording_is_not_an_invented_detail():
    original = "instaled teh CA with update-ca-certificates, PVC to 50Gi (oc patch pvc). נוקה ה-cache של NuGet"
    assert knowledge.invented_details("Installed the CA with update-ca-certificates; PVC grown to 50Gi with oc patch pvc. "
                                      "נוקה ה-cache של NuGet בסוכן, אחרי 3 ניסיונות.", original) == []
    assert knowledge.invented_details("Set NODE_EXTRA_CA_CERTS=/ca.pem", original) == ["NODE_EXTRA_CA_CERTS=/ca.pem"]


def test_a_note_with_no_fix_is_held_back(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: ('{"usable": "false", "problem": "p", "fix": "fixed"}', {}))
    got = knowledge.review_fix("k", "m", {"title": "t", "text": "", "fix_raw": "fixed"}, knowledge._Pacer(lambda s: None))
    assert got["usable"] is False and got["fix"] == ""


def test_a_review_that_is_not_json_is_never_kept_unreviewed(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (calls.append(1) or "Sure! Here is the fix.", {}))
    with pytest.raises(RuntimeError):
        knowledge.review_fix("k", "m", {"title": "t", "text": "", "fix_raw": "x"}, knowledge._Pacer(lambda s: None))
    assert len(calls) == 2


def test_the_reviewer_is_the_admins_choice_then_devbots_default(monkeypatch):
    monkeypatch.setenv("DEVBOT_DEFAULT_MODEL", "b")
    assert knowledge.pick_chat_model(["a", "b", "c"], "c") == "c"
    assert knowledge.pick_chat_model(["a", "b", "c"], "gone") == "b"
    assert knowledge.pick_chat_model(["a"], "") == "a"
    assert knowledge.pick_chat_model([], "") == ""


def test_a_stop_ends_a_long_wait_within_seconds(monkeypatch):
    slept = []
    pacer = knowledge._Pacer(slept.append)
    asked = iter([False, False, True])
    monkeypatch.setattr(knowledge, "_stop_requested", lambda: next(asked))
    with pytest.raises(knowledge._Stopped):
        pacer.wait(90)
    assert slept == [2.0, 2.0]  # two slices of two seconds, not ninety


def test_a_lock_whose_pod_is_gone_does_not_block_builds(monkeypatch):
    import time as _time

    class FakeRedis:
        def __init__(self, value):
            self.value = value
        def get(self, key):
            return self.value
        def delete(self, key):
            self.value = None

    old = FakeRedis(str(_time.time() - 7200).encode())  # the six-hour lock of a killed pod
    monkeypatch.setattr(knowledge, "get_redis", lambda: old)
    monkeypatch.setattr(knowledge, "_alive_in_db", lambda: False)
    assert knowledge.running() is False and old.value is None
    fresh = FakeRedis(str(_time.time()).encode())  # a build that has only just taken it
    monkeypatch.setattr(knowledge, "get_redis", lambda: fresh)
    assert knowledge.running() is True
    beating = FakeRedis(str(_time.time() - 7200).encode())  # an old build still beating
    monkeypatch.setattr(knowledge, "get_redis", lambda: beating)
    monkeypatch.setattr(knowledge, "_alive_in_db", lambda: True)
    assert knowledge.running() is True and beating.value is not None


def test_the_pacer_waits_when_the_minute_runs_out():
    slept = []
    pacer = knowledge._Pacer(slept.append)
    pacer.after({"rpm_left": 10, "tpm_left": 5000}, 1000)
    assert slept == []
    pacer.after({"rpm_left": 1}, 10)
    pacer.after({"tpm_left": 500}, 1000)
    # Two waits of 20 seconds, each slept in slices of two so a Stop can end it.
    assert pacer.waited == 40 and sum(slept) == 40 and max(slept) == 2.0


# ── a whole build, against a real Postgres ───────────────────────────────────

class _Redis:
    """Enough of Redis for the build, expiry included: the heartbeat is only proven by
    a lock that WOULD have expired without it."""

    def __init__(self):
        self.data = {}
        self.until = {}

    def _alive(self, key):
        import time as _time

        if key in self.until and self.until[key] <= _time.time():
            self.data.pop(key, None)
            self.until.pop(key, None)
        return key in self.data

    def set(self, key, value, nx=False, ex=None):
        if nx and self._alive(key):
            return False
        self.data[key] = str(value)
        self.until.pop(key, None)
        if ex:
            self.expire(key, ex)
        return True

    def setex(self, key, ttl, value):
        self.set(key, value, ex=ttl)

    def expire(self, key, ttl):
        import time as _time

        if self._alive(key):
            self.until[key] = _time.time() + ttl

    def get(self, key):
        return self.data.get(key) if self._alive(key) else None

    def delete(self, key):
        self.data.pop(key, None)
        self.until.pop(key, None)

    def exists(self, key):
        return self._alive(key)


PAGES = {
    "test-1": {"title": "Build agents and the internal CA", "body": "<p>certificate trust on agents</p>", "version": 1},
    "test-2": {"title": "Rotating tokens", "body": "<p>rotate the token before it expires</p>", "version": 1},
    "test-3": {"title": "Restricted secrets", "body": "<p>production secret rotation</p>", "version": 1, "restricted": True},
}
WORDS = {"certificate": 0, "trust": 0, "ca": 0, "agents": 1, "agent": 1, "rotate": 2, "token": 3, "tokens": 3,
         "secret": 4, "production": 4, "rotating": 2, "rotation": 2}


def _vector(text):
    v = [0.0] * 6
    for w in text.lower().replace(".", " ").split():
        if w in WORDS:
            v[WORDS[w]] += 1.0
    v[5] = 0.05
    return v


def _confluence(token):
    def handler(request):
        path = request.url.path
        if path.startswith("/rest/api/space/"):
            return httpx.Response(200, json={"key": "TST", "name": "Test"})
        if path == "/rest/api/content":
            rows = [{"id": pid, "title": p["title"], "version": {"number": p["version"], "when": "2026-09-01T00:00:00"},
                     "_links": {"webui": f"/pages/viewpage.action?pageId={pid}"}} for pid, p in PAGES.items()]
            return httpx.Response(200, json={"results": rows, "_links": {}})
        pid = path.rsplit("/", 1)[-1]
        if pid not in PAGES:
            return httpx.Response(404, json={})
        if PAGES[pid].get("restricted") and token != "index-token":
            return httpx.Response(403, json={"message": "no"})
        return httpx.Response(200, json={"id": pid, "body": {"storage": {"value": PAGES[pid]["body"]}}})

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"Accept": "application/json"})


@pytest.fixture()
def index(monkeypatch):
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    try:
        knowledge.ensure_tables()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"no database: {exc}")
    from db import execute, query_one

    if (query_one("SELECT COUNT(*) AS n FROM devbot_index_pages WHERE page_id NOT LIKE 'test-%%'") or {}).get("n"):
        pytest.skip("this database holds a real page index; not touching it")
    saved = query_one("SELECT * FROM devbot_index_settings WHERE id = 1")
    embedded = []

    def embed(key, model, texts, timeout=60.0):
        embedded.append(len(texts))
        return [_vector(t) for t in texts], {"rpm_left": 50, "tpm_left": 100000}

    monkeypatch.setenv("DEVBOT_LLM_BASE_URL", "https://gw.test/v1")
    monkeypatch.setenv("DEVBOT_INDEX_MIN_SCORE", "50")
    monkeypatch.setattr(knowledge, "get_redis", lambda: _Redis.shared)
    monkeypatch.setattr(knowledge, "confluence_base", lambda: "https://conf.test")
    monkeypatch.setattr(knowledge, "_confluence", _confluence)
    monkeypatch.setattr(llm, "embed", embed)
    monkeypatch.setattr(llm, "list_models", lambda key, embedding=False: [{"id": "intfloat/multilingual-e5-large"}])
    _Redis.shared = _Redis()
    knowledge._loaded["gen"] = None
    knowledge.save_settings(["TST"], "", True, "test", confluence_token="index-token", gateway_key="hub-key")
    yield embedded
    execute("DELETE FROM devbot_index_pages WHERE page_id LIKE 'test-%%'")
    execute("DELETE FROM devbot_index_runs WHERE trigger = 'test'")
    if saved:
        execute("UPDATE devbot_index_settings SET spaces = %s, confluence_token = %s, gateway_key = %s, model = %s, "
                "enabled = %s WHERE id = 1", [saved["spaces"], saved["confluence_token"], saved["gateway_key"],
                                              saved["model"], saved["enabled"]])
    else:
        execute("DELETE FROM devbot_index_settings WHERE id = 1")
    knowledge._loaded["gen"] = None


def test_a_build_then_a_search_that_only_shows_what_the_person_may_open(index):
    result = knowledge.build("test", sleep=lambda s: None)
    assert result["status"] == "done", result
    assert (result["pages_seen"], result["pages_indexed"], result["pages_failed"]) == (3, 3, 0)
    assert knowledge.ready()

    found = knowledge.search("my build agent does not trust the certificate", limit=3)
    assert found and found[0]["id"] == "test-1" and "certificate" in found[0]["passage"]
    secret = knowledge.search("production secret rotation", limit=3)
    assert secret and secret[0]["id"] == "test-3"
    # The index holds it; the person may not open it, so they never see it.
    assert knowledge.visible_to("person-token", "https://conf.test", secret, "u1", 3) == \
        [r for r in secret if r["id"] != "test-3"]
    assert knowledge.visible_to("", "https://conf.test", found, "u1", 3) == []

    # Nothing changed: nothing is read again.
    assert knowledge.build("test", sleep=lambda s: None)["pages_indexed"] == 0
    # One page edited, one gone: exactly those.
    PAGES["test-2"]["version"] = 2
    gone = PAGES.pop("test-3")
    try:
        again = knowledge.build("test", sleep=lambda s: None)
        assert (again["pages_indexed"], again["pages_removed"]) == (1, 1)
        assert not any(r["id"] == "test-3" for r in knowledge.search("production secret rotation", limit=3))
    finally:
        PAGES["test-3"] = gone
        PAGES["test-2"]["version"] = 1

    status = knowledge.admin_status()
    assert status["pages"] == 2 and status["runs"][0]["status"] == "done"
    assert "index-token" not in str(status) and "hub-key" not in str(status)


def test_a_second_build_while_one_runs_does_not_start(index):
    import time as _time

    _Redis.shared.set(knowledge._LOCK_KEY, str(_time.time()), nx=True)  # taken a moment ago
    assert knowledge.build("test", sleep=lambda s: None)["started"] is False
    assert knowledge.start("test")["started"] is False


def test_a_build_whose_pod_died_is_not_running_for_ever(index):
    from db import execute, query_one

    execute("INSERT INTO devbot_index_runs (trigger, status) VALUES ('test', 'running')")
    orphan = query_one("SELECT MAX(id) AS id FROM devbot_index_runs WHERE trigger = 'test'")["id"]
    assert knowledge.admin_status()["runs"][0]["status"] == "died"
    knowledge.build("test", sleep=lambda s: None)
    row = query_one("SELECT status, finished_at FROM devbot_index_runs WHERE id = %s", [orphan])
    assert row["status"] == "died" and row["finished_at"] is not None


def test_the_heartbeat_keeps_a_live_builds_lock_and_says_it_is_alive(index, monkeypatch):
    import threading
    import time as _time

    from db import execute_returning, query_one
    get_redis = knowledge.get_redis  # the fixture's Redis, which the build uses

    monkeypatch.setattr(knowledge, "_HEARTBEAT", 0.2)
    monkeypatch.setattr(knowledge, "_LOCK_TTL", 1)
    run = execute_returning("INSERT INTO devbot_index_runs (trigger, heartbeat_at) "
                            "VALUES ('test', now() - interval '1 hour') RETURNING id")[0]["id"]
    get_redis().set(knowledge._LOCK_KEY, "tok", ex=1)
    done = threading.Event()
    beat = threading.Thread(target=knowledge._heartbeat, args=(run, "tok", done))
    beat.start()
    _time.sleep(1.6)  # past the lock's own one second: only the heartbeat kept it
    alive = bool(get_redis().exists(knowledge._LOCK_KEY))
    done.set()
    beat.join()
    fresh = query_one("SELECT heartbeat_at > now() - interval '5 seconds' AS ok FROM devbot_index_runs WHERE id = %s", [run])
    get_redis().delete(knowledge._LOCK_KEY)
    assert alive and fresh["ok"]


def test_the_fix_field_is_found_by_its_name_whatever_its_reference():
    fields = [
        {"referenceName": "Custom.9f0e1a2b-3c4d", "name": "Solution", "type": "html"},
        {"referenceName": "Microsoft.VSTS.TCM.SystemInfo", "name": "System Info", "type": "html"},
        {"referenceName": "Microsoft.VSTS.CMMI.RootCause", "name": "Root Cause", "type": "string"},
        {"referenceName": "Microsoft.VSTS.Build.FoundIn", "name": "Found In", "type": "string"},
        {"referenceName": "Custom.FixedInVersion", "name": "Fixed In Version", "type": "string"},
        {"referenceName": "Microsoft.VSTS.Common.ResolvedReason", "name": "Resolved Reason", "type": "string"},
        {"referenceName": "Custom.SolutionDate", "name": "Solution Date", "type": "dateTime"},
        {"referenceName": "Custom.abc", "name": "\u05ea\u05d9\u05e7\u05d5\u05df", "type": "plainText"},
    ]
    # Named like a solution first, root cause after, System Info last; never a build,
    # a version, a reason or a date.
    assert knowledge.resolution_fields(fields) == ["Custom.9f0e1a2b-3c4d", "Custom.abc", "Microsoft.VSTS.CMMI.RootCause",
                                                   "Microsoft.VSTS.TCM.SystemInfo"]
    # The admin's, several in order.
    assert knowledge.resolution_fields(fields, "Custom.9f0e1a2b-3c4d, Microsoft.VSTS.TCM.SystemInfo") == [
        "Custom.9f0e1a2b-3c4d", "Microsoft.VSTS.TCM.SystemInfo"]


def test_the_review_says_who_can_apply_a_fix(monkeypatch):
    monkeypatch.setattr(llm, "complete", lambda *a, **k: (
        '{"usable": true, "problem": "p", "fix": "Freed space on the agent server.", "who": "support"}', {}))
    got = knowledge.review_fix("k", "m", {"title": "Disk", "text": "", "fix_raw": "freed space on the agent server"},
                               knowledge._Pacer(lambda s: None))
    assert got["audience"] == "support"
    monkeypatch.setattr(llm, "complete", lambda *a, **k: ('{"usable": true, "problem": "p", "fix": "Ran helm rollback."}', {}))
    got = knowledge.review_fix("k", "m", {"title": "Helm", "text": "", "fix_raw": "ran helm rollback"},
                               knowledge._Pacer(lambda s: None))
    assert got["audience"] == "anyone"


def test_what_the_hub_knows_goes_to_the_model_with_who_can_apply_it():
    from devbot import grounding

    found = {"pages": [{"title": "Freeing disk", "url": "https://conf/p/1", "space": "OPS", "match": "88%",
                        "passage": "Run  the cleanup job."}],
             "fixes": [{"problem": "Agent out of disk.", "fix": "Freed space on the server.", "match": "91%",
                        "recorded_in": "Bug #7", "who_can_apply": "the support team only"}]}
    text = grounding.for_prompt(found)
    assert "[Freeing disk](https://conf/p/1)" in text and "Run the cleanup job." in text
    assert "Bug #7" in text and "support team only" in text and "/ui/support?new=1" in text
    assert grounding.for_prompt({"pages": [], "fixes": []}) == ""
    assert grounding.summary(found) == "Found 1 page and 1 past fix"

    class Ctx:
        systems = {"confluence": True}
    assert grounding.gather(Ctx(), "thanks!") is None
