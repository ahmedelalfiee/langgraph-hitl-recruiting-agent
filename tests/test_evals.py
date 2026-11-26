"""The eval scorers are code too, so they get tests: a bug here would hide real failures."""
from evals.run import load, run_suite
from evals.score import _times, score_confirm, score_draft, score_triage
from opzy.config import load_business

BIZ = load_business()
SIG = BIZ.signature
SLOTS = [{"label": "Tuesday 24 November at 15:00"}, {"label": "Wednesday 25 November at 11:00"}]
OFFER = {"slots": SLOTS, "expect": "offer"}

GOOD = ("Hi Omar,\n\nThanks for applying for the Senior Frontend Developer role. Your React and "
        "TypeScript background sounds like a great fit. Could you do a 30-minute video call on Tuesday "
        "24 November at 15:00? If that doesn't suit, Wednesday 25 November at 11:00 works too. Once you "
        "confirm, I'll send a Google Meet link and a calendar invite. Looking forward to talking, and "
        "let me know if you have any questions before then.\n\n" + SIG.strip())


def test_times_are_read_in_common_formats():
    assert _times("at 15:00, or 3pm, or 11.30, or 9 a.m.") == {900, 690, 540}


def test_good_draft_passes_every_check():
    assert all(score_draft(OFFER, GOOD, SIG).values()), score_draft(OFFER, GOOD, SIG)


def test_invented_time_is_caught():
    bad = GOOD.replace("11:00", "16:30")
    assert score_draft(OFFER, bad, SIG)["no_invented_times"] is False


def test_invented_day_is_caught():
    bad = GOOD.replace("Wednesday 25 November", "Thursday 26 November")
    assert score_draft(OFFER, bad, SIG)["no_invented_days"] is False


def test_question_reply_must_not_offer_a_slot():
    case = {"slots": [], "expect": "answer"}
    text = "Hi Omar,\n\nIt's hybrid. Want to chat on Friday at 10:00?\n\n" + SIG.strip()
    assert score_draft(case, text, SIG)["no_slot_offered"] is False


def test_missing_signature_and_em_dash_are_caught():
    c = score_draft(OFFER, GOOD.replace(SIG.strip(), "Cheers") + " — Sara", SIG)
    assert c["ends_with_signature"] is False and c["no_em_dash"] is False


def test_triage_scoring_ignores_accents_and_case():
    case = {"kind": "application", "role": "Backend Developer", "name": "Inês"}
    out = {"kind": "application", "role": "backend developer", "candidate_first_name": "Ines"}
    assert all(score_triage(case, out).values())


def test_confirm_checks_the_slot_only_for_a_confirmation():
    case = {"decision": "confirm", "slot_index": 1}
    assert score_confirm(case, {"decision": "confirm", "slot_index": 0}) == {"decision": True, "slot": False}
    assert score_confirm({"decision": "reschedule"}, {"decision": "reschedule"}) == {"decision": True}


class Oracle:
    """Answers with the labels, so a correct pipeline must score 100%."""

    usage = None
    model = "oracle"

    def __init__(self):
        self.t = {c["email"]["subject"] + c["email"]["body"]: c for c in load("triage", None)}
        self.c = {c["body"]: c for c in load("confirm", None)}

    def triage(self, email, biz):
        c = self.t[email["subject"] + email["body"]]
        kind = c["kind"] or c["kind_any"][0]
        role = c["role_any"][0] if "role_any" in c else c["role"]
        return {"kind": kind, "role": role, "candidate_first_name": c.get("name"), "summary": ""}

    def confirm(self, email, slots, biz):
        c = self.c[email["body"]]
        return {"decision": c["decision"], "slot_index": c["slot_index"]}


def test_pipeline_scores_a_perfect_model_at_100():
    for suite in ("triage", "confirm"):
        s = run_suite(suite, Oracle(), BIZ)["summary"]
        assert s["all_checks_pass_pct"] == 100.0, (suite, s["failures"])


def test_a_crashing_model_fails_cases_instead_of_crashing_the_run():
    class Broken(Oracle):
        def confirm(self, *a):
            raise ValueError("no JSON in model output")

    s = run_suite("confirm", Broken(), BIZ, limit=3)["summary"]
    assert s["all_checks_pass_pct"] == 0.0 and len(s["failures"]) == 3


def test_every_case_goes_through_the_real_prompt_code():
    """The Oracle skips prompt building, which once hid a crash: confirm cases stored slots
    as plain strings, and the real prompt expected {"label": ...}. Only the API call is faked here."""
    from opzy.llm import PromptLLM

    class Canned(PromptLLM):
        model = "canned"

        def _ask(self, prompt, max_tokens=600):
            if '"claims"' in prompt:
                return '{"claims": []}'
            if '"decision"' in prompt:
                return '{"decision": "other", "slot_index": null, "summary": ""}'
            if '"kind"' in prompt:
                return '{"kind": "other", "role": null, "candidate_first_name": null, "summary": ""}'
            return "Hi,\\n\\nThanks for your email.\\n\\n" + SIG.strip()

    for suite in ("triage", "confirm", "draft", "judge"):
        rows = run_suite(suite, Canned(), BIZ, judge=Canned())["rows"]
        crashed = [r["id"] for r in rows if r["checks"].get("ran") is False]
        assert not crashed, (suite, crashed[:3], rows[0]["output"])


def test_invented_salary_is_caught():
    """From the first real run: the model told a candidate a salary range that exists nowhere."""
    case = {"slots": [], "expect": "answer", "unknown": True}
    text = ("Hi Inês,\n\nThe salary range is usually between €55,000 and €70,000 gross per year.\n\n"
            + SIG.strip())
    c = score_draft(case, text, SIG)
    assert c["no_invented_money"] is False and c["defers_unknown"] is False


def test_deferring_an_unknown_answer_passes():
    case = {"slots": [], "expect": "answer", "unknown": True}
    text = ("Hi Inês,\n\nThanks for asking. I don't have the salary details to hand, so I'll check "
            "with the team and get back to you this week.\n\n" + SIG.strip())
    c = score_draft(case, text, SIG)
    assert c["no_invented_money"] and c["defers_unknown"]


# ---- the groundedness judge

def test_judge_sees_the_facts_and_flags_claims():
    from evals.judge import judge_draft

    seen = {}

    class Judge:
        def _ask(self, prompt, max_tokens=600):
            seen["prompt"] = prompt
            return ('{"claims": [{"claim": "the range is €55k to €70k", "support": "NONE"},'
                    ' {"claim": "Backend Developer role", "support": "Backend Developer"}]}')

    case = load("draft", None)[8]  # d09, the salary question
    v = judge_draft(Judge(), BIZ, case, "Hi Inês, the range is €55k to €70k." + SIG)
    assert v["grounded"] is False and v["unsupported"] == ["the range is €55k to €70k"]
    p = seen["prompt"]
    assert "Backend Developer" in p and "salary range" in p.lower() and "€55k" in p


def test_judge_cannot_invent_its_own_evidence():
    """A quote that isn't really in the sources doesn't count as support."""
    from evals.judge import judge_draft

    class Liar:
        def _ask(self, prompt, max_tokens=600):
            return '{"claims": [{"claim": "full relocation support", "support": "relocation support offered"}]}'

    case = load("draft", None)[0]
    assert judge_draft(Liar(), BIZ, case, "x")["grounded"] is False


def test_real_quotes_count_as_support_despite_quotes_and_spacing():
    from evals.judge import judge_draft

    class Honest:
        def _ask(self, prompt, max_tokens=600):
            return ('{"claims": [{"claim": "Tuesday at 15:00", "support": "Tuesday 24 November at 15:00"},'
                    ' {"claim": "Meet link after", "support": "Google Meet link sent  after the candidate confirms."}]}')

    case = load("draft", None)[0]
    assert judge_draft(Honest(), BIZ, case, "x")["grounded"] is True


def test_judge_suite_scores_a_judge_that_matches_the_labels_at_100():
    labels = {c["text"]: c["grounded"] for c in load("judge", None)}

    class LabelJudge:
        def _ask(self, prompt, max_tokens=600):
            text = next(t for t in labels if t in prompt)
            return '{"claims": []}' if labels[text] else '{"claims": [{"claim": "x", "support": "NONE"}]}'

    s = run_suite("judge", None, BIZ, judge=LabelJudge())["summary"]
    assert s["all_checks_pass_pct"] == 100.0 and s["cases"] == 16


def test_judge_set_has_both_kinds_of_example():
    rows = load("judge", None)
    drafts = {c["id"] for c in load("draft", None)}
    assert {r["grounded"] for r in rows} == {True, False}
    assert all(r["draft"] in drafts for r in rows)
