"""Read a form into an inventory of fields, and decide what not to touch.

Split out from the browser deliberately: parsing a form is a pure function over
HTML, so the adapters that map your details onto real selectors can be tested
against captured pages instead of against a running Chromium. The harness does
the same parse on `page.content()` and executes what the adapter returns.

The reason this file exists at all is `tests/fixtures/ats/greenhouse.html`:

    <input required tabindex="-1" aria-hidden="true" value=""/>

Two of those sit in a real Greenhouse form. They are required, invisible and
must stay empty -- a honeypot. An adapter that filled every required input
would fill them, and announce itself as a bot on every application it made.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from selectolax.parser import HTMLParser


@dataclass(frozen=True)
class Field:
    tag: str                 # input | select | textarea
    name: str = ""
    id: str = ""
    type: str = "text"
    aria_label: str = ""
    data_qa: str = ""
    required: bool = False
    hidden: bool = False
    honeypot: bool = False
    accept: str = ""
    label: str = ""          # text of <label for=...>, which is often the only
                             # thing that says what a uuid-named field is asking

    @property
    def selector(self) -> str:
        """A CSS selector the harness can act on, preferring the most stable
        hook the page offers. data-qa is a deliberate test hook and outlives a
        redesign; an id outlives a class; a name is the wire contract."""
        if self.data_qa:
            return f'[data-qa="{self.data_qa}"]'
        if self.id:
            return f"#{self.id}"
        if self.name:
            return f'{self.tag}[name="{self.name}"]'
        return ""

    @property
    def key(self) -> str:
        """What an adapter matches on."""
        return self.name or self.id

    @property
    def prompt(self) -> str:
        """The human-readable question, best source first. Ashby names every
        custom field with a uuid, so the <label for=...> is the only thing
        that says what is being asked."""
        return (self.label or self.aria_label or "").strip()

    @property
    def is_choice(self) -> bool:
        return self.type in ("radio", "checkbox") or self.tag == "select"


def _attr(node, key: str) -> str:
    v = node.attributes.get(key)
    return "" if v is None else str(v)


# The token field a captcha writes its result into. Present on real forms as
# an ordinary textarea or hidden input, which means an adapter looking for
# "somewhere to put an answer" will happily find it:
#     Ashby   <textarea name="g-recaptcha-response">
#     Lever   <input name="h-captcha-response">
# Writing anything here is at best nonsense and at worst forging a captcha
# result, which is the one thing the brief is explicit about not doing.
CAPTCHA_TOKEN_FIELDS = ("g-recaptcha-response", "h-captcha-response",
                        "cf-turnstile-response", "captcha")


def is_captcha_token(field: "Field") -> bool:
    key = f"{field.name} {field.id}".lower()
    return any(tok in key for tok in CAPTCHA_TOKEN_FIELDS)


def _is_honeypot(node) -> bool:
    """Required, invisible, and empty. Any one of those is ordinary; together
    they are a trap, because nothing a person can see is being asked for."""
    hidden = (_attr(node, "aria-hidden").lower() == "true"
              or _attr(node, "tabindex") == "-1"
              or "display:none" in _attr(node, "style").replace(" ", ""))
    required = "required" in node.attributes
    has_label = bool(_attr(node, "aria-label") or _attr(node, "name")
                     or _attr(node, "id") or _attr(node, "placeholder"))
    return hidden and required and not has_label


def _label_for(tree, node) -> str:
    fid = _attr(node, "id")
    if not fid:
        return _attr(node, "data-label")
    lab = tree.css_first(f'label[for="{fid}"]')
    if lab is not None:
        return " ".join(lab.text().split())
    # Fixtures capture the resolved label as data-label, because a <label> that
    # lives elsewhere in the document does not survive being trimmed.
    return _attr(node, "data-label")


def parse(html: str, *, form_selector: str = "form") -> list[Field]:
    """Every fillable field in the first matching form, honeypots flagged.

    Falls back to the whole document when no form element is present: Ashby
    renders its form client-side and there is no <form> in the served HTML at
    all, so a parser that required one would return nothing for it forever.
    """
    tree = HTMLParser(html or "")
    root = tree.css_first(form_selector) or tree.body or tree
    if root is None:
        return []

    out: list[Field] = []
    for node in root.css("input, select, textarea"):
        tag = node.tag
        ftype = (_attr(node, "type") or ("text" if tag == "input" else tag)).lower()
        if ftype in ("submit", "button", "image", "reset"):
            continue
        out.append(Field(
            tag=tag,
            name=_attr(node, "name"),
            id=_attr(node, "id"),
            type=ftype,
            aria_label=_attr(node, "aria-label"),
            data_qa=_attr(node, "data-qa"),
            required="required" in node.attributes,
            hidden=ftype == "hidden",
            honeypot=_is_honeypot(node),
            accept=_attr(node, "accept"),
            label=_label_for(tree, node),
        ))
    return out


def fillable(fields: Iterable[Field]) -> list[Field]:
    """Everything an adapter may touch.

    Honeypots are excluded because filling them is the trap. Hidden inputs are
    excluded because they carry the site's own state -- Lever's accountId,
    origin, referer, resumeStorageId -- and writing to them is either
    meaningless or actively breaks the submission. Captcha token fields are
    excluded because writing a value there is forging a captcha result.
    """
    return [f for f in fields
            if not f.honeypot and not f.hidden and f.selector
            and not is_captcha_token(f)]


@dataclass(frozen=True)
class FillAction:
    selector: str
    kind: str                # text | file | select | check
    value: str
    field_key: str = ""


@dataclass(frozen=True)
class FillPlan:
    actions: Sequence[FillAction]
    unmapped: Sequence[str]          # required fields nothing could fill
    skipped_honeypots: int = 0
    # Choice fields left blank. A required one is already in `unmapped`; these
    # are the optional ones, reported because "optional" is the form's opinion
    # and not necessarily yours -- the rendered Ashby form asks work rights as
    # an optional radio group, and submitting with it blank is a real omission
    # rather than a tidy default.
    choices_left: Sequence[str] = ()

    @property
    def complete(self) -> bool:
        """A plan with an unfillable required field must not be submitted: the
        form would be rejected, or worse, accepted with a blank answer.

        A plan with *no* actions is not complete either. Ashby's served HTML
        contains no form at all, so parsing it yields nothing and an
        emptiness-blind check called that a finished plan -- one step from
        submitting a blank application.
        """
        return bool(self.actions) and not self.unmapped
