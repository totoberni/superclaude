"""Tests for the offline answer generator (`bin/generate_answers.py`).

The tool is a bin/ script, not an engine module, so it is loaded by path. NO test
here spawns a real subprocess: the model call goes through the injected runner
seam, and `subprocess.run` is monkeypatched to explode if anything reaches for it.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from engine.content import load_generated_answers
from engine.kernel.contracts import Field, FieldMap, Locator
from engine.kernel.ssot import SSOT

FIXTURES = Path(__file__).parent / "fixtures" / "content"
TOOL_PATH = Path(__file__).parent.parent / "bin" / "generate_answers.py"
HARNESS_PATH = Path(__file__).parent.parent / "w5_accept.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("generate_answers_tool",
                                                  TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ssot() -> SSOT:
    return SSOT.load(FIXTURES / "ssot-fake.yaml")


def questions_doc() -> dict:
    return {
        "vendor": "greenhouse",
        "slug": "acme",
        "job_id": "12345",
        "posting_lang": "en",
        "questions": [
            {"key": "question_1", "label": "Why do you want to work here?",
             "type": "textarea", "norm_type": "LONGTEXT", "required": True,
             "options": [], "max_length": 800},
            {"key": "question_2", "label": "AI Policy for Application",
             "type": "textarea", "norm_type": "LONGTEXT", "required": True,
             "options": [], "max_length": 400},
        ],
    }


def make_field(key: str, label: str, *, type_: str = "input_text",
               section: str = "STANDARD", decline_allowed: bool = False,
               options: list[str] | None = None, norm_type: str = "",
               max_length: int | None = None) -> Field:
    return Field(key=key, label=label, type=type_, required=False,
                 options=list(options or []), source="test",
                 locator=Locator(role="textbox", name=label),
                 section=section, decline_allowed=decline_allowed,
                 norm_type=norm_type, max_length=max_length)


def test_generator_builds_grounded_prompt_from_fake_ssot(tool, ssot: SSOT) -> None:
    """The prompt carries the SSOT grounding and the anti-fabrication rules, and
    nothing that is not in the SSOT."""
    question = questions_doc()["questions"][0]
    prompt = tool.build_prompt(question, ssot, company="Acme", posting_lang="en",
                               jd_text="We build analytical systems.")

    assert "Why do you want to work here?" in prompt
    assert "Your work on distributed analytical systems" in prompt  # SSOT prose
    assert "Ada Lovelace" in prompt                                  # SSOT identity
    assert "We build analytical systems." in prompt                  # JD context
    assert "ONLY facts present in the grounding block" in prompt
    assert "Do NOT fabricate" in prompt
    assert "British spelling" in prompt
    assert "Stay within 800 characters" in prompt
    assert "Plain text only" in prompt


def test_generator_writes_schema_valid_yaml(tool, ssot: SSOT, tmp_path) -> None:
    """Capture feeds the generator and the output round-trips through the engine's
    own loader: the schema the generator writes IS the schema the overlay reads.
    Capture drops the uploads and the fields policy never auto-answers
    (COMPLIANCE_EEOC / DEMOGRAPHIC / VOLUNTARY, or `decline_allowed`) using the
    KERNEL's predicates, so a demographic question is never sent to a model and
    the generator never hands the overlay an answer the kernel declined on
    purpose."""
    fieldmap = FieldMap(vendor="greenhouse", posting_id="12345",
                        captured_at="2026-07-13T00:00:00Z", fields=[
                            make_field("question_1", "Why do you want to work here?",
                                       type_="textarea", max_length=800),
                            make_field("question_2", "AI Policy for Application",
                                       type_="textarea", max_length=400),
                            make_field("q_cv", "Upload your CV", type_="input_file"),
                            make_field("q_attach", "Attach a cover letter"),
                            make_field("q_gender", "Gender",
                                       section="DEMOGRAPHIC"),
                            make_field("q_veteran", "Veteran status",
                                       section="COMPLIANCE_EEOC"),
                            make_field("q_disability", "Disability status",
                                       section="VOLUNTARY"),
                            make_field("q_race", "Race", decline_allowed=True),
                        ])
    captured = dict(questions_doc(),
                    questions=tool.questions_from_fieldmap(fieldmap))
    assert [question["key"] for question in captured["questions"]] == [
        "question_1", "question_2"]

    doc = tool.generate_answers(captured, ssot, company="Acme",
                                tos_mode="disclose",
                                runner=lambda prompt, model: "A grounded answer.")
    out = tool.write_yaml(doc, tmp_path / "acme-12345.yaml")
    assert oct(out.stat().st_mode)[-3:] == "600"

    generated = load_generated_answers(out)
    assert generated.vendor == "greenhouse"
    assert generated.slug == "acme"
    assert generated.posting_lang == "en"
    disclosure = str(ssot.get("canned_answers.ai_use_disclosure")).strip()
    values = {answer.key: answer.value for answer in generated.answers}
    # tos-mode disclose is not tos-mode allow: the seeded disclosure RIDES WITH the
    # model-written essay (an employer who requires disclosure receives the essay
    # AND the statement that it was AI-refined), and it answers the AI-use question
    # verbatim.
    assert values["question_1"] == f"A grounded answer.\n\n{disclosure}"
    assert values["question_2"] == disclosure
    assert generated.tos_forbidden == []

    # tos-mode forbid-essays hands the free-text questions to the human instead,
    # and lists them by name so the acceptance gate can subtract them.
    forbidding = tool.generate_answers(captured, ssot, company="Acme",
                                       tos_mode="forbid-essays",
                                       runner=lambda prompt, model: "unused")
    assert forbidding["answers"] == []
    assert [entry["label"] for entry in forbidding["tos_forbidden"]] == [
        "Why do you want to work here?", "AI Policy for Application"]


def test_generator_runner_injection_no_real_subprocess(tool, ssot: SSOT,
                                                       monkeypatch) -> None:
    """The model call goes through the injectable seam; nothing shells out."""
    def explode(*args, **kwargs):
        raise AssertionError("the generator must not spawn a subprocess in tests")

    monkeypatch.setattr(tool.subprocess, "run", explode)

    seen: list[tuple[str, str]] = []

    def fake_runner(prompt: str, model: str) -> str:
        seen.append((prompt, model))
        return "Injected answer."

    monkeypatch.setattr(tool, "_RUNNER", fake_runner)

    doc = tool.generate_answers(questions_doc(), ssot, company="Acme",
                                tos_mode="allow")
    # ONE question reaches the model: the essay. The AI-use question does not,
    # not even in `allow` -- it is answered from the seeded disclosure (see
    # test_generator_ai_policy_is_never_asked_of_the_model).
    assert [prompt.splitlines()[2] for prompt, _ in seen] == [
        "Question: Why do you want to work here?"]
    assert all(model == "sonnet" for _, model in seen)
    assert all("GROUNDING" in prompt for prompt, _ in seen)
    values = {answer["label"]: answer["value"] for answer in doc["answers"]}
    assert values["Why do you want to work here?"] == "Injected answer."
    assert values["AI Policy for Application"].startswith(
        "I drafted this application myself")

    # What the injected model returns is checked, never swallowed. An empty
    # answer is a hard error, not a silent `continue`: swallowing it would drop
    # the question from BOTH `answers` and `tos_forbidden`, losing coverage with
    # no per-question record.
    with pytest.raises(RuntimeError, match="empty answer"):
        tool.generate_answers(questions_doc(), ssot, company="Acme",
                              tos_mode="allow",
                              runner=lambda prompt, model: "   ")

    # The posting's own `max_length` is enforced on the model's OUTPUT, not just
    # asserted in the prompt: an over-cap answer would be truncated mid-sentence
    # by the form at fill time. Exactly at the cap is fine: the limit is a limit,
    # not an off-by-one.
    capped = questions_doc()
    capped["questions"] = [capped["questions"][0]]  # max_length: 800
    with pytest.raises(RuntimeError, match="over the posting's own 800-character"):
        tool.generate_answers(capped, ssot, company="Acme", tos_mode="allow",
                              runner=lambda prompt, model: "x" * 801)
    at_cap = tool.generate_answers(capped, ssot, company="Acme", tos_mode="allow",
                                   runner=lambda prompt, model: "x" * 800)
    assert len(at_cap["answers"][0]["value"]) == 800


def test_capture_keeps_only_the_questions_this_tool_answers(tool) -> None:
    """A model is called for FREE-TEXT questions and for nothing else, and capture is
    where that gate starts: a desired-salary input, a relocation dropdown and a
    one-line notice period are the KERNEL resolver's to answer and the content
    overlay's to canned-route. Prose written for one of them would be taken VERBATIM
    into a one-line field by the overlay (a field with no options takes the candidate
    as-is), which is the "nothing is guessed" invariant broken by the one channel
    that exists to keep it.

    Essay-SHAPED, not essay-labelled: the 800-character `input_text` is kept, because
    a vendor rendering its "why us?" box as a plain text input is asking for an essay
    just as much as a textarea is. Same predicate as the overlay's (`is_free_text`).

    The AI-USE question is kept whatever SHAPE it is asked in (here a yes/no): it is
    answered from the seeded disclosure and NEVER by a model, and dropping it here
    would leave an employer's own disclosure question unanswered.
    """
    fieldmap = FieldMap(vendor="greenhouse", posting_id="12345",
                        captured_at="2026-07-13T00:00:00Z", fields=[
                            make_field("question_1", "Why do you want to work here?",
                                       type_="textarea", max_length=800),
                            make_field("q_shaped", "Tell us about a project.",
                                       max_length=800),          # essay-SHAPED input
                            make_field("q_ai", "Did you use AI to write this "
                                               "application?", type_="yes_no",
                                       options=["Yes", "No"]),   # never a model's
                            make_field("q_salary", "What is your desired salary?",
                                       max_length=60),
                            make_field("q_reloc", "Are you willing to relocate?",
                                       type_="yes_no", options=["Yes", "No"]),
                            make_field("q_notice", "Notice period"),
                            make_field("q_cv", "Upload your CV", type_="input_file"),
                            make_field("q_gender", "Gender", section="DEMOGRAPHIC"),
                        ])
    assert [question["key"] for question in tool.questions_from_fieldmap(fieldmap)] == [
        "question_1", "q_shaped", "q_ai"]


def test_generator_refuses_a_question_that_is_not_free_text(tool, ssot: SSOT) -> None:
    """A questions file carrying a question of any OTHER shape is a broken file (a
    hand edit, or a capture from a mismatched build), and it is refused BEFORE the
    first model call, in every ToS mode.

    Load-bearing, not pedantry: routing such a question to the model would answer a
    salary box or a two-option dropdown with model prose that the overlay fills
    verbatim -- and it would do it with NEITHER the disclosure a REQUIRES-DISCLOSURE
    employer is owed under `disclose` NOR the abstention a FORBIDS employer is owed
    under `forbid-essays`, because both gate on the same free-text predicate. The
    document is not half-written either: the refusal is a whole-document check that
    runs before any prompt is sent, so a broken file costs nothing.
    """
    asked: list[str] = []

    def runner(prompt: str, model: str) -> str:
        asked.append(prompt)
        return "Injected answer."

    for shape in ({"key": "q_salary", "label": "What is your desired salary?",
                   "type": "input_text", "norm_type": "", "required": True,
                   "options": [], "max_length": 60},
                  {"key": "q_reloc", "label": "Are you willing to relocate?",
                   "type": "yes_no", "norm_type": "", "required": True,
                   "options": ["Yes", "No"], "max_length": None}):
        doc = dict(questions_doc(),
                   questions=[questions_doc()["questions"][0], shape])
        for mode in ("allow", "disclose", "forbid-essays"):
            with pytest.raises(ValueError, match="is not free text"):
                tool.generate_answers(doc, ssot, company="Acme", tos_mode=mode,
                                      runner=runner)
    assert asked == []   # not one prompt was sent


def test_generator_disclose_forbids_an_essay_with_no_room_for_the_disclosure(
        tool, ssot: SSOT, tmp_path) -> None:
    """Under `disclose` what the form receives is essay PLUS disclosure, so an essay
    whose own cap cannot fit both is UNANSWERABLE in that mode: it is recorded by
    name for the owner (the fail-closed route the AI-use question already takes), not
    squeezed into whatever the disclosure leaves.

    The alternative is what makes this load-bearing. Clamping the model's budget to
    the few characters left would prompt for a stub, get a longer answer back, and
    the hard error on that over-cap answer would abort the WHOLE generation --
    discarding every essay already written in the run and recording the unanswerable
    question in neither `answers` nor `tos_forbidden`. Here the run survives: the
    long essay is answered, written out, and reads back through the engine's loader.
    """
    disclosure = str(ssot.get("canned_answers.ai_use_disclosure")).strip()
    reserved = len(disclosure) + 2      # the blank line the disclosure rides behind

    doc = dict(questions_doc(), questions=[
        questions_doc()["questions"][0],                       # 800: answerable
        dict(questions_doc()["questions"][0], key="question_3", label="Why us?",
             max_length=60)])                                  # no room at all
    generated = tool.generate_answers(
        doc, ssot, company="Acme", tos_mode="disclose",
        runner=lambda prompt, model: "Injected answer.")
    assert [answer["key"] for answer in generated["answers"]] == ["question_1"]
    assert [(entry["label"], entry["reason"])
            for entry in generated["tos_forbidden"]] == [
        ("Why us?", f"disclosure text is {len(disclosure)} characters: the posting's "
                    "own 60-character limit leaves no room for an answer under it")]
    out = tool.write_yaml(generated, tmp_path / "acme-12345.yaml")
    assert [answer.key for answer in load_generated_answers(out).answers] == [
        "question_1"]

    # The boundary. A cap leaving EXACTLY `MIN_DISCLOSED_ANSWER` characters under the
    # disclosure is answerable, and that is the budget the model is told; one
    # character less is refused. The composed answer fills the cap exactly, which the
    # generator accepts (a limit is a limit, not an off-by-one).
    prompts: list[str] = []

    def runner(prompt: str, model: str) -> str:
        prompts.append(prompt)
        return "x" * tool.MIN_DISCLOSED_ANSWER

    cap = reserved + tool.MIN_DISCLOSED_ANSWER
    exact = dict(questions_doc(), questions=[
        dict(questions_doc()["questions"][0], max_length=cap)])
    at_boundary = tool.generate_answers(exact, ssot, company="Acme",
                                        tos_mode="disclose", runner=runner)
    assert at_boundary["tos_forbidden"] == []
    assert f"Stay within {tool.MIN_DISCLOSED_ANSWER} characters" in prompts[0]
    assert len(at_boundary["answers"][0]["value"]) == cap

    tight = dict(questions_doc(), questions=[
        dict(questions_doc()["questions"][0], max_length=cap - 1)])
    below = tool.generate_answers(tight, ssot, company="Acme", tos_mode="disclose",
                                  runner=runner)
    assert below["answers"] == []
    assert [entry["label"] for entry in below["tos_forbidden"]] == [
        "Why do you want to work here?"]
    assert len(prompts) == 1            # the refused question was never prompted


def test_generator_ai_policy_is_never_asked_of_the_model(tool, ssot: SSOT) -> None:
    """A question about the applicant's OWN AI USE IN THIS APPLICATION never reaches
    a model, in ANY tos mode. It is answered verbatim from the seeded disclosure, or
    it is not answered at all and is recorded by name.

    The reason is grounding, not politeness: how this application was written is
    not a fact of the SSOT excerpt the prompt carries, so a model asked to declare
    it can only invent a stance about the applicant's conduct -- the one class of
    fabrication this tool exists to prevent.

    The predicate is NARROW for the mirror-image reason. An essay ABOUT AI ("your
    experience with large language models") is a question about the owner's WORK:
    routing it to the disclosure would answer an 800-character question with an
    unrelated paragraph about how the FORM was written, count the field complete,
    and record the loss in neither `answers` nor `tos_forbidden`. Only a deictic
    reference to THIS application (or a form's own AI-policy section) marks the
    question the model must never see.

    `allow` and `disclose` take the same route on THAT question; they differ on the
    essays, where only `disclose` sends the disclosure along."""
    asked: list[str] = []

    def runner(prompt: str, model: str) -> str:
        asked.append(prompt)
        return "Injected answer."

    disclosure = str(ssot.get("canned_answers.ai_use_disclosure")).strip()

    for authorship in ("AI Policy for Application",
                       "Did you use AI to write this application?",
                       "Have you used an AI assistant to complete this form?",
                       "Do you disclose the use of ChatGPT in your answers?"):
        assert tool.is_ai_policy_question(authorship), authorship
    for about_ai in ("Describe your experience using large language models in "
                     "production.",
                     "Tell us about a project where you used artificial "
                     "intelligence.",
                     "Which AI assistant have you built on, and what did you "
                     "learn?"):
        assert not tool.is_ai_policy_question(about_ai), about_ai

    # End to end: the LLM-experience ESSAY is answered by the model like any other
    # essay, not filled with the authorship disclosure.
    llm_essay = dict(questions_doc()["questions"][0], key="question_3",
                     label="Describe your experience using large language models "
                           "in production.")
    doc = tool.generate_answers(dict(questions_doc(), questions=[llm_essay]), ssot,
                                company="Acme", tos_mode="allow", runner=runner)
    assert doc["answers"] == [{"key": "question_3", "label": llm_essay["label"],
                               "value": "Injected answer."}]
    assert doc["tos_forbidden"] == []
    assert any("large language models" in prompt for prompt in asked)

    unseeded = SSOT({"identity": {"full_name": "Ada Lovelace"}})
    doc = tool.generate_answers(questions_doc(), unseeded, company="Acme",
                                tos_mode="allow", runner=runner)
    assert [(entry["label"], entry["reason"])
            for entry in doc["tos_forbidden"]] == [
        ("AI Policy for Application", "disclosure text not seeded")]
    assert [answer["label"] for answer in doc["answers"]] == [
        "Why do you want to work here?"]

    # `disclose` with NOTHING seeded cannot attach the disclosure the employer
    # requires, so every free-text question goes to human handoff rather than
    # shipping an undisclosed model-authored essay: fail closed, recorded by name.
    doc = tool.generate_answers(questions_doc(), unseeded, company="Acme",
                                tos_mode="disclose", runner=runner)
    assert doc["answers"] == []
    assert [(entry["label"], entry["reason"])
            for entry in doc["tos_forbidden"]] == [
        ("Why do you want to work here?", "disclosure text not seeded"),
        ("AI Policy for Application", "disclosure text not seeded")]

    for mode in ("allow", "disclose"):
        doc = tool.generate_answers(questions_doc(), ssot, company="Acme",
                                    tos_mode=mode, runner=runner)
        values = {answer["label"]: answer["value"] for answer in doc["answers"]}
        assert values["AI Policy for Application"] == disclosure, mode
        assert doc["tos_forbidden"] == [], mode
        # The two modes are NOT interchangeable: the disclosure rides with the
        # essay under `disclose`, and under `allow` it does not.
        assert values["Why do you want to work here?"] == (
            f"Injected answer.\n\n{disclosure}" if mode == "disclose"
            else "Injected answer."), mode

    # Under `disclose` the model is told the budget the disclosure LEAVES it (the
    # essay's own cap is 800), so the COMPOSED answer still fits the form.
    assert any(f"Stay within {800 - len(disclosure) - 2} characters" in prompt
               for prompt in asked)

    # The seeded disclosure is held to the posting's own cap exactly as a model
    # answer is: a compliance statement the form would truncate mid-sentence is
    # recorded by name, never filled. Nothing reaches the fill layer unchecked.
    tiny = dict(questions_doc(),
                questions=[dict(questions_doc()["questions"][1], max_length=40)])
    doc = tool.generate_answers(tiny, ssot, company="Acme", tos_mode="disclose",
                                runner=runner)
    assert doc["answers"] == []
    assert [entry["reason"] for entry in doc["tos_forbidden"]] == [
        f"disclosure text is {len(disclosure)} characters, over the posting's own "
        "40-character limit"]

    # Answered or refused, the AI-use question was never put to the model: the
    # only prompts sent were for the essays.
    assert asked and not any("AI Policy" in prompt for prompt in asked)


def test_generator_refuses_missing_ssot(tool, tmp_path, capsys) -> None:
    """No SSOT, no generation: the tool refuses rather than inventing content. A
    typo'd --jd is refused on the same footing, since it would otherwise produce
    an under-grounded generation that reads as a successful one. An OMITTED
    option stays fine (that grounding simply was not offered).

    A typo'd or corrupt --questions is refused the same way, and for the same reason
    the other two input paths are: an owner error on the command line deserves the
    one-line message the tool already gives, not a raw traceback."""
    questions = tmp_path / "questions.json"
    questions.write_text("{}")
    code = tool.main(["generate", "--questions", str(questions),
                      "--ssot", str(tmp_path / "absent.yaml"),
                      "--company", "Acme", "--tos-mode", "allow",
                      "--out", str(tmp_path / "out.yaml")])
    assert code == 2
    assert "SSOT not found" in capsys.readouterr().err
    assert not (tmp_path / "out.yaml").exists()

    code = tool.main(["generate", "--questions", str(questions),
                      "--ssot", str(FIXTURES / "ssot-fake.yaml"),
                      "--jd", str(tmp_path / "absent-jd.txt"),
                      "--company", "Acme", "--tos-mode", "allow",
                      "--out", str(tmp_path / "out.yaml")])
    assert code == 2
    assert "job description not found" in capsys.readouterr().err
    assert not (tmp_path / "out.yaml").exists()

    code = tool.main(["generate", "--questions", str(tmp_path / "absent.json"),
                      "--ssot", str(FIXTURES / "ssot-fake.yaml"),
                      "--company", "Acme", "--tos-mode", "allow",
                      "--out", str(tmp_path / "out.yaml")])
    assert code == 2
    assert "questions file not found" in capsys.readouterr().err
    assert not (tmp_path / "out.yaml").exists()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    code = tool.main(["generate", "--questions", str(corrupt),
                      "--ssot", str(FIXTURES / "ssot-fake.yaml"),
                      "--company", "Acme", "--tos-mode", "allow",
                      "--out", str(tmp_path / "out.yaml")])
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert not (tmp_path / "out.yaml").exists()


# -- AI-policy ATTESTATION SELECT: fail closed always (W5.1-R2 FX3, 2026-07-18) --
#
# OWNER RULING. An AI-policy attestation (an AI-policy question that offers OPTIONS,
# e.g. Canonical's "I agree to use only my own words ... AI ... will disqualify",
# Yes/No) is NEVER answered from a seeded scalar, seeded or not, in any --tos-mode.
# Its Yes/No polarity is not fixed across employers -- Yes = compliant on an
# own-words framing, Yes = used-AI on a "did you use AI?" framing -- so one scalar
# cannot answer both honestly. It fails closed to human handoff, recorded by name.
# Its mere presence also forces forbid-essays for the WHOLE posting: every free-text
# question routes to tos_forbidden instead of the model, so no essay is shipped to an
# employer that signalled it forbids AI-authored content. See docs/vendor-tos.md
# (Canonical) and bin/generate_answers.py `_route_by_tos` / `_posting_forbids_ai`.

def _attestation_select(**over) -> dict:
    q = {"key": "question_9", "label": "AI Policy for Application",
         "type": "multi_value_single_select", "norm_type": "", "required": True,
         "options": ["Yes", "No"], "max_length": None}
    q.update(over)
    return q


def _never_called(prompt: str, model: str) -> str:
    raise AssertionError("the model was called for an attestation select")


def test_generator_ai_policy_attestation_fails_closed_both_polarities(
        tool, ssot: SSOT) -> None:
    # ANTI-GAMING (W5.1-R2 FX3). Neither polarity of an AI-policy attestation is ever
    # answered from a scalar, seeded or NOT: an "own words / no AI" framing (Yes =
    # compliant) and a "did you use AI?" framing (Yes = used AI) attach opposite
    # senses to the same option, so one seeded scalar cannot answer both honestly.
    # Both fail closed to human handoff in every mode, recorded by name -- the engine
    # never attests on the owner's behalf.
    own_words = ("I agree to use only my own words in this application; the use of AI "
                 "will disqualify my application")
    did_use_ai = "Did you use AI to write this application?"
    assert tool.is_ai_policy_question(own_words)
    assert tool.is_ai_policy_question(did_use_ai)

    seeded_yes = SSOT({"canned_answers": dict(
        (ssot.get("canned_answers") or {}), ai_policy_attestation="Yes")})
    seeded_no = SSOT({"canned_answers": dict(
        (ssot.get("canned_answers") or {}), ai_policy_attestation="No")})

    for label in (own_words, did_use_ai):
        for seed in (ssot, seeded_yes, seeded_no):
            for mode in ("allow", "disclose", "forbid-essays"):
                doc = tool.generate_answers(
                    dict(questions_doc(),
                         questions=[_attestation_select(label=label)]),
                    seed, company="Acme", tos_mode=mode, runner=_never_called)
                assert doc["answers"] == [], (label, mode)
                assert [f["label"] for f in doc["tos_forbidden"]] == [label], \
                    (label, mode)


def test_generator_ai_forbid_attestation_forces_forbid_essays(
        tool, ssot: SSOT) -> None:
    # A detected AI-policy attestation on a posting forces forbid-essays for that
    # posting: every free-text question routes to tos_forbidden instead of the model,
    # so no essay is shipped to an employer that forbids AI-authored content -- even
    # under --tos-mode=allow. `_never_called` proves the essay never reaches the model.
    essay = {"key": "question_1", "label": "Why do you want to work here?",
             "type": "textarea", "norm_type": "LONGTEXT", "required": True,
             "options": [], "max_length": 800}
    posting = dict(questions_doc(), questions=[essay, _attestation_select()])

    for mode in ("allow", "disclose", "forbid-essays"):
        doc = tool.generate_answers(posting, ssot, company="Acme",
                                    tos_mode=mode, runner=_never_called)
        assert doc["answers"] == [], mode
        forbidden = {f["label"]: f["reason"] for f in doc["tos_forbidden"]}
        assert set(forbidden) == {"Why do you want to work here?",
                                  "AI Policy for Application"}, mode
        assert forbidden["Why do you want to work here?"] == (
            "employer forbids AI-generated content"), mode

    # CONTRAST: the SAME essay on a posting with NO attestation is answered by the
    # model under `allow` -- proving it is the detected attestation, not the essay,
    # that forces the forbid.
    answered = tool.generate_answers(
        dict(questions_doc(), questions=[essay]), ssot, company="Acme",
        tos_mode="allow", runner=lambda prompt, model: "An essay.")
    assert [(a["key"], a["value"]) for a in answered["answers"]] == [
        ("question_1", "An essay.")]
    assert answered["tos_forbidden"] == []


# --------------------------------------------------------------------------- #
# DECODE POLICY (P1-4). A runner drives a foreign program, and a foreign program
# can emit bytes that are not valid UTF-8. Under the strict decode a bare
# `text=True` gives, ONE such byte raised UnicodeDecodeError out of subprocess and
# aborted the whole generation (the identical defect cost a 13-minute production
# run on 2026-07-20 through a 170KB model response). These tests drive REAL bytes
# through the REAL subprocess: a test that only inspected the kwargs would still
# pass if the decode raised.
# --------------------------------------------------------------------------- #

def _fake_bin(tmp_path, name: str, payload: bytes, *, returncode: int = 0,
              stream: str = "stdout") -> Path:
    """An executable stand-in that ignores its arguments and writes `payload` as
    RAW BYTES, so the real `subprocess.run` is the thing that decodes them."""
    script = tmp_path / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.{stream}.buffer.write({payload!r})\n"
        f"sys.exit({returncode})\n"
    )
    script.chmod(0o755)
    return script


def test_strict_decode_would_raise_on_the_same_bytes(tmp_path) -> None:
    """Teeth for the regressions below: the kwargs the runners USED to pass do
    raise on this exact input, so the passing tests that follow are not vacuous."""
    script = _fake_bin(tmp_path, "bad_utf8", b"an answ\x88er")
    with pytest.raises(UnicodeDecodeError):
        subprocess.run([str(script)], capture_output=True, text=True, timeout=30)


def test_command_runner_survives_invalid_utf8_byte(tool, tmp_path,
                                                   capsys) -> None:
    """`--runner-cmd` through the real shell: an undecodable byte costs ONE
    character, not the run, and it is NAMED on stderr rather than swallowed."""
    script = _fake_bin(tmp_path, "bad_utf8", b"an answ\x88er")
    assert tool._command_runner(str(script))("prompt", "sonnet") == "an answ�er"
    assert "1 undecodable byte(s)" in capsys.readouterr().err


def test_claude_runner_survives_invalid_utf8_byte(tool, tmp_path, monkeypatch,
                                                  capsys) -> None:
    """The PRODUCTION runner, resolved off PATH exactly as it is in production:
    the same byte that used to abort the generation now degrades one character."""
    _fake_bin(tmp_path, "claude", b"an answ\x88er")
    monkeypatch.setenv("PATH", str(tmp_path))
    assert tool._claude_runner("prompt", "sonnet") == "an answ�er"
    assert "1 undecodable byte(s)" in capsys.readouterr().err


def test_decode_replacement_count_is_recorded_and_discriminates(
        tool, tmp_path, monkeypatch, capsys) -> None:
    """The count must be attributable AND it must discriminate: a clean run over
    the same path says nothing, so the warning is a signal and not noise."""
    _fake_bin(tmp_path, "claude", b"tw\x88o b\x88ad")
    monkeypatch.setenv("PATH", str(tmp_path))
    tool._claude_runner("prompt", "sonnet")
    assert "2 undecodable byte(s)" in capsys.readouterr().err

    _fake_bin(tmp_path, "claude", "a clean answer".encode("utf-8"))
    assert tool._claude_runner("prompt", "sonnet") == "a clean answer"
    assert capsys.readouterr().err == ""


def test_undecodable_stderr_on_a_failing_runner_is_still_named(tool, tmp_path,
                                                               capsys) -> None:
    """A non-zero exit whose STDERR carries bad bytes: the count is taken BEFORE
    the exit-code branch, so a lossy diagnosis says it is lossy. The failure
    contract itself is unchanged -- it still raises."""
    script = _fake_bin(tmp_path, "bad_utf8", b"boom\x88boom", returncode=1,
                       stream="stderr")
    with pytest.raises(RuntimeError, match="runner exited 1"):
        tool._command_runner(str(script))("prompt", "sonnet")
    assert "1 undecodable byte(s)" in capsys.readouterr().err


def test_runners_never_pass_the_strict_default(tool, monkeypatch) -> None:
    """The kwarg pair itself, pinned on BOTH runners. Not a substitute for the
    byte-level tests above (it would pass even if the decode raised); it exists so
    a future edit that re-introduces `text=True` names itself."""
    module = tool
    seen: list[dict] = []

    def fake_run(*args, **kwargs):
        seen.append(kwargs)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok",
                                           stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._claude_runner("prompt", "sonnet")
    module._command_runner("some-command")("prompt", "sonnet")
    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        assert "text" not in kwargs


# --------------------------------------------------------------------------- #
# FRESHNESS CONTRACT (NEW-1). The answers file lives at a path derived from the
# posting, so a regeneration that CRASHES leaves the previous run's file exactly
# where the next fill looks for it. Guarding on `is_file()` alone loaded that stale
# prose and reported it as applied: the only silent-degradation path in the engine.
# The document must now PROVE it belongs to the posting, the question set and the
# SSOT grounding in front of the reader.
# --------------------------------------------------------------------------- #

def _provenance_fieldmap(**over) -> FieldMap:
    """The posting these answers are written for: one essay and the AI-use
    question, the shape `questions_from_fieldmap` keeps."""
    fields = over.pop("fields", None) or [
        make_field("question_1", "Why do you want to work here?",
                   type_="textarea", max_length=800),
        make_field("question_2", "AI Policy for Application",
                   type_="textarea", max_length=400),
    ]
    return FieldMap(vendor="greenhouse", posting_id="12345",
                    captured_at="2026-07-13T00:00:00Z", fields=fields, **over)


def _write_answers(tool, ssot: SSOT, tmp_path, fieldmap=None, **over) -> Path:
    """A generated-answers document written the PRODUCTION way: the questions go
    through a JSON round-trip first, because `capture` writes them to a JSON file
    and `generate` reads them back, and a fingerprint that survives only in-memory
    dicts would fail on every real run."""
    questions = json.loads(json.dumps(
        tool.questions_from_fieldmap(fieldmap or _provenance_fieldmap())))
    doc = tool.generate_answers(dict(questions_doc(), questions=questions, **over),
                                ssot, company="Acme", tos_mode="allow",
                                runner=lambda prompt, model: "A grounded answer.")
    return tool.write_yaml(doc, tmp_path / "greenhouse-acme-12345.yaml")


def test_provenance_is_stamped_without_breaking_the_frozen_schema(
        tool, ssot: SSOT, tmp_path) -> None:
    """The block is written, and the engine's own loader still reads the document:
    `schema_version` stays "1" and an unknown key is ignored, so no existing reader
    is disturbed by the stamp."""
    path = _write_answers(tool, ssot, tmp_path)
    raw = yaml.safe_load(path.read_text())
    assert raw["schema_version"] == "1"
    assert set(raw["provenance"]) == {"contract_version", "questions_fingerprint",
                                      "grounding_fingerprint"}
    assert raw["provenance"]["contract_version"] == tool.PROVENANCE_VERSION
    assert load_generated_answers(path).vendor == "greenhouse"


def test_fresh_document_is_accepted(tool, ssot: SSOT, tmp_path) -> None:
    """The accept path: the same posting, the same questions, the same SSOT."""
    path = _write_answers(tool, ssot, tmp_path)
    assert tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=_provenance_fieldmap(), ssot=ssot) is None


def test_stale_document_is_refused_when_the_questions_changed(
        tool, ssot: SSOT, tmp_path) -> None:
    """THE DEFECT ITSELF. Answers were written, the posting's questions then
    changed, the regeneration crashed, and the old file stayed on disk. It must be
    refused, by a named reason, so the essays surface UNFILLED."""
    path = _write_answers(tool, ssot, tmp_path)
    moved_on = _provenance_fieldmap(fields=[
        make_field("question_1", "Why do you want to work here?",
                   type_="textarea", max_length=200),   # the cap shrank
        make_field("question_2", "AI Policy for Application",
                   type_="textarea", max_length=400),
    ])
    reason = tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=moved_on, ssot=ssot)
    assert reason is not None
    assert "question set has changed" in reason

    # A NEW question the answers never covered is the same refusal: an answers
    # file that is merely INCOMPLETE for this form is not a file to trust either.
    added = _provenance_fieldmap(fields=[
        *_provenance_fieldmap().fields,
        make_field("question_3", "Describe a hard problem you solved",
                   type_="textarea", max_length=600),
    ])
    assert "question set has changed" in tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=added, ssot=ssot)


def test_stale_document_is_refused_when_the_ssot_grounding_changed(
        tool, ssot: SSOT, tmp_path) -> None:
    """Prose built from facts the SSOT no longer states is stale prose, even when
    the form did not move."""
    path = _write_answers(tool, ssot, tmp_path)
    edited = SSOT({**yaml.safe_load((FIXTURES / "ssot-fake.yaml").read_text()),
                   "experience_years": 9})
    reason = tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=_provenance_fieldmap(), ssot=edited)
    assert reason is not None and "SSOT grounding has changed" in reason

    # An SSOT edit OUTSIDE the grounding block does NOT invalidate the answers: no
    # prompt ever saw that field, so nothing the model was told has changed.
    untold = SSOT({**yaml.safe_load((FIXTURES / "ssot-fake.yaml").read_text()),
                   "notify": {"ntfy_topic": "somewhere-else"}})
    assert tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=_provenance_fieldmap(), ssot=untold) is None


def test_document_for_another_posting_is_refused(tool, ssot: SSOT,
                                                 tmp_path) -> None:
    """Identity is checked before the fingerprints, so a file that landed under the
    wrong name reports the plain reason rather than a digest mismatch."""
    path = _write_answers(tool, ssot, tmp_path)
    for field, value in (("vendor", "lever"), ("slug", "other-co"),
                         ("job_id", "99999")):
        posting = {"vendor": "greenhouse", "slug": "acme", "job_id": "12345"}
        posting[field] = value
        reason = tool.stale_answers_reason(
            path, **posting, fieldmap=_provenance_fieldmap(), ssot=ssot)
        assert reason is not None and f"this posting is {field}=" in reason


def test_document_with_no_provenance_is_refused(tool, ssot: SSOT,
                                                tmp_path) -> None:
    """BACKWARD COMPATIBILITY, decided deliberately: a legacy file carries nothing
    that separates a fresh one from a stale one, so it is REFUSED rather than
    accepted with a warning. Accepting it would leave the hole open for exactly the
    population the defect was found in. The reason names the fix."""
    path = _write_answers(tool, ssot, tmp_path)
    legacy = yaml.safe_load(path.read_text())
    legacy.pop("provenance")
    path.write_text(yaml.safe_dump(legacy, sort_keys=False, allow_unicode=True))
    # it is a perfectly VALID document to the engine's loader: only provenance
    # is missing, which is precisely why existence was never proof of freshness.
    assert load_generated_answers(path).answers
    reason = tool.stale_answers_reason(
        path, vendor="greenhouse", slug="acme", job_id="12345",
        fieldmap=_provenance_fieldmap(), ssot=ssot)
    assert reason is not None
    assert "no provenance block" in reason and "Re-run" in reason


def test_unverifiable_documents_fail_closed(tool, ssot: SSOT, tmp_path) -> None:
    """Every uncertainty is a refusal: there is no input that returns None on
    something the reader could not positively verify."""
    posting = dict(vendor="greenhouse", slug="acme", job_id="12345",
                   fieldmap=_provenance_fieldmap(), ssot=ssot)

    missing = tmp_path / "gone.yaml"
    assert tool.stale_answers_reason(missing, **posting) is not None

    not_yaml = tmp_path / "broken.yaml"
    not_yaml.write_text("{unclosed: [")
    assert tool.stale_answers_reason(not_yaml, **posting) is not None

    not_mapping = tmp_path / "list.yaml"
    not_mapping.write_text("- just\n- a list\n")
    assert "not a mapping" in tool.stale_answers_reason(not_mapping, **posting)

    path = _write_answers(tool, ssot, tmp_path)
    for bad in ("0", "", None, {"contract_version": "1"}):
        doc = yaml.safe_load(path.read_text())
        doc["provenance"] = bad
        wrong = tmp_path / "wrong-version.yaml"
        wrong.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        assert tool.stale_answers_reason(wrong, **posting) is not None, bad


def test_fingerprints_ignore_question_order_but_not_content(tool,
                                                            ssot: SSOT) -> None:
    """Order is the vendor's to change and a reordered form is not a changed
    question; anything the answer DEPENDS on is."""
    questions = tool.questions_from_fieldmap(_provenance_fieldmap())
    assert tool.questions_fingerprint(questions) == tool.questions_fingerprint(
        list(reversed(questions)))
    assert questions[0]["required"] is False  # so the flip below really flips
    for change in ({"label": "Why us?"}, {"max_length": 100}, {"key": "q9"},
                   {"options": ["Yes", "No"]}, {"required": True},
                   {"type": "input_text"}, {"norm_type": "LONGTEXT"}):
        mutated = [dict(questions[0], **change), questions[1]]
        assert tool.questions_fingerprint(mutated) != tool.questions_fingerprint(
            questions), change


# --------------------------------------------------------------------------- #
# The READER half of the contract lives in `w5_accept.py`, a top-level script that
# needs a live browser and cannot be imported. Its GUARD is pinned structurally
# instead: the loader call must sit inside the branch the freshness check opens, so
# no edit can restore the unguarded read without this test going red.
# --------------------------------------------------------------------------- #

def _harness_tree() -> ast.Module:
    return ast.parse(HARNESS_PATH.read_text())


def _calls_named(tree, name: str) -> list[ast.Call]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == name]


def test_harness_loads_generated_answers_only_behind_the_freshness_check() -> None:
    tree = _harness_tree()
    assert len(_calls_named(tree, "stale_answers_reason")) == 1

    loads = _calls_named(tree, "load_generated_answers")
    assert len(loads) == 1, "one read site, so one guard covers it"

    guarded = [call for node in ast.walk(tree) if isinstance(node, ast.If)
               and any(isinstance(sub, ast.Name) and sub.id == "stale"
                       for sub in ast.walk(node.test))
               for stmt in node.body
               for call in ast.walk(stmt) if isinstance(call, ast.Call)]
    assert loads[0] in guarded, (
        "the generated-answers read must sit inside the `stale is None` branch")


def test_harness_records_the_refusal_rather_than_dropping_it() -> None:
    """A refused file must be visible in the machine-readable result, not only on
    stderr: the census is what the operator reads."""
    tree = _harness_tree()
    constants = {node.value for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert "generated_rejected" in constants
