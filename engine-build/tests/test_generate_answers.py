"""Tests for the offline answer generator (`bin/generate_answers.py`).

The tool is a bin/ script, not an engine module, so it is loaded by path. NO test
here spawns a real subprocess: the model call goes through the injected runner
seam, and `subprocess.run` is monkeypatched to explode if anything reaches for it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from engine.content import load_generated_answers
from engine.kernel.contracts import Field, FieldMap, Locator
from engine.kernel.ssot import SSOT

FIXTURES = Path(__file__).parent / "fixtures" / "content"
TOOL_PATH = Path(__file__).parent.parent / "bin" / "generate_answers.py"


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
