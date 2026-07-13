"""W5 live fill-acceptance harness: capture -> resolve from SSOT -> fill a LIVE throwaway apply page,
with an independent network audit proving ZERO application-submit POSTs completed.

The never-send guard is the engine's own (install_never_send via kernel.capture_toolkit._default_browser_page).
The audit is INDEPENDENT: it re-applies base._is_submit_request to every request/response the
context saw, so a submit that slipped past the guard would show as a completed submit POST.

Usage: xvfb-run python w5_accept.py <vendor> <slug> <job_id> <apply_url>
"""
import json, os, sys, traceback
from pathlib import Path

from engine.ssot import SSOT
from engine.profile_map import profile_from_real_ssot
from engine.providers import greenhouse, lever, ashby, workable
from engine.providers.base import _is_submit_request
from engine.kernel.contracts import FillAssets
from engine.kernel import capture_toolkit

VENDOR, SLUG, JOB_ID, APPLY_URL = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
PROV = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
        "workable": workable}[VENDOR]
SSOT_PATH = os.path.expanduser("~/automations/ssot/job.yaml")

result = {"vendor": VENDOR, "slug": SLUG, "job_id": JOB_ID, "stage": "start"}
try:
    ssot = SSOT.load(SSOT_PATH)
    profile = profile_from_real_ssot(ssot)
    result["stage"] = "capture"
    opener = None
    if VENDOR in ("lever", "ashby"):
        from engine.run import _build_capture_opener
        opener = _build_capture_opener()
    fieldmap = PROV.capture(SLUG, JOB_ID, opener)
    result["stage"] = "resolve"
    # Whitelisted upload assets (verified() drops any path missing on disk):
    # a full fill needs the CV pdf; photo stays None until a posting needs
    # it. cover_letter is likewise optional: verified() drops it to None when
    # the file is absent, so a run with no cover-letter document still works.
    # extra_documents seeds the optional-attachment slots (transcripts,
    # certification); the COMPRESSED university transcript is used ALWAYS -- the
    # 14.7MB original exceeds ATS upload caps -- and verified() drops any entry
    # whose file is absent, so a run missing one still works.
    assets = FillAssets(
        cv_ats=Path(os.path.expanduser("~/automations/documents/cv-ats.pdf")),
        cv_atsi=Path(os.path.expanduser("~/automations/documents/cv-atsi.pdf")),
        cover_letter=Path(os.path.expanduser(
            "~/automations/documents/cover_letter.pdf")),
        extra_documents={
            "lse_certification": Path(os.path.expanduser(
                "~/automations/documents/lse-grade-letter.pdf")),
            "transcript_university": Path(os.path.expanduser(
                "~/automations/documents/transcript-university-compressed.pdf")),
            "transcript_ib": Path(os.path.expanduser(
                "~/automations/documents/transcript-ib.pdf")),
        },
    ).verified()
    values = PROV.resolve_values(fieldmap, ssot, profile, assets=assets)

    # Content overlay (W5.1b): canned-answer routing + option-match + generated
    # essays, applied ONCE here (harness-central) so no vendor plugin wires it.
    # The overlay only ADDS resolved values before the fill; completeness stays
    # the fill-side census (kernel completeness + live-DOM sweep), so an overlay
    # bug cannot inflate the report. A malformed generated file raises and fails
    # the run loudly (error+trace path) rather than being skipped silently.
    result["stage"] = "overlay"
    from engine import content
    gen_path = Path(os.path.expanduser(
        f"~/automations/ssot/generated/{VENDOR}-{SLUG}-{JOB_ID}.yaml"))
    generated = content.load_generated_answers(gen_path) if gen_path.is_file() else None
    overlay = content.apply_content_overlay(
        values, fieldmap, ssot, generated=generated,
        posting_lang=(generated.posting_lang if generated else "en"))
    result["overlay"] = {
        "generated_file": str(gen_path) if generated else None,
        "applied": overlay.applied,
        "tos_forbidden": overlay.tos_forbidden,
        "unresolved": overlay.unresolved,
    }

    audit = {"requests": 0, "posts": 0, "completed_submits": [], "aborted_submits": []}
    result["stage"] = "browser"
    with capture_toolkit._default_browser_page() as page:
        ctx = page.context

        def on_req(r):
            audit["requests"] += 1
            if r.method == "POST":
                audit["posts"] += 1

        def on_resp(r):
            try:
                req = r.request
                if _is_submit_request(req.method, r.url, req.post_data):
                    audit["completed_submits"].append([r.url, r.status])  # VIOLATION if non-empty
            except Exception:
                pass

        def on_failed(r):
            try:
                if _is_submit_request(r.method, r.url, r.post_data):
                    audit["aborted_submits"].append(r.url)
            except Exception:
                pass

        ctx.on("request", on_req)
        ctx.on("response", on_resp)
        ctx.on("requestfailed", on_failed)

        result["stage"] = "goto"
        page.goto(APPLY_URL, wait_until="domcontentloaded", timeout=45000)
        # Let the vendor SPA hydrate (React wires its field + upload change
        # handlers) before driving fields: filling right after domcontentloaded
        # can leave a file upload in the input's FileList but unprocessed by the
        # vendor's own component state (no rendered confirmation).
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(2500)
        result["stage"] = "fill"
        report = PROV.fill(page, fieldmap, values)
        try:
            page.screenshot(path=f"/tmp/w5-{VENDOR}-filled.png", full_page=True)
            result["screenshot_path"] = f"/tmp/w5-{VENDOR}-filled.png"
        except Exception as exc:
            result["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        try:
            open(f"/tmp/w5-dom-{VENDOR}.html", "w").write(page.content())
        except Exception:
            pass

    g = lambda o, a, d=None: getattr(o, a, d)
    # Field-by-field detail for the hostile review: what value landed in each
    # driven field (truncated), plus every skip with its reason and the uploads.
    resolved_fields = [
        {"key": fv.key, "label": getattr(fv, "label", ""),
         "value": str(fv.value)[:200], "asset": getattr(fv, "asset", None)}
        for fv in g(values, "fields", []) or []
    ]
    result.update({
        "stage": "done",
        "fields_total": len(fieldmap.fields),
        "report": {
            "fillable_total": g(report, "fillable_total"),
            "filled": g(report, "filled"),
            "required_unfilled": g(report, "required_unfilled"),
            "justified_skips": g(report, "justified_skips"),
            "uploads": g(report, "uploads"),
            "skipped": g(report, "skipped"),
            "complete": g(report, "complete"),
        },
        "resolved_fields": resolved_fields,
        "net_requests": audit["requests"],
        "net_posts": audit["posts"],
        "submit_posts_completed": audit["completed_submits"],
        "submit_posts_aborted": audit["aborted_submits"],
        "NEVER_SEND_HELD": len(audit["completed_submits"]) == 0,
    })
except Exception as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
    result["trace"] = traceback.format_exc()[-800:]

print("W5_ACCEPT_JSON " + json.dumps(result))
