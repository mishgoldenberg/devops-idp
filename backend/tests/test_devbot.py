"""DevBot's pure logic: the parts that decide what a person is told and what a model
is sent. No network, no database.

Run with:  cd backend && python -m pytest tests -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

pytest.importorskip("httpx", reason="backend deps not installed")

from devbot import budget, config, llm  # noqa: E402
from devbot.orchestrator import to_model_messages  # noqa: E402


# ── configuration ────────────────────────────────────────────────────────────

def test_an_undefined_pipeline_variable_is_not_a_url(monkeypatch):
    monkeypatch.setenv("DEVBOT_LLM_BASE_URL", "$(DEVBOT_LLM_BASE_URL)")
    assert config.llm_base_url() == ""
    assert not config.enabled()
    monkeypatch.setenv("DEVBOT_LLM_BASE_URL", "models.example/v1/")
    assert config.llm_base_url() == "https://models.example/v1"


# ── gateway failures, in words ───────────────────────────────────────────────

def test_rate_limit_says_how_long_to_wait():
    exc = llm.classify(429, '{"error":{"message":"litellm.RateLimitError: Max rpm reached. Try again in 12 seconds."}}', {})
    assert exc.kind == "limit" and exc.retry_after == 12
    assert "requests per minute" in exc.message and "12 seconds" in exc.message
    assert "litellm" not in exc.message


def test_retry_after_header_wins():
    exc = llm.classify(429, "{}", {"retry-after": "7"})
    assert exc.retry_after == 7


def test_missing_tool_flags_is_its_own_kind():
    body = '{"error":{"message":"\\"auto\\" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set"}}'
    exc = llm.classify(400, body, {}, "Qwen/Qwen2.5-Coder-7B-Instruct")
    assert exc.kind == "tools"


def test_key_problems_are_not_model_problems():
    assert llm.classify(401, '{"error":{"message":"Invalid proxy server token passed"}}', {}).kind == "key"
    denied = llm.classify(401, '{"error":{"message":"key not allowed to access model. This key can only access models=[a]"}}', {}, "b")
    assert denied.kind == "model_denied"
    assert llm.classify(400, '{"error":{"message":"Budget has been exceeded! Current cost: 5"}}', {}).kind == "budget"
    assert llm.classify(400, '{"error":{"message":"This model\'s maximum context length is 8192 tokens"}}', {}).kind == "context"


def test_limits_are_read_from_headers():
    headers = {"x-ratelimit-limit-requests": "20", "x-ratelimit-remaining-requests": "17",
               "x-ratelimit-limit-tokens": "40000", "x-ratelimit-remaining-tokens": "not-a-number"}
    assert llm.read_limits(headers) == {"rpm": 20, "rpm_left": 17, "tpm": 40000}


def test_embedding_models_are_not_chat_models():
    assert llm.is_embedding("nomic-ai/nomic-embed-text-v1.5")
    assert llm.is_embedding("intfloat/multilingual-e5-large-instruct")
    assert llm.is_embedding("Qwen/Qwen3-VL-Embedding-8B")
    assert not llm.is_embedding("openai/gpt-oss-120b")
    assert not llm.is_embedding("anything", mode="chat")
    assert llm.is_embedding("anything", mode="embedding")


def test_think_tags_split_across_chunks():
    s = llm._ThinkSplitter()
    out = []
    for piece in ["<thi", "nk>plan it", " out</th", "ink>\n\nThe ans", "wer."]:
        out += s.feed(piece)
    out += s.flush()
    reasoning = "".join(t for k, t in out if k == "reasoning")
    content = "".join(t for k, t in out if k == "content")
    assert reasoning == "plan it out"
    assert content == "\n\nThe answer."


def test_text_that_only_looks_like_a_tag_start_is_not_lost():
    s = llm._ThinkSplitter()
    out = s.feed("a < b and <th") + s.feed("ings") + s.flush()
    assert "".join(t for _, t in out) == "a < b and <things"


# ── budgets ──────────────────────────────────────────────────────────────────

def test_hebrew_costs_more_tokens_than_english_of_the_same_length():
    assert budget.estimate("שלום " * 40) > budget.estimate("hello " * 40)


def test_a_small_key_makes_small_requests():
    plan = budget.plan(131072, None, {"tpm": 10000, "rpm": 6})
    assert plan.prompt_cap <= 4500
    assert plan.rounds == 3
    assert plan.notes
    roomy = budget.plan(131072, None, {})
    assert roomy.prompt_cap == config.max_prompt_tokens()


def test_never_fewer_than_two_rounds():
    assert budget.plan(32768, None, {"rpm": 1}).rounds == 2


def _turn(question, result_size=0):
    msgs = [{"role": "user", "content": question}]
    if result_size:
        msgs += [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c" + question, "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c" + question, "content": "r" * result_size},
        ]
    msgs.append({"role": "assistant", "content": "answer to " + question})
    return msgs


def test_fit_drops_old_tool_results_before_old_questions():
    system = {"role": "system", "content": "sys"}
    history = _turn("one", 20000) + _turn("two", 20000) + [{"role": "user", "content": "three?"}]
    fitted, dropped = budget.fit([system] + history, 2500)
    assert dropped == 0
    assert [m["content"] for m in fitted if m["role"] == "tool"] == [budget.OMITTED, budget.OMITTED]
    assert fitted[-1]["content"] == "three?"


def test_fit_keeps_the_question_when_everything_else_must_go():
    system = {"role": "system", "content": "sys"}
    history = _turn("a" * 9000) + _turn("b" * 9000) + [{"role": "user", "content": "the question"}]
    fitted, dropped = budget.fit([system] + history, 1500)
    assert dropped > 0
    assert fitted[0]["role"] == "system"
    assert fitted[-1]["content"] == "the question"


# ── replaying a conversation ─────────────────────────────────────────────────

def test_an_unpaired_tool_call_is_not_replayed():
    rows = [
        {"role": "user", "content": "q1", "meta": {}},
        {"role": "assistant", "content": "", "meta": {"internal": True, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "x", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "y", "arguments": "{}"}},
        ]}},
        {"role": "tool", "content": "result a", "meta": {"call_id": "a"}},
        {"role": "assistant", "content": "answer 1", "meta": {}},
        {"role": "user", "content": "q2", "meta": {}},
        {"role": "assistant", "content": "", "meta": {"internal": True, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}},
    ]
    out = to_model_messages(rows)
    calls = [m for m in out if m.get("tool_calls")]
    assert len(calls) == 1 and [c["id"] for c in calls[0]["tool_calls"]] == ["a"]
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "assistant", "user"]


# ── tools ────────────────────────────────────────────────────────────────────

from devbot import tools  # noqa: E402
from devbot.tools import base as tool_base  # noqa: E402
from devbot.tools.confluence import cql_literal, page_id_from, storage_to_text  # noqa: E402

ALL = {"azure": True, "sonarqube": True, "artifactory": True, "confluence": True}


def _names(specs):
    return {s["function"]["name"] for s in specs}


def test_only_the_systems_a_question_names_are_offered():
    names = _names(tools.specs_for("Which SonarQube projects fail their quality gate?", ALL))
    assert names and all(n.startswith("sonar_") for n in names)


def test_hebrew_questions_find_their_system():
    names = _names(tools.specs_for("איזה פייפליין נכשל אצלי היום?", ALL))
    assert "ado_pipeline_runs" in names


def test_a_system_that_is_not_connected_is_never_offered():
    names = _names(tools.specs_for("search confluence and artifactory", {**ALL, "artifactory": False}))
    assert not any(n.startswith("art_") for n in names)
    assert any(n.startswith("conf_") for n in names)


def test_a_question_naming_nothing_gets_the_starting_points():
    names = _names(tools.specs_for("hello there", ALL))
    assert names and len(names) <= tool_base.MAX_OFFERED


def test_a_follow_up_keeps_the_previous_answers_systems():
    names = _names(tools.specs_for("and the second one?", ALL, recent=["confluence"]))
    assert "conf_search" in names


def test_results_shrink_to_fit_without_breaking_json():
    import json
    big = {"rows": [{"title": "x" * 500, "n": i} for i in range(200)]}
    text = tool_base.compact(big, 1500)
    assert len(text) <= 1500
    assert json.loads(text)["rows"][-1].startswith("... and")


def test_confluence_code_blocks_survive_as_code():
    storage = ('<h2>Fix</h2><p>Run &amp; wait:</p><ac:structured-macro ac:name="code"><ac:parameter ac:name="language">bash'
               '</ac:parameter><ac:plain-text-body><![CDATA[kubectl rollout restart deploy/x]]></ac:plain-text-body>'
               '</ac:structured-macro><ul><li>then check</li></ul>')
    text = storage_to_text(storage)
    assert "```bash\nkubectl rollout restart deploy/x\n```" in text
    assert "Run & wait:" in text and "- then check" in text


def test_cql_and_wiql_values_cannot_break_out():
    assert cql_literal('a" OR space = "X') == '"a\\" OR space = \\"X"'
    from devbot.tools.ado import literal
    assert literal("x' OR 1=1") == "'x'' OR 1=1'"


def test_page_links_become_ids():
    assert page_id_from("https://wiki/pages/viewpage.action?pageId=5001") == "5001"
    assert page_id_from("https://wiki/spaces/OPS/pages/5002/Deployment+runbook") == "5002"
    assert page_id_from("77") == "77"


def test_an_answer_with_json_in_it_is_not_a_failed_tool_call():
    names = ["ado_work_items", "conf_search"]
    assert not llm.wrote_tool_call_as_text('Here it is: {"name": "hub", "version": 3}', names)
    assert not llm.wrote_tool_call_as_text('{"name": "hub", "arguments": {}}', names)
    assert llm.wrote_tool_call_as_text('{"name": "conf_search", "arguments": {"query": "x"}}', names)
    assert llm.wrote_tool_call_as_text('```json\n{"name": "ado_work_items", "arguments": {}}\n```', names)
    assert llm.wrote_tool_call_as_text('<tool_call>{"name": "x"}</tool_call>', names)


def test_a_failure_question_gets_the_investigation():
    names = _names(tools.specs_for("What is the error on the latest pipeline?", ALL))
    assert "investigate_pipeline_failure" in names
    names = _names(tools.specs_for("Why is it broken?", ALL))
    assert "investigate_pipeline_failure" in names and "investigate_work_item" in names


# ── investigations ───────────────────────────────────────────────────────────

from devbot.tools import investigate as inv  # noqa: E402


def test_the_error_is_read_out_of_a_noisy_log():
    log = "\n".join(["2026-09-24T08:01:0%dZ   restoring..." % i for i in range(9)] + [
        "2026-09-24T08:01:10Z ##[error]/src/App.csproj : error NU1101: Unable to find package Contoso.Core. No packages exist",
        "2026-09-24T08:01:11Z ##[error]Process completed with exit code 1.",
        "2026-09-24T08:01:12Z   Build succeeded with 0 error(s)",
    ])
    lines = inv.error_lines(log)
    assert any("NU1101" in l for l in lines)
    assert not any(l.startswith("2026-") for l in lines)
    assert not any("0 error(s)" in l for l in lines)


def test_the_specific_error_beats_the_exit_code():
    sig = inv.signature(["Process completed with exit code 1.",
                         "/src/App.csproj : error NU1101: Unable to find package Contoso.Core."])
    assert sig.startswith("error NU1101")
    assert inv.search_terms(sig)[0] == "NU1101 Contoso.Core"


def test_a_missing_package_is_named():
    got = inv.missing_package("error NU1101: Unable to find package Contoso.Core. No packages exist with this id")
    assert got == {"kind": "nuget", "name": "Contoso.Core", "version": ""}
    got = inv.missing_package("Could not find artifact com.contoso:hub-core:jar:2.3.1 in central")
    assert got["kind"] == "maven" and got["name"] == "hub-core" and got["version"] == "2.3.1"
    assert inv.missing_package("everything is fine") is None


def test_test_history_reads_naturally():
    assert inv.history_note(0, 1) == "new: passed in the previous run"
    assert inv.history_note(2, 3) == "also failed in 2 of the 3 previous runs"
    assert inv.history_note(3, 3) == "also failed in the 3 previous runs"


# ── proposals ────────────────────────────────────────────────────────────────

from devbot.markup import markdown_to_storage  # noqa: E402
from devbot.tools import proposals  # noqa: E402


def test_markdown_becomes_confluence_formatting_and_nothing_else():
    fence = chr(96) * 3
    storage = markdown_to_storage("## Fix\n" + fence + "bash\nrm -rf <x> ]]> done\n" + fence + "\n- **run** it <script>")
    assert "<h2>Fix</h2>" in storage
    assert 'ac:name="code"' in storage and "rm -rf <x> ]]]]><![CDATA[> done" in storage
    assert "<li><strong>run</strong> it &lt;script&gt;</li>" in storage
    assert "<script>" not in storage.replace("<![CDATA[", "")


def test_only_http_links_survive():
    assert '<a href="https://x/y">' in markdown_to_storage("[a](https://x/y)")
    assert "href" not in markdown_to_storage("[a](javascript:alert(1))")


def test_a_bug_draft_carries_the_evidence():
    run = {"pipeline": "ci", "run": "7", "branch": "dev", "project": "Hub", "collection": "C", "url": "http://r"}
    a = proposals.bug_for_failure(run, "error NU1101: nope", {"step": "Restore", "lines": ["l1", "l2"]},
                                  {"title": "Fix page", "url": "http://p"})
    assert a["kind"] == "wi-create" and a["suggested"] and a["status"] == "proposed"
    body = a["target"]["description"]
    assert "error NU1101: nope" in body and "l2" in body and "http://r" in body and "http://p" in body


def test_the_model_hears_how_a_proposal_ended():
    rows = [
        {"role": "user", "content": "q", "meta": {}},
        {"role": "assistant", "content": "a", "meta": {"actions": [
            {"title": "Run the pipeline again", "status": "done", "result": "Queued as run 9."},
            {"title": "Open a bug about it", "status": "declined"},
            {"title": "Other", "status": "proposed"}]}},
    ]
    said = to_model_messages(rows)[-1]["content"]
    assert "Run the pipeline again: done -- Queued as run 9." in said
    assert "Open a bug about it: declined by the person" in said
    assert "Other" not in said


def test_a_failed_question_is_not_replayed():
    rows = [
        {"role": "user", "content": "q1", "meta": {}},
        {"role": "assistant", "content": "", "meta": {"error": {"kind": "limit", "message": "wait"}}},
        {"role": "user", "content": "q1", "meta": {}},
    ]
    assert [m["content"] for m in to_model_messages(rows)] == ["q1"]
