# Vendor and employer ToS: automated filling and AI-generated content

What each ATS platform and each known employer says about (a) automated form
filling and (b) AI-generated application content, with the operational
consequence for the engine's content channel (`engine/content.py`,
`bin/generate_answers.py`).

Verdict vocabulary: **FORBIDS**, **REQUIRES-DISCLOSURE**, **SILENT/ALLOWS**,
**UNCLEAR**. Only claims backed by a quoted source below are recorded; nothing is
inferred from silence beyond the SILENT/ALLOWS label itself. Sources reviewed
2026-07 (accessed 2026-07-13).

## Standing rule

Completeness counts a free-text field (essay, cover letter, any
write-your-answer question) as **ToS-fillable, and therefore automated**, UNLESS
this document records a FORBIDS or REQUIRES-DISCLOSURE verdict for that platform
or that employer. A FORBIDS verdict means the field is listed in the generated
answers' `tos_forbidden` block: it is left unfilled for a human, and it is NEVER
hidden from the field map. `engine/content.py` carries it through to
`OverlayReport.tos_forbidden` by name, so that the acceptance gate can subtract
it explicitly once the overlay is wired into the vendor loops (that wiring is a
later stage: today the overlay has no production caller). A
REQUIRES-DISCLOSURE verdict means the answer is still automated, and the
disclosure text (`canned_answers.ai_use_disclosure`) rides with it: under
`--tos-mode disclose` the generator APPENDS that seeded text to every
model-written answer, and it reduces the prompt's length budget by the
disclosure's own length so the composed answer still fits the posting's
`max_length`. That is the one behavioural difference between `disclose` and
`allow`, and it is the point of the mode: an employer who asks for disclosure
must not receive a bare model-authored essay. When the disclosure text is NOT
seeded there is no compliant essay to send, so under `disclose` every free-text
question is listed `tos_forbidden` ("disclosure text not seeded") for a human
instead: fail closed. Same posture when the posting's own `max_length` has no
room for BOTH the disclosure and an answer under it: that question is
unanswerable under `disclose`, so it is listed `tos_forbidden` with the reason
rather than squeezed into whatever the disclosure leaves.

Every one of these verdicts applies to FREE-TEXT questions, which are the only
questions the generator automates at all: a dropdown, a yes/no or a one-line
input is answered deterministically from the SSOT (the kernel resolver, and the
content overlay's canned routes), and is never put to a model in any mode.

The engine stays honest either way: a ToS-forbidden field is reported by name,
never quietly dropped from the denominator.

A question about the applicant's OWN AI use IN THIS APPLICATION ("did you use AI
to write these answers?") is handled outside the modes above, and identically in
all of them: it is answered verbatim from `canned_answers.ai_use_disclosure`, or
it is not answered at all. It is never put to a model, not even under the default
`allow` mode. The reason is grounding: how an application was written is not a
fact of the SSOT excerpt the prompt carries, so a model asked to declare it could
only invent a stance about the owner's own conduct. When the disclosure key is not
seeded, or the posting's `max_length` would truncate the disclosure mid-sentence,
the question is listed `tos_forbidden` with that reason, so the gap escalates to
the owner. `allow` and `disclose` take the same route on THAT question; they
differ on the essays, where only `disclose` sends the disclosure along.

A question ABOUT AI is not that question. "Describe your experience with large
language models" asks about the applicant's WORK, and it is answered by the model
like any other essay: filling it with the authorship disclosure would answer an
800-character question with an unrelated paragraph and count the field complete.
The predicate keys on a deictic reference to this application ("this form", "these
answers"), or on a form's own AI-policy section title, never on the bare word
"use".

Demographic questions sit outside this rule entirely. COMPLIANCE_EEOC /
DEMOGRAPHIC / VOLUNTARY fields are never auto-answered on POLICY grounds, whatever
a vendor's ToS permits: the generator never sends them to a model and the overlay
never fills one.

## Platforms

### Greenhouse

| Axis | Verdict |
|---|---|
| Automated form filling | **FORBIDS** |
| AI-generated content | **SILENT/ALLOWS** |

The applicant-facing *My Greenhouse User Agreement*, section 3 ("Your Account"),
lists among prohibited activities: "use automated means, including spiders,
robots, crawlers, or similar means or processes to access or use the Services."
Automated filling of the hosted form falls under "access or use the Services".
The corporate terms at greenhouse.com/legal bind employer customers, not the
applicant; the User Agreement above is the applicant-binding one.

No AI or AI-generated-content provision appears anywhere in the User Agreement.

Consequence: **essays automated** (no AI restriction). The automation clause bears
on the fill channel, not the content channel, and the engine's fill stays a
never-submitting dry run under owner supervision; the owner owns that call.

- https://my.greenhouse.io/users/agreement
- https://www.greenhouse.com/legal

### Lever

| Axis | Verdict |
|---|---|
| Automated form filling | **SILENT/ALLOWS** |
| AI-generated content | **SILENT/ALLOWS** |

Lever's Terms of Service "govern a customer's acquisition and use of Lever" and
bind only the recruiting Customer ("the entity identified on the Order Form").
An applicant on jobs.lever.co is not a party to it. There is no spider, robot,
crawler, or scraping clause, and no AI-generated-content provision, in Lever's
terms or in parent Employ Inc's terms.

Consequence: **essays automated**.

- https://www.lever.co/legal/terms-of-service
- https://www.employinc.com/terms-of-service/

### Ashby

| Axis | Verdict |
|---|---|
| Automated form filling | **SILENT/ALLOWS** |
| AI-generated content | **SILENT/ALLOWS** |

Ashby's published Customer Terms of Service bind only "Customer" = "the
corporation, LLC, partnership... or other business entity entering into this
Agreement". The Acceptable Use clause (section 5.1) restricts customer conduct;
"Customer is responsible and liable for... Users' use of the Service" (5.4). No
applicant-facing clause forbids form-fill automation, bots, scraping, or
AI-generated answers. The only candidate-facing document is a Privacy Policy
(data handling: "Ashby does not train AI models on customer data"), silent on
applicant automation and on AI-authored content.

Consequence: **essays automated**.

### Workable

| Axis | Verdict |
|---|---|
| Automated form filling | **SILENT/ALLOWS** (detected and tagged) |
| AI-generated content | **SILENT/ALLOWS** (detected and tagged) |

Workable's terms bind the signing "Customer" only; section 4.5 restricts customer
conduct and carries no anti-bot, anti-scraping, or automation clause reaching
candidates. Workable does not prohibit AI-assisted or automated applications.
Its help documentation states that Workable "provides tools to help you identify
AI-assisted applications and manage them": it "detects AI-assisted applications
by analyzing application metadata", "The candidate is tagged as AI-assisted", and
employers can "filter your candidate list to include or exclude AI-assisted
applications". That is employer-configurable management, not a platform ban, and
no disclosure is required of the candidate.

Consequence: **essays automated**. Note the operational risk, which is a
detection risk and not a ToS risk: an application may be involuntarily tagged
AI-assisted and filtered by the employer.

## Employers

### Anthropic

| Axis | Verdict |
|---|---|
| AI-generated content | **REQUIRES-DISCLOSURE** (AI-refined allowed, AI-generated not) |

Current guidance (anthropic.com/candidate-ai-guidance): when applying, "create
your first draft yourself, then use Claude to refine it. We want to see your real
experience, but Claude can polish how you communicate." Core principle: "Be
yourself. Use AI to refine your ideas, not replace them. We want to see your
actual experience and how you think, not AI-generated responses." Take-home
exercises: "Complete these without Claude unless we indicate otherwise." Live
interviews: "no AI assistance unless we indicate otherwise." Anthropic states it
expects "the same transparency from you." (An early-2025 blanket ban on AI during
the application process was reversed around 2025-07-21.)

Consequence: **essays automated with disclosure text**. Run the generator with
`--tos-mode disclose`: every essay carries the SSOT's
`canned_answers.ai_use_disclosure` appended to it, and an AI-use question is
answered verbatim from that same text, which must state that the owner drafted the
content and used AI to refine the wording. A fully model-authored answer WITHOUT
that statement is out of policy here, which is why the generator refuses to write
one: with the disclosure unseeded, `disclose` hands every essay to the owner
instead. The disclosure text is load-bearing, and the owner reviews every Anthropic
answer before the fill.

- https://www.anthropic.com/candidate-ai-guidance
- https://fortune.com/2025/07/21/billion-dollar-giant-anthropic-ai-ban-hiring-policy-change-job-seekers-interview-process/

### ElevenLabs

| Axis | Verdict |
|---|---|
| AI-generated content | **SILENT/ALLOWS** |

No prohibition found; the posture is pro-AI. The ElevenLabs blog ("Agents and the
Candidate Experience") states: "just as we use AI to streamline our internal
operations, we encourage candidates to use it to empower your preparation." That
addresses interview preparation, not written application answers specifically, so
the narrower point is silent rather than explicitly permitted. ElevenLabs also
publishes an applicant privacy and AI notice covering its OWN use of AI in
hiring, not applicant conduct.

Consequence: **essays automated**.

### Agicap

| Axis | Verdict |
|---|---|
| AI-generated content | **SILENT/ALLOWS** |

Agicap's board is on Lever. All 38 live postings scanned through the public Lever
postings API: zero mention AI-generated content, LLMs, automated tools, or any
application-AI policy. career.agicap.com carries no applicant-AI statement; it
links a candidate privacy policy covering Agicap's data processing, not applicant
conduct.

Consequence: **essays automated**.

## Summary

| Target | Type | Automation | AI content | Consequence |
|---|---|---|---|---|
| Greenhouse | Platform | FORBIDS | SILENT/ALLOWS | essays automated |
| Lever | Platform | SILENT/ALLOWS | SILENT/ALLOWS | essays automated |
| Ashby | Platform | SILENT/ALLOWS | SILENT/ALLOWS | essays automated |
| Workable | Platform | SILENT/ALLOWS (tagged) | SILENT/ALLOWS (tagged) | essays automated |
| Anthropic | Employer | not addressed | REQUIRES-DISCLOSURE | essays automated with disclosure text |
| ElevenLabs | Employer | not addressed | SILENT/ALLOWS | essays automated |
| Agicap | Employer | not addressed | SILENT/ALLOWS | essays automated |

No target FORBIDS AI-generated application content outright, so no essay field is
currently listed `tos_forbidden` on ToS grounds alone; the `forbid-essays` mode
exists for the first employer that does. Anthropic is the one target whose
guidance a fully model-authored answer would breach, and the disclose mode covers
it. A new employer with no researched policy inherits its platform's verdict, and
an employer whose policy cannot be established is recorded UNCLEAR here with
"owner escalation pending" rather than assumed permissive.
