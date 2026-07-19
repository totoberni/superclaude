"""Lever apply-DOM capture + parse (W5.1 Stage 2d; moved from engine.browse).

Lever is the server-rendered vendor: the apply page ships the full form DOM at
load, so there is nothing to intercept. `capture_lever` loads the page once,
reads the rendered DOM via `page.content()`, and parses the fixed base fields
plus the custom `.application-question` cards onto the canonical `FieldMap`
(`source="lever_dom"`). Every parser fails LOUDLY: a form that yields no
recognizable fields raises `CaptureShapeError` naming the shape that missed,
NEVER a silently empty FieldMap.

ROUND-3/4 LIVE FINDINGS carried over verbatim (jobs.lever.co, 2026-07-03):
each base field renders TWICE (an invisible mirror carrying the true submission
`name` with no label, plus a labeled visible twin); `_dedup_lever_base_fields`
+ the whole-map `_dedup_by_key` collapse each pair back into one logical Field
(human label, OR'd `required`, richer-source `type`). A custom question card can
render its wording inline (e.g. a consent checkbox beside the input rather than
in a `.application-label`), so `_resolve_field_label` tries `aria-label`,
`placeholder`, and the enclosing element's own text before ever emitting an
empty label.

Only the LEVER-specific capture/parse code moved here. Its transitive closure is
DISJOINT from Ashby's graphql parse (which reads the intercepted JSON and touches
no HTML tree): the two vendors share NONE of each other's helpers. Everything this
module reaches beyond
its own helpers is generic browser/HTML INFRA already single-sourced in the
kernel -- the browser page factory + timeout + tree builder/finders + node-text
reader + `CaptureShapeError` + `_now` from `engine.kernel.capture_toolkit`, and
the `Field`/`FieldMap`/`Locator` contracts + `_role_for_type` from
`engine.kernel.contracts` -- so nothing is re-implemented and there is exactly
one home for each name.

The `engine.browse` re-export shim that once forwarded these names was dissolved
in Stage 4: every importer now reaches this module directly. `engine.fill._capture`
imports `capture_lever` from here, and the tests import `LEVER_SOURCE`,
`capture_lever`, and `_parse_lever` from `engine.providers.lever.capture`; a test
that needs to swap `capture_lever` monkeypatches it on this module.

LAZY-IMPORT INVARIANT (mirrors base.py): patchright is
imported lazily inside `_default_browser_page` (in the kernel), only when a real
capture runs, so importing this module -- and importing `engine.providers.lever`
-- stays browser-free for the daily poller. Tests drive the parse path over
fixture DOM through a fake browser/page factory and never touch patchright or the
network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from engine.kernel.capture_toolkit import (
    _TIMEOUT_MS,
    CaptureShapeError,
    _build_tree,
    _default_browser_page,
    _find_all,
    _first,
    _has_class,
    _node_text,
    _Node,
    _now,
)
from engine.kernel.contracts import (
    Field,
    FieldMap,
    Locator,
    _role_for_type,
)

LEVER_SOURCE = "lever_dom"

# Lever base-field `name` -> canonical human label (round-3 finding): the live
# apply page renders every base field TWICE, an invisible mirror carrying the
# true submission `name` with no label at all, plus a labeled visible twin.
# `_dedup_lever_base_fields` collapses each pair into one logical Field and
# uses this table to recover the human label when neither duplicate happens
# to carry one.
_LEVER_BASE_LABELS = {
    # Round-4 live finding: the apply page also carries a base `location`
    # input (autocomplete widget) whose visible twin is "Current location";
    # without this entry it escaped dedup and its label extraction grabbed
    # the widget's error text.
    "location": "Current location",
    "name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "org": "Current company",
    "urls[LinkedIn]": "LinkedIn URL",
    "urls[Twitter]": "Twitter URL",
    "urls[GitHub]": "GitHub URL",
    "urls[Portfolio]": "Portfolio URL",
    "urls[Other]": "Other URL",
    "resume": "Resume / CV",
}


# The two container classes the base/custom parse passes walk. A required control
# rendered OUTSIDE both is picked up by `_lever_stray_required_fields` (W5B-LEVER
# F3), never dropped.
_FIELD_CLASS = "application-field"
_CARD_CLASS = "application-question"
_KNOWN_CONTAINER_CLASSES = (_FIELD_CLASS, _CARD_CLASS)

# The CLICKABLE widget roles: a control the applicant TICKS rather than types
# into. Used ONLY as the top `_field_richness` tie-break, to state the invariant
# that a click widget must never be demoted to a phantom textbox by a duplicate
# parse (W5B-LEVER round 5).
#
# Deliberately NOT imported from the kernel's `_CLICK_HAZARD_ROLES`
# (fill_toolkit.py:499), even though the two sets are extensionally equal today.
# They are DIFFERENT pieces of knowledge and they are about to diverge: the
# kernel set is a HAND-OFF POLICY ("roles we defer to a human"), which the owner
# ruling of 2026-07-13 retires and W5.1c is expected to SHRINK; this set is a
# DOM FACT ("roles that are ticked, not typed"), which no policy can change.
# Importing the policy set here would silently stop protecting radios from
# demotion on the day W5.1c drops "radio" from the hand-off set. Two names, two
# lifetimes: not a DRY violation.
_WIDGET_ROLES = frozenset({"radio", "checkbox"})


def lever_apply_url(slug: str, job_id: str) -> str:
    return f"https://jobs.lever.co/{slug}/{job_id}/apply"


# -- the two key spaces a Lever field lives in (W5B-LEVER F2) -------------------
# Every parse path below records BOTH names a live control answers to:
#   * the HUMAN LABEL   -> `Field.label` (what an applicant reads: "Full name")
#   * the SUBMISSION NAME ATTRIBUTE -> `Field.key` (what the form posts: "name",
#     "cards[a1b2c3][field0]"), read here through `_control_name`.
# Recording the name attribute is load-bearing for completeness: the live DOM
# sweep names a required control by its accessible name, which FALLS BACK TO THE
# `name` ATTRIBUTE when the control carries no aria-label/placeholder
# (kernel/fill_toolkit.py:147-157). A capture that kept only the human label left
# the fill-side completeness check comparing two disjoint key spaces, so every
# required control read as a spurious dom_only gap (live agicap run, 2026-07-12).
# `Field.key` is the carrier because the FieldMap schema (kernel.contracts, a
# frozen contract) exposes no extra per-field slot and `key` is the attribute that
# survives to_dict/from_dict into the fill process; `fill._field_alias_names`
# reads the name attribute back off it.

def _control_name(control: "_Node") -> str:
    """The control's submission `name` attribute (the DOM key space)."""
    return (control.attrs.get("name") or "").strip()


def capture_lever(slug: str, job_id: str, browser_factory=None, *,
                  now: Callable[[], str] | None = None) -> FieldMap:
    """Capture one Lever posting's field map from the server-rendered apply DOM.

    Loads the apply page once and reads the rendered DOM via `page.content()`
    (server-rendered: no interception needed), then parses the fixed base fields
    plus the custom `.application-question` cards (`source="lever_dom"`). A
    read-only page load, so Lever's POST rate limits never apply.
    """
    factory = browser_factory or _default_browser_page
    url = lever_apply_url(slug, job_id)
    with factory() as page:
        page.goto(url, wait_until="domcontentloaded", timeout=_TIMEOUT_MS)
        html_source = page.content()
    return _parse_lever(html_source, slug, job_id, now=now)


# -- Lever DOM parse -----------------------------------------------------------

def _parse_lever(html_source: str, slug: str, job_id: str, *,
                 now: Callable[[], str] | None = None) -> FieldMap:
    tree = _build_tree(html_source)
    fields = _dedup_by_key(
        _lever_base_fields(tree)
        + _lever_custom_fields(tree, slug, job_id)
        + _lever_stray_required_fields(tree))
    if not fields:
        raise CaptureShapeError(
            f"lever: the apply page for {slug}/{job_id} rendered no recognizable "
            "form fields (no .application-field or .application-question blocks "
            "found); the DOM shape has drifted or the page did not load")
    return FieldMap(vendor="lever", posting_id=str(job_id),
                    captured_at=_now(now), fields=fields)


def _dedup_by_key(fields: list) -> list:
    """Final whole-map same-key collapse: two parse paths can BOTH emit a field
    for the same submission key, and this decides which parse SURVIVES.

    THE TIE-BREAK RULE, explicitly (W5B-LEVER round 5). The duplicate kept is the
    RICHER parse, `_field_richness` descending:

      1. a CLICK WIDGET role (radio/checkbox) outranks a typed role. A parse that
         saw the real widget must never lose to one that read the same control as
         a textbox: the loser's locator hunts a textbox that does not exist, so
         the field dies as a FILL ERROR, and `fill._is_control_field` never drives
         it because it keys off exactly this role.
      2. then the richer `_lever_type_rank` (options-bearing > file > boolean >
         textarea > plain text). `_control_type` returns `input_text` for ANY
         input it does not recognize, INCLUDING `type=hidden`, so a parse that
         read a hidden mirror is always rank 0 and always loses to the parse that
         read the real control.
      3. only when both are equal does document order decide (first wins).

    `required` is the OR across duplicates (never narrowed by the collapse) and an
    empty label is backfilled from a duplicate that carries one.

    WHY THIS IS NOT "keep the first". First-wins is what shipped, and it silently
    discarded the correct parse of the live agicap radio group (round 5 blocker):
    the base pass emitted a phantom textbox for the same key and, being first,
    won. An implicit ordering dependency is not a decision, it is an accident
    waiting for the next vendor shape.

    NOTE this is the SECOND line of defence, not the first: the base/custom
    collision on a card-wrapped control is now prevented at the source
    (`_card_owned_field_containers`). What still reaches here is the residual
    collision the base pass can produce ON ITS OWN, e.g. Lever's invisible base
    MIRROR inputs (round-3 finding) rendered as standalone `.application-field`
    blocks while the visible control lives in a card: the mirror is a hidden
    input, so it parses `input_text`/rank 0 and correctly loses to the card."""
    by_key: dict[str, object] = {}
    for fld in fields:
        kept = by_key.get(fld.key)
        if kept is None:
            by_key[fld.key] = fld
            continue
        winner, loser = ((fld, kept)
                         if _field_richness(fld) > _field_richness(kept)
                         else (kept, fld))
        winner.required = kept.required or fld.required
        if not winner.label and loser.label:
            winner.label = loser.label
        by_key[fld.key] = winner  # dict keeps the key's ORIGINAL position
    return list(by_key.values())


def _field_richness(fld: Field) -> tuple[int, int]:
    """How much a parse actually knows about the control behind a key: see the
    tie-break rule in `_dedup_by_key`. Higher wins."""
    return (1 if fld.locator.role in _WIDGET_ROLES else 0, _lever_type_rank(fld))


@dataclass
class _RawBaseField:
    """One `.application-field` container's parse, pre-dedup."""
    key: str
    label: str
    type: str
    required: bool
    options: list[str]
    container: "_Node"
    control: "_Node"


def _card_owned_field_containers(tree: "_Node") -> set[int]:
    """The `.application-field` containers NESTED INSIDE an `.application-question`
    card, which the base pass must NOT parse (W5B-LEVER round 5 blocker).

    THE LIVE SHAPE, re-derived 2026-07-13 over four postings (agicap 11/11, gopuff
    29/29, swile 20/20, nium 12/12 -- EVERY container, on every page): Lever wraps
    each control in a card that carries the human question in a SIBLING
    `.application-label` div, and the control itself in a nested
    `.application-field` div:

        <li class="application-question">
          <div>
            <div class="application-label"><div class="text">QUESTION<span class="required"/></div></div>
            <div class="application-field"><ul><li><label><input .../></label></li>...</ul></div>

    A nested `.application-field` is therefore the card's INPUT WRAPPER, not an
    independent base field, and the CARD is the semantic unit that owns it. Parsed
    as a base field it is structurally LABEL-BLIND: the question lives in a sibling
    of the container, outside it, so `_control_label` finds nothing and the label
    falls back to the container's own text, which is the control's OPTION WORDINGS
    MASHED TOGETHER ('Natif/Bilingue Professionnel Intermediaire Debutant' for the
    agicap radio group; 'Select... Alberta Alaska Alabama ...' for gopuff's state
    select). That phantom parse then collided with the correct card parse on the
    same submission key and, being first, WON: the live required radio group was
    captured as a `textbox` (round-5 blocker) and would have died as a fill error
    the moment an answer was resolved for it.

    Skipping these containers removes the duplicate AT THE SOURCE rather than
    arbitrating it afterwards, and it does so for EVERY control type at once (radio,
    select, checkbox, multi-checkbox, upload card), which a richness ranking over
    the two parses cannot: a card `<select>` parses to the same type, role AND
    options through both paths, differing ONLY in the label, so the two would TIE
    and the mashed label would survive.

    Nothing is lost by deferring to the card:
      * requiredness CANNOT be narrowed -- the card's subtree strictly CONTAINS the
        nested container's, so `_is_required(card, ...)` sees every `required` attr
        and every `.required` marker the base pass saw, plus the marker span in the
        card's own label (on live agicap it strictly gains: the resume card is
        required through its marker span while the nested container shows nothing);
      * the control is still captured -- by `_lever_custom_field`, WITH the question
        as its label and its options enumerated;
      * a `.application-field` that is NOT inside a card (the invisible base mirrors
        + labeled twins of the round-3 finding) is untouched and still parses here.
    """
    return {id(inner)
            for card in _find_all(tree, lambda n: _has_class(n, _CARD_CLASS))
            for inner in _find_all(card, lambda n: _has_class(n, _FIELD_CLASS))}


def _lever_base_fields(tree: "_Node") -> list[Field]:
    """The fixed base fields: every input inside a STANDALONE `.application-field`
    block (one the card pass does not already own: `_card_owned_field_containers`).

    The live apply page renders each base field TWICE (round-3 finding): an
    invisible mirror carrying the true submission `name` with no label, and a
    labeled visible twin. Both parse to the SAME `key` here, so
    `_dedup_lever_base_fields` collapses each duplicate pair back into ONE
    logical Field before returning.
    """
    card_owned = _card_owned_field_containers(tree)
    raw: list[_RawBaseField] = []
    for container in _find_all(tree, lambda n: _has_class(n, _FIELD_CLASS)):
        if id(container) in card_owned:
            continue
        control = _first(container, _is_form_control)
        if control is None:
            continue
        name = _control_name(control)
        if not name:
            continue
        raw.append(_RawBaseField(
            key=name,
            label=_control_label(container),
            type=_control_type(control),
            required=_is_required(container, control),
            options=_select_options(control),
            container=container,
            control=control,
        ))
    return _dedup_lever_base_fields(raw)


def _dedup_lever_base_fields(raw: list[_RawBaseField]) -> list[Field]:
    groups: dict[str, list[_RawBaseField]] = {}
    order: list[str] = []
    for item in raw:
        if item.key not in groups:
            order.append(item.key)
        groups.setdefault(item.key, []).append(item)
    _merge_lever_groups_by_normalized_label(groups, order)
    fields: list[Field] = []
    for key in order:
        items = groups.get(key)
        if items is None:
            continue  # merged away into another key's group
        fields.append(_merge_lever_base_group(key, items))
    return fields


def _merge_lever_groups_by_normalized_label(groups: dict, order: list[str]) -> None:
    """Fallback normalized-label match: collapse two DIFFERENT keys into one
    group when both are label-bearing singletons whose labels normalize to
    the same slug. Covers a base/labeled pair whose visible twin renders
    under a different `name` than its hidden mirror -- not observed live yet
    for Lever, kept defensive since the same duplication shape recurred once
    already (round-2 Ashby shape drift) and could recur here too."""
    seen: dict[str, str] = {}
    for key in list(order):
        items = groups.get(key)
        if items is None or len(items) != 1 or not items[0].label:
            continue
        slug = _slug(items[0].label)
        primary = seen.get(slug)
        if primary is None:
            seen[slug] = key
        elif primary != key:
            groups[primary].extend(items)
            del groups[key]


def _merge_lever_base_group(key: str, items: list[_RawBaseField]) -> Field:
    label = _pick_lever_label(key, items)
    required = any(item.required for item in items)
    field_type, options = _richer_lever_type(items)
    return Field(
        key=key,
        label=label,
        type=field_type,
        required=required,
        options=options,
        source=LEVER_SOURCE,
        # `_control_role`, never `_role_for_type`: this is the THIRD and last
        # emission site, and until round 7 it was the one that still derived the
        # role from the TYPE. See `_control_role` for the invariant, and
        # `_base_group_control` for which of a group's controls answers it.
        locator=Locator(role=_control_role(_base_group_control(items), field_type),
                        name=label),
        step_index=0,
        conditional_on=None,
    )


def _base_group_control(items: list[_RawBaseField]) -> "_Node":
    """The APPLICANT-FACING control a base group's locator must answer to: the
    click widget if the group has one, else the first parse's control (W5B-LEVER
    round 7).

    This is the SAME choice `_lever_custom_field` makes when it picks its
    `primary` (radios first, then checkboxes, then the plain control), made here
    so that every emission site hands `_control_role` the control the applicant
    actually OPERATES rather than whichever one happened to parse first.

    WHY NOT SIMPLY `items[0].control`. A base group is the duplicate parses of ONE
    submission key, and Lever's live shape (round-3 finding) puts an INVISIBLE
    MIRROR input first in document order, ahead of its visible twin. Reading the
    role off `items[0]` unconditionally would therefore read it off a
    `type=hidden` input, which is neither a radio nor a checkbox, so it would fall
    straight back to `_role_for_type` -- the exact type-derivation the invariant
    forbids, reintroduced through the back door.

    The non-click fallthrough IS safe on `items[0]`, because `_control_role` then
    answers from `field_type`, and `field_type` is already the RICHEST parse's
    (`_richer_lever_type`): a `<select>` twin behind a hidden mirror still yields
    `multi_value_single_select` -> `combobox`. Only a CLICK widget needs the
    control itself, because no type in the frozen vocabulary can name one."""
    for item in items:
        if _is_radio(item.control):
            return item.control
    for item in items:
        if _is_checkbox(item.control):
            return item.control
    return items[0].control


def _pick_lever_label(key: str, items: list[_RawBaseField]) -> str:
    """Keep the human label: the first duplicate that actually carries one,
    else the deterministic base/label table, else the harder-extraction
    fallback chain (never an empty string, per round-3 item 2)."""
    for item in items:
        if item.label:
            return item.label
    if key in _LEVER_BASE_LABELS:
        return _LEVER_BASE_LABELS[key]
    return _resolve_field_label("", items[0].container, items[0].control, key)


def _richer_lever_type(items: list[_RawBaseField]) -> tuple[str, list[str]]:
    """Type from the richer source: an option-carrying or more specific
    control (e.g. a `type=file` upload widget) outranks the plain-text
    default that a hidden mirror typically parses as."""
    best = items[0]
    best_rank = _lever_type_rank(best)
    for item in items[1:]:
        rank = _lever_type_rank(item)
        if rank > best_rank:
            best, best_rank = item, rank
    return best.type, best.options


def _lever_type_rank(item: _RawBaseField) -> int:
    if item.options:
        return 4
    if item.type == "input_file":
        return 3
    if item.type == "boolean":
        return 2
    if item.type == "textarea":
        return 1
    return 0


def _card_controls(card: "_Node") -> list["_Node"]:
    """The card's APPLICANT-FACING controls: every form control in it that is not a
    hidden input. Lever pairs each consent checkbox with a hidden twin of the same
    submission name carrying the unticked value "0" (round-3 finding), and a hidden
    input is never something an applicant answers -- so a card whose ONLY controls
    are hidden asks nothing, exactly like a card with no control at all."""
    return [n for n in _find_all(card, _is_form_control)
            if (n.attrs.get("type") or "").lower() != "hidden"]


def _lever_custom_fields(tree: "_Node", slug: str, job_id: str) -> list[Field]:
    """One Field per `.application-question` card that actually ASKS something.

    A card with NO applicant-facing control is FURNITURE, not a question, and is
    SKIPPED (W5B-LEVER round 8 blocker). Until this wave such a card raised
    `CaptureShapeError` and killed the WHOLE capture, so no FieldMap was produced at
    all and the apply pipeline was dead before the fill was ever reached -- on THREE
    of the four live postings. Re-derived from the live DOM 2026-07-14, both shapes
    verbatim (they are pinned in the fixture and in
    `test_lever_control_less_question_card_is_furniture_not_a_capture_error`):

      1. the "Apply with LinkedIn" OAuth widget (live on nium, swile):
         `<li class="application-question awli-application-row">` whose
         `.application-label` reads "LinkedIn profile" and whose `.application-field`
         holds a `<button type="button">`, no form control;
      2. the legitimate-interest privacy notice (live on gopuff, swile):
         `<li class="application-question">` holding only
         `<p data-qa="legitimate-interest-copy">`; no label, no control.

    Neither asks the applicant for a value, and NEITHER CAN BE ANSWERED: there is no
    control to type into, tick, or select from. Skipping loses nothing and hides
    nothing -- a card that DOES carry a control is still parsed here, a REQUIRED
    control the two container passes miss is still swept in by
    `_lever_stray_required_fields`, and the live DOM sweep at fill time is still the
    authoritative required-field oracle (`fill._sweep_gaps`), so a genuinely required
    control that this pass ever skipped would still force NOT_COMPLETE rather than
    pass silently. A page that yields NO fields at all still raises (`_parse_lever`).
    """
    fields: list[Field] = []
    for card in _find_all(tree, lambda n: _has_class(n, _CARD_CLASS)):
        controls = _card_controls(card)
        if not controls:
            continue                      # FURNITURE: an unanswerable card, not a question
        for container, group in _name_groups(card, controls):
            fields.append(_lever_custom_field(container, group))
    return fields


def _name_groups(card: "_Node", controls: list["_Node"]) -> list[tuple["_Node", list["_Node"]]]:
    """A card's controls, split into ONE GROUP PER DISTINCT SUBMISSION NAME when the card
    is a set of checkboxes posting under SEVERAL names (W5B-LEVER round 10).

    The checkbox-GROUP arm below reads a multi-checkbox card as ONE question whose N
    options SHARE one submission name (live: agicap `pronouns`). Live swile falsifies that
    premise: ONE `.application-question` card holds TWO consent checkboxes posting under
    DIFFERENT names (`consent[store]`, REQUIRED, and `consent[marketing]`, optional), each
    in its own `<label>` with its own hidden `0` twin. Read as one group, that card emitted
    ONE Field keyed by the FIRST checkbox and the second control was SUBTRACTED from the
    FieldMap in silence -- an applicant-facing control the fill could never even hand off.

    Several submission names in one card is several QUESTIONS: one Field each, each read
    from the control's OWN wrapper (`_control_wrapper`), because the CARD's text is every
    control's wording glued together and would label both fields identically.

    Deliberately NARROW: it splits ONLY a card whose applicant-facing controls are ALL
    checkboxes AND post under >=2 distinct names. A genuine checkbox group (one name) is
    untouched, and so is every other card shape."""
    checkboxes = [n for n in controls
                  if (n.attrs.get("type") or "").lower() == "checkbox"]
    names = {_control_name(cb) for cb in checkboxes}
    if len(checkboxes) != len(controls) or len(names) < 2:
        return [(card, controls)]
    groups: dict[str, list["_Node"]] = {}
    for checkbox in checkboxes:
        groups.setdefault(_control_name(checkbox), []).append(checkbox)
    return [(_control_wrapper(card, group[0]), group) for group in groups.values()]


def _control_wrapper(card: "_Node", control: "_Node") -> "_Node":
    """The innermost `<label>`/`<li>` inside `card` that holds `control` -- the element
    whose text is THIS control's wording, where the card's own text is the wording of
    every control in it. Falls back to the card when the control has no such wrapper."""
    holders = [n for n in _find_all(card, lambda n: n.tag in ("label", "li"))
               if any(c is control for c in _find_all(n, _is_form_control))]
    if not holders:
        return card
    return min(holders, key=lambda n: (len(_find_all(n, _is_form_control)),
                                       0 if n.tag == "label" else 1))


def _lever_custom_field(card: "_Node", controls: list["_Node"]) -> Field:
    raw_label = _control_label(card)
    checkboxes = [n for n in controls
                  if (n.attrs.get("type") or "").lower() == "checkbox"]
    radios = [n for n in controls if _is_radio(n)]
    if radios:
        # A RADIO GROUP: one question, N mutually exclusive options sharing one
        # submission name (W5B-LEVER round 3; the live agicap posting asks its
        # language-level question this way). See `_control_role` for why the
        # locator role, not the type, is what makes this safe.
        field_type = "multi_value_single_select"
        options = [_radio_option_label(card, radio) for radio in radios]
        primary = radios[0]
        key = _control_name(primary) or _slug(raw_label)
    elif len(checkboxes) > 1:
        # A CHECKBOX GROUP: one question, N independently tickable options sharing
        # one submission name (live: agicap `pronouns`, gopuff's required
        # `cards[bf8587da-...][field0]`, swile's required `consent[store]`). The type
        # says the DATA is a multi-choice; `_control_role` says the WIDGET is ticked,
        # not selected from -- see it for why the role, not the type, is what keeps
        # this field out of a phantom `listbox` locator.
        field_type = "multi_value_multi_select"
        options = [_checkbox_label(cb) for cb in checkboxes]
        primary = checkboxes[0]
        key = _control_name(primary) or _slug(raw_label)
    else:
        primary = controls[0]
        field_type = _control_type(primary)
        options = _select_options(primary)
        key = _control_name(primary) or _slug(raw_label)
    label = _resolve_field_label(raw_label, card, primary, key)
    return Field(
        key=key,
        label=label,
        type=field_type,
        # `primary`, NOT `controls[0]` (W5B-LEVER round 7). The Field this card
        # emits IS the primary widget: its key, its locator and its options all come
        # from `primary`, so its requiredness must describe the SAME control. Reading
        # it off `controls[0]` reports the requiredness of a DIFFERENT control the
        # moment a card's first control is not its primary widget -- a radio group
        # preceded by an optional free-text "other" box captures the REQUIRED group
        # as OPTIONAL, because the text box carries no `required` and the card has no
        # `.required` marker span to OR back in. The two coincide on every live Lever
        # card today (the group's own controls are the only non-hidden ones in it, so
        # `controls[0] is primary`), which is why the choice was arbitrary and
        # untested rather than wrong; it is now the coherent one.
        required=_is_required(card, primary),
        options=options,
        source=LEVER_SOURCE,
        locator=Locator(role=_control_role(primary, field_type), name=label),
        step_index=0,
        conditional_on=None,
    )


def _lever_stray_required_fields(tree: "_Node") -> list[Field]:
    """Required controls NEITHER container pass above can see (W5B-LEVER F3).

    `_lever_base_fields` only walks `.application-field` blocks and
    `_lever_custom_fields` only walks `.application-question` cards, so a required
    control rendered outside BOTH (a bare form input, a vendor widget block) never
    entered the field map at all -- while the live DOM sweep, which scans the
    WHOLE page for `[required], [aria-required='true']`, still requires it. The
    result was a permanent dom_only gap forcing NOT_COMPLETE with no captured
    field to answer it. This third pass sweeps the whole tree for the same
    required signals the sweep reads (native `required` OR `aria-required="true"`,
    via `_control_marks_required`) and enumerates every such control the two
    container passes did not already claim, so the capture covers the same
    required set the sweep does.

    Deliberately NARROW: only REQUIRED controls are swept in (an optional stray
    control stays out of the field map, exactly as before), hidden inputs are
    skipped (never applicant-facing), and `_dedup_by_key` collapses anything that
    also parsed through a container pass. The sweep itself is untouched and stays
    authoritative: a required control this pass ALSO misses still surfaces as a
    dom_only gap and still forces NOT_COMPLETE."""
    claimed = {id(control)
               for container in _find_all(tree, _is_known_container)
               for control in _find_all(container, _is_form_control)}
    fields: list[Field] = []
    for control in _find_all(tree, _is_form_control):
        if id(control) in claimed:
            continue
        if (control.attrs.get("type") or "").lower() == "hidden":
            continue
        if not _control_marks_required(control):
            continue
        key = _control_name(control)
        label = _stray_label(tree, control, key)
        field_type = _control_type(control)
        fields.append(Field(
            key=key or _slug(label),
            label=label,
            type=field_type,
            required=True,
            options=_select_options(control),
            source=LEVER_SOURCE,
            # `_control_role` (not `_role_for_type`): a required stray RADIO must be
            # captured with its real role too, or it dies as a fill error (a
            # nonexistent textbox is hunted), exactly as the card radios did. How it
            # is later filled is deferred to the W5.1c click-policy wave.
            locator=Locator(role=_control_role(control, field_type), name=label),
            step_index=0,
            conditional_on=None,
        ))
    return fields


def _is_known_container(node: "_Node") -> bool:
    return any(_has_class(node, cls) for cls in _KNOWN_CONTAINER_CLASSES)


def _stray_label(tree: "_Node", control: "_Node", key: str) -> str:
    """The best available name for a control outside the known containers: its own
    `<label for=...>` element, then `aria-label`, then the submission `name`
    attribute last (never an empty label)."""
    control_id = (control.attrs.get("id") or "").strip()
    if control_id:
        label_node = _first(tree, lambda n: (
            n.tag == "label"
            and (n.attrs.get("for") or "").strip() == control_id))
        if label_node is not None:
            text = _node_text(label_node, exclude_cls="required")
            if text:
                return text
    aria = (control.attrs.get("aria-label") or "").strip()
    if aria:
        return aria
    return key or "(unlabeled)"


def _resolve_field_label(label: str, container: "_Node", control: "_Node",
                         key: str) -> str:
    """Harder label-extraction fallback chain (round-3 item 2): a captured
    field must never carry an empty label. Tried in order: the caller's own
    `.application-label` read (`label`, already attempted before this is
    called), `aria-label`, `placeholder`, the enclosing element's own trimmed
    text (e.g. a consent checkbox whose wording sits inline rather than in a
    dedicated label element), and finally the field's key as a last resort."""
    if label:
        return label
    aria = (control.attrs.get("aria-label") or "").strip()
    if aria:
        return aria
    placeholder = (control.attrs.get("placeholder") or "").strip()
    if placeholder:
        return placeholder
    enclosing = _node_text(container, exclude_cls="required")
    if enclosing:
        return enclosing
    return f"(unlabeled: {key})" if key else "(unlabeled)"


def _is_form_control(node: "_Node") -> bool:
    return node.tag in ("input", "textarea", "select")


def _is_radio(control: "_Node") -> bool:
    return (control.attrs.get("type") or "").lower() == "radio"


def _is_checkbox(control: "_Node") -> bool:
    return (control.attrs.get("type") or "").lower() == "checkbox"


def _control_role(control: "_Node", field_type: str) -> str:
    """The ARIA role for a control's locator, and THE INVARIANT IT ENFORCES: a
    control the applicant TICKS is never captured with a role the fill would TYPE
    into or SELECT from. A radio is a "radio", a checkbox is a "checkbox", and only
    a non-click control falls through to its type's canonical role (W5B-LEVER round
    3 for the radio, round 6 for the checkbox GROUP).

    THE INVARIANT IS STATED HERE ONCE AND HOLDS AT EVERY EMISSION SITE. A locator
    role is emitted in exactly THREE places, and all three route through this
    function; `_role_for_type` has exactly ONE caller in this module, the
    fallthrough below:

      1. `_lever_custom_field`   -> `_control_role(primary, ...)`
      2. `_lever_stray_required_fields` -> `_control_role(control, ...)`
      3. `_merge_lever_base_group` -> `_control_role(_base_group_control(items), ...)`

    Site 3 was the LAST hold-out and it is why this is spelled out. Until round 7 it
    read `_role_for_type(field_type)`, so the invariant was declared universally and
    enforced at two sites out of three. That is not a near-miss: it is the SAME
    defect, at its third site, of the bug that cost this wave two rounds (the agicap
    radio in round 5, the checkbox GROUP in round 6, both "role derived from type").
    It was harmless only by luck -- no live Lever page routes a click widget through
    the base pass, because every live `.application-field` is card-owned -- and luck
    is not a design. Three sites enforcing one invariant three different ways is how
    the third one gets forgotten; there is now one way.

    THE ROLE IS A DOM FACT, THE TYPE IS A DATA FACT, and they are different pieces
    of knowledge (the same split `_WIDGET_ROLES` above is drawn on). `_role_for_type`
    can only answer from the TYPE, so it necessarily gets a click widget wrong:
      * a RADIO group is typed `multi_value_single_select` -> "combobox";
      * a CHECKBOX group is typed `multi_value_multi_select` -> "listbox"
        (contracts.py:78).
    Both are roles NO element on a Lever page has. The type is right in both cases
    (a radio group IS one choice among enumerated options; a checkbox group IS
    several), and it stays: the kernel's type vocabulary is FROZEN, carries no
    radio/checkbox member, and the select types are what let the resolve layer
    render the chosen option(s). Only the ROLE is corrected here.

    WHY THE ROLE IS LOAD-BEARING AND NOT COSMETIC. `fill._is_control_field` keys
    off exactly this role: {"checkbox", "radio"}, so a click widget captured with a
    TYPED role is NOT routed through `drive_control`. The fill instead tries the
    NATIVE text/select path, `base._locate` builds `get_by_role(<phantom role>,
    name=...)`, that resolves to ZERO elements on the live page, and the field dies
    as a FILL ERROR -- unfillable and unexplained. That was the round-5 blocker for
    the agicap RADIO group, and this wave then reproduced it ONE CONTROL TYPE OVER:
    typing the checkbox group richly (round 5) moved it off role "checkbox" (which
    the single-checkbox `boolean` type had given it by accident) onto "listbox", so a
    group that USED to be safely handed off started being driven into a locator miss.
    Re-derived live 2026-07-13: agicap `pronouns` (11 checkboxes), gopuff
    `cards[bf8587da-...][field0]` (5, REQUIRED) and `surveysResponses[44bc1677-...]`
    (8), swile `surveysResponses[4e73d71a-...]` (8) and `consent[store]` (2,
    REQUIRED); zero `[role=listbox]` and zero `<select multiple>` on any of those
    pages. Keying the role off the CONTROL, not the type, is what makes the invariant
    hold for every click widget at once instead of one special case at a time.

    CURRENT downstream behaviour (W5.1c, landed): `fill._drive_control_field` drives
    a correctly captured click widget through the kernel's
    `control_toolkit.drive_control` (`.check()`/`.uncheck()`, readback-confirmed),
    so it counts as filled the moment the page confirms it, and only an
    unconfirmed or driveless control still blocks. This module changes no kernel
    code and no fill code; it only makes the capture honest so the fill has a real
    role to act on."""
    if _is_radio(control):
        return "radio"
    if _is_checkbox(control):
        return "checkbox"
    return _role_for_type(field_type)


def _radio_option_label(card: "_Node", radio: "_Node") -> str:
    """One radio's option wording: its own `<label for=...>`, else the `<label>`
    element wrapping it, else its `value` attribute. The option texts become the
    field's `options`; the QUESTION (the card's `.application-label`) stays the
    field's label, never a mash of the option texts."""
    radio_id = (radio.attrs.get("id") or "").strip()
    if radio_id:
        node = _first(card, lambda n: (
            n.tag == "label"
            and (n.attrs.get("for") or "").strip() == radio_id))
        if node is not None:
            text = _node_text(node, exclude_cls="required")
            if text:
                return text
    holder = _first(card, lambda n: (
        n.tag == "label"
        and any(control is radio for control in _find_all(n, _is_form_control))))
    if holder is not None:
        text = _node_text(holder, exclude_cls="required")
        if text:
            return text
    return (radio.attrs.get("value") or "").strip()


def _control_type(control: "_Node") -> str:
    if control.tag == "textarea":
        return "textarea"
    if control.tag == "select":
        return "multi_value_single_select"
    input_type = (control.attrs.get("type") or "text").lower()
    if input_type == "file":
        return "input_file"
    if input_type == "checkbox":
        return "boolean"
    return "input_text"


def _control_label(container: "_Node") -> str:
    label_node = _first(container, lambda n: _has_class(n, "application-label"))
    if label_node is None:
        return ""
    return _node_text(label_node, exclude_cls="required")


def _is_required(container: "_Node", control: "_Node") -> bool:
    if control is not None and _control_marks_required(control):
        return True
    return _first(container, lambda n: _has_class(n, "required")) is not None


def _control_marks_required(control: "_Node") -> bool:
    """A control that declares ITSELF required: the native `required` attribute,
    or `aria-required="true"` (W5B-LEVER F3).

    The ARIA form is not cosmetic: the live DOM sweep's required selector is
    `[required], [aria-required='true']` (kernel/fill_toolkit.py:58), so a control
    marked required ONLY through the ARIA attribute was required by the sweep but
    captured OPTIONAL here -- the field then had no requiredness to reconcile
    against and the sweep entry became an unanswerable gap."""
    if "required" in control.attrs:
        return True
    return (control.attrs.get("aria-required") or "").strip().lower() == "true"


def _select_options(control: "_Node") -> list[str]:
    if control.tag != "select":
        return []
    options: list[str] = []
    for option in _find_all(control, lambda n: n.tag == "option"):
        value = option.attrs.get("value", "")
        text = _node_text(option)
        if value == "" and (not text or text.lower().startswith("select")):
            continue  # the "Select..." placeholder is not a real option
        # ...but an EMPTY VALUE ALONE IS NOT A PLACEHOLDER. An option that carries
        # real wording ("None of the above", "Prefer not to say") is a real answer
        # the applicant can choose, and dropping it would silently shrink the
        # choices the resolve layer may render. BOTH halves are pinned (W5B-LEVER
        # round 7): the placeholder goes, a genuine empty-value option stays.
        options.append(text or value)
    return options


def _checkbox_label(checkbox: "_Node") -> str:
    return checkbox.attrs.get("value", "") or checkbox.attrs.get("name", "")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_") or "field"
