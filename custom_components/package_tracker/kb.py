"""Shipping-email knowledge base: senders, tracking numbers, status and delivery dates.

Pure Python (no Home Assistant imports) so it can be tested against saved .eml files. Anything
that needs "today" or a time zone takes it as an argument.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import datetime as dt
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from enum import IntEnum
import html as _html
import logging
import re
from typing import Any
import urllib.parse

from .address import Place, ship_to

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check digits
# ---------------------------------------------------------------------------


def _mod10_31_from_right(body: str) -> int:
    """GS1-style check digit: weights 3,1,3,1... from the right (USPS Pub 199)."""
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def usps_ok(n: str) -> bool:
    return n.isdigit() and len(n) >= 20 and _mod10_31_from_right(n[:-1]) == int(n[-1])


def ups_1z_ok(n: str) -> bool:
    """1Z + 15 alnum + check. Letters map (ord-63)%10; odd positions x1, even x2."""
    if not re.fullmatch(r"1Z[0-9A-Z]{15}[0-9]", n):
        return False
    total = 0
    for i, c in enumerate(n[2:17]):
        v = int(c) if c.isdigit() else (ord(c) - 63) % 10
        total += v * (2 if i % 2 else 1)
    return (10 - total % 10) % 10 == int(n[17])


def fedex12_ok(n: str) -> bool:
    """FedEx Express 12: weights 3,1,7 repeating over the first 11, (sum%11)%10."""
    if not re.fullmatch(r"\d{12}", n):
        return False
    weights = (3, 1, 7, 3, 1, 7, 3, 1, 7, 3, 1)
    total = sum(int(a) * b for a, b in zip(n[:11], weights, strict=True))
    return total % 11 % 10 == int(n[11])


def fedex15_ok(n: str) -> bool:
    return bool(re.fullmatch(r"\d{15}", n)) and _mod10_31_from_right(n[:14]) == int(n[14])


def fedex96_ok(n: str) -> bool:
    return bool(re.fullmatch(r"96\d{20}", n)) and _mod10_31_from_right(n[7:21]) == int(n[21])


def fedex34_ok(n: str) -> bool:
    if not re.fullmatch(r"\d{34}", n) or n.startswith("420"):
        return False
    weights = (1, 7, 3, 1, 7, 3, 1, 7, 3, 1, 7, 3, 1)
    total = sum(int(a) * b for a, b in zip(n[20:33], weights, strict=True))
    return total % 11 % 10 == int(n[33])


def ontrac_ok(n: str) -> bool:
    m = re.fullmatch(r"([CD])(\d{13})(\d)", n)
    if not m:
        return False
    seq = ("4" if m[1] == "C" else "5") + m[2]
    total = sum(int(c) * (2 if i % 2 else 1) for i, c in enumerate(seq))
    return (10 - total % 10) % 10 == int(m[3])


def dhl10_ok(n: str) -> bool:
    return bool(re.fullmatch(r"\d{10}", n)) and int(n[:-1]) % 7 == int(n[-1])


def s10_ok(n: str) -> bool:
    m = re.fullmatch(r"([A-Z]{2})(\d{8})(\d)([A-Z]{2})", n)
    if not m:
        return False
    weights = (8, 6, 4, 2, 3, 5, 9, 7)
    r = 11 - sum(int(a) * b for a, b in zip(m[2], weights, strict=True)) % 11
    check = {10: 0, 11: 5}.get(r, r)
    return check == int(m[3])


# ---------------------------------------------------------------------------
# Tracking-number patterns. Strong ones are accepted from free text when the
# check digit passes; weak ones only when the mail is from that carrier or the
# number sits in a "tracking number" label or a carrier tracking URL.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrkPattern:
    carrier: str
    kind: str
    regex: re.Pattern[str]
    check: Callable[[str], bool] | None
    strong: bool


# Tracking numbers must not run into neighbouring letters or digits.
_START = r"(?<![0-9A-Za-z])"
_END = r"(?![0-9A-Za-z])"
TRACKING_PATTERNS: tuple[TrkPattern, ...] = (
    TrkPattern("ups", "1Z", re.compile(_START + r"(1Z[0-9A-Z]{16})" + _END), ups_1z_ok, True),
    # USPS IMpb: 92-95 + 20 or 24 digits; an optional 420+ZIP5/ZIP9 routing prefix is skipped.
    TrkPattern(
        "usps",
        "impb",
        re.compile(_START + r"(?:420\d{5}(?:\d{4})?)?(9[2-5]\d{20}(?:\d{4})?)" + _END),
        usps_ok,
        True,
    ),
    TrkPattern("usps", "legacy22", re.compile(_START + r"(91\d{20})" + _END), usps_ok, False),
    TrkPattern("usps", "s10", re.compile(_START + r"([A-Z]{2}\d{9}US)" + _END), s10_ok, True),
    TrkPattern("usps", "usps20", re.compile(_START + r"(\d{20})" + _END), usps_ok, False),
    TrkPattern("fedex", "ground96", re.compile(_START + r"(96\d{20})" + _END), fedex96_ok, True),
    TrkPattern("fedex", "34", re.compile(_START + r"(\d{34})" + _END), fedex34_ok, False),
    TrkPattern("fedex", "ground15", re.compile(_START + r"(\d{15})" + _END), fedex15_ok, False),
    TrkPattern("fedex", "express12", re.compile(_START + r"(\d{12})" + _END), fedex12_ok, False),
    TrkPattern("amazon", "TBA", re.compile(_START + r"(TB[ACM]\d{12})" + _END), None, True),
    TrkPattern("ontrac", "CD", re.compile(_START + r"([CD]\d{14})" + _END), ontrac_ok, True),
    TrkPattern("ontrac", "1LS", re.compile(_START + r"(1LS[0-9A-Z]{12})" + _END), None, False),
    TrkPattern("dhl", "ecommerce", re.compile(_START + r"(GM[0-9A-Z]{10,39})" + _END), None, False),
    TrkPattern("dhl", "express10", re.compile(_START + r"(\d{10})" + _END), dhl10_ok, False),
    TrkPattern("gofo", "GFUS", re.compile(_START + r"(GFUS\d{14})" + _END), None, True),
)

AMAZON_ORDER_RE = re.compile(r"(?<!\d)(\d{3}-\d{7}-\d{7})(?!\d)")
WALMART_ORDER_RE = re.compile(r"(?<!\d)(\d{7}-\d{7,8})(?!\d)")
ETSY_ORDER_RE = re.compile(r"(?i)(?:Receipt|Order)\s*#\s*(\d{9,11})\b")
TRACKING_LABEL_RE = re.compile(
    r"(?i)(?:(?P<carrier>[A-Za-z][A-Za-z.&-]{1,15}(?: [A-Za-z][A-Za-z.&-]{1,15})?)\s+)?"
    r"tracking\s*(?:number\b|#|no\.|no\b|id\b)\s*:?\s*#?\s*"
    # The value must contain a digit: "Tracking number available soon" is not a number.
    r"(?P<num>(?:[0-9]{4} ){2,8}[0-9]{1,6}|(?=[A-Z]*[0-9])[A-Z0-9]{8,40})(?![A-Za-z0-9])"
)
KNOWN_CARRIERS = {
    "usps": "usps",
    "ups": "ups",
    "fedex": "fedex",
    "dhl": "dhl",
    "ontrac": "ontrac",
    "lasership": "ontrac",
    "amazon": "amazon",
    "canada post": "canadapost",
}
BRAND_WORDS = re.compile(r"(?i)(usps|fedex|amazon|\bups\b|\bdhl\b)")
TRACKING_URL_PARAM_RE = re.compile(
    r"(?i)[?&](?:tLabels|tracknum|trackNums|trackingNumber|tracknumbers|trknbr|selectedTrckNum|"
    r"tracking_number|tracking)=([A-Z0-9]{8,40})"
)
_REPEATED = re.compile(r"^(\w)\1+$")


def norm(n: str) -> str:
    return re.sub(r"[\s-]", "", n).upper()


def _labelled_numbers(upper: str) -> set[str]:
    """Numbers next to a "tracking number" label or inside a carrier tracking link."""
    labelled = {norm(m["num"]) for m in TRACKING_LABEL_RE.finditer(upper)}
    labelled |= {norm(m[1]) for m in TRACKING_URL_PARAM_RE.finditer(upper)}
    return labelled


def find_tracking(text: str, *, sender_carrier: str | None = None) -> list[tuple[str, str]]:
    """Return [(carrier, number)] in first-seen order, de-duplicated.

    A number is accepted when:
    - a strong pattern matches and its check digit passes;
    - a weak pattern matches, the check passes, and the sender is that carrier or it's labelled;
    - the check fails, but the carrier's own mail labels it as the tracking number.
    A match inside an accepted span (e.g. the 420+ZIP prefix of an IMpb) is skipped.
    """
    upper = text.upper()
    labelled = _labelled_numbers(upper)
    blocked = {
        b.replace("-", "") for b in AMAZON_ORDER_RE.findall(text) + WALMART_ORDER_RE.findall(text)
    }
    # Spaced numbers ("9400 1000 0000 ...") only exist in labels; search those too.
    haystacks = [upper, *(lab for lab in labelled if lab not in upper)]
    out: dict[str, str] = {}
    spans: list[tuple[int, int]] = []
    for pat in TRACKING_PATTERNS:
        for hay_idx, hay in enumerate(haystacks):
            for m in pat.regex.finditer(hay):
                n = m.group(1)
                if n in out or n in blocked or _REPEATED.match(n):
                    continue
                if hay_idx == 0 and any(s <= m.start() < e or s < m.end() <= e for s, e in spans):
                    continue
                ok = pat.check is None or pat.check(n)
                is_labelled = n in labelled or any(n in lab for lab in labelled)
                from_carrier = sender_carrier == pat.carrier
                if (ok and (pat.strong or from_carrier or is_labelled)) or (
                    not ok and is_labelled and from_carrier
                ):
                    out[n] = pat.carrier
                    if hay_idx == 0:
                        spans.append((m.start(), m.end()))
    return [(c, n) for n, c in out.items()]


# ---------------------------------------------------------------------------
# Status model
# ---------------------------------------------------------------------------


class Status(IntEnum):
    ORDERED = 10
    LABEL_CREATED = 20
    IN_TRANSIT = 30
    OUT_FOR_DELIVERY = 50
    AVAILABLE_FOR_PICKUP = 60
    DELIVERED = 70


STATUS_WORDS = {
    Status.ORDERED: "ordered",
    Status.LABEL_CREATED: "label created",
    Status.IN_TRANSIT: "in transit",
    Status.OUT_FOR_DELIVERY: "out for delivery",
    Status.AVAILABLE_FOR_PICKUP: "ready for pickup",
    Status.DELIVERED: "delivered",
}


@dataclass
class Event:
    """One status observation extracted from one email."""

    source: str
    status: Status | None
    email_date: dt.datetime
    exception: str | None = None
    carrier: str | None = None
    tracking: list[str] = field(default_factory=list)
    order_id: str | None = None
    eta_start: dt.date | None = None
    eta_end: dt.date | None = None
    eta_time: str | None = None
    description: str | None = None
    shipped_from: str | None = None
    shipment_ids: list[str] = field(default_factory=list)  # Amazon: one id per box
    items: int | None = None  # Amazon: "Ordered 3 items" / "Shipped 1 item"
    ship_to: list[Place] = field(default_factory=list)  # where the email says it's going


@dataclass
class Digest:
    """USPS Informed Delivery daily digest."""

    digest_date: dt.date | None
    email_date: dt.datetime
    mailpieces: int | None
    packages: dict[str, str]  # tracking -> section (today/soon/awaiting/outbound/unknown)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTHS = {m.lower(): i for i, m in enumerate(_MONTH_NAMES, 1)}
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MON = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_WD = r"(?:Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun)[a-z]*"
_TIME = r"\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?"
_WD_PREFIX = r"(?:" + _WD + r",?\s+)?"  # optional "Friday, "
_MON_DAY = _MON + r"\s+\d{1,2}"  # "Oct 9"


def email_local_dt(
    msg: EmailMessage, tz: dt.tzinfo, fallback: dt.datetime | None = None
) -> dt.datetime:
    """The email's Date header in local time; INTERNALDATE (or now) if it is missing/bad."""
    try:
        d = parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        d = fallback or dt.datetime.now(dt.UTC)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.UTC)
    return d.astimezone(tz)


def _month_day(mon: str, day: str, year: str | None, ref: dt.date) -> dt.date | None:
    mi = MONTHS.get(mon[:3].lower())
    if not mi:
        return None
    try:
        if year:
            return dt.date(int(year), mi, int(day))
        cands = [dt.date(ref.year + k, mi, int(day)) for k in (-1, 0, 1)]
    except ValueError:
        return None
    return min(cands, key=lambda c: abs((c - ref).days))


def resolve_relative(word: str, ref: dt.date) -> dt.date | None:
    w = word.strip().lower()
    if w == "today":
        return ref
    if w == "tomorrow":
        return ref + dt.timedelta(days=1)
    for idx, name in enumerate(WEEKDAYS):
        if w[:3] == name[:3]:
            # Same weekday name as the email's own day means today ("Arriving Friday" sent Friday).
            return ref + dt.timedelta(days=(idx - ref.weekday()) % 7)
    return None


_DATE_NUMERIC = re.compile(_WD_PREFIX + r"(?P<mm>\d{1,2})/(?P<dd>\d{1,2})/(?P<yy>\d{4})")
_DATE_WORDS = re.compile(
    _WD_PREFIX + r"(?P<mon>" + _MON + r")\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?(?:,\s*(?P<y>\d{4}))?"
)
_DATE_DAY_MON = re.compile(r"\b(?P<d>\d{1,2})\s+(?P<mon>" + _MON + r")(?:\s+(?P<y>\d{4}))?\b")


def parse_date_phrase(s: str, ref: dt.date) -> dt.date | None:
    if m := _DATE_NUMERIC.search(s):
        try:
            return dt.date(int(m["yy"]), int(m["mm"]), int(m["dd"]))
        except ValueError:
            return None
    if m := _DATE_WORDS.search(s):
        return _month_day(m["mon"], m["d"], m["y"], ref)
    if m := _DATE_DAY_MON.search(s):
        return _month_day(m["mon"], m["d"], m["y"], ref)
    if m := re.search(r"(?i)\b(today|tomorrow|" + _WD + r")\b", s):
        return resolve_relative(m[1], ref)
    return None


_RANGE_LEAD = r"(?i)(?:now expected|arriving|delivery (?:by|between))[:\s]+"
ETA_RANGE_RE = re.compile(_RANGE_LEAD + "(?P<a>" + _MON_DAY + r")\s*[-–]\s*(?P<b>" + _MON_DAY + ")")
_ETA_LEAD = (
    r"(?i)(?:now arriving(?: early)?(?: on)?|it.s now arriving|arriving|will arrive"
    r"|estimated delivery(?: date)?(?: is)?|estimated delivery on"
    r"|expected delivery(?: date)?(?: on)?|scheduled delivery(?: date)?)[:\s]+"
)
# "Fri, 10/9/2026", "Oct 9, 2026", "9 Oct"
_ETA_DATE = (
    _WD_PREFIX + r"(?:\d{1,2}/\d{1,2}/\d{4}|" + _MON_DAY + r"(?:,\s*\d{4})?|\d{1,2}\s+" + _MON + ")"
)
ETA_SINGLE_RE = re.compile(_ETA_LEAD + "(?P<a>" + _ETA_DATE + "|today|tomorrow|" + _WD + ")")
WINDOW_RE = re.compile(
    r"(?i)\b(?:between|estimated between)\s+(" + _TIME + r")\s+(?:and|-|to)\s+(" + _TIME + r")"
)
BY_TIME_RE = re.compile(r"(?i)\b(?:arriving |delivered )?by\s+(" + _TIME + r")")


def extract_eta(text: str, ref: dt.date) -> tuple[dt.date | None, dt.date | None]:
    if m := ETA_RANGE_RE.search(text):
        return parse_date_phrase(m["a"], ref), parse_date_phrase(m["b"], ref)
    if m := ETA_SINGLE_RE.search(text):
        d = parse_date_phrase(m["a"], ref)
        return d, d
    return None, None


def extract_window(text: str) -> str | None:
    if m := WINDOW_RE.search(text):
        return f"between {m[1]} and {m[2]}".lower()
    if m := BY_TIME_RE.search(text):
        return f"by {m[1]}".lower()
    return None


# ---------------------------------------------------------------------------
# Body text and sender authentication
# ---------------------------------------------------------------------------


def body_text(msg: EmailMessage) -> str:
    """Plain text if present, plus stripped HTML with link targets appended."""
    plain = htm = None
    for part in msg.walk():
        ct = part.get_content_type()
        try:
            if ct == "text/plain" and plain is None:
                plain = part.get_content()
            elif ct == "text/html" and htm is None:
                htm = part.get_content()
        except (LookupError, ValueError, AttributeError):
            continue
    if htm is not None:
        t = re.sub(r"(?is)<(script|style).*?</\1>", " ", htm)
        hrefs = re.findall(r"(?is)<a\b[^>]*?href=[\"']([^\"']+)[\"']", t)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        htm = _html.unescape(t) + "\n" + _html.unescape(" ".join(hrefs))
    txt = " ".join(x for x in (plain, htm) if x)
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", txt)


def from_address(msg: EmailMessage) -> str:
    try:
        if msg["From"] and msg["From"].addresses:
            return msg["From"].addresses[0].addr_spec.lower()
    except (AttributeError, IndexError, ValueError):
        pass
    m = re.search(r"[\w.+-]+@[\w.-]+", str(msg["From"] or ""))
    return m[0].lower() if m else ""


def _from_is_ambiguous(msg: EmailMessage) -> bool:
    """True when our parser and Gmail's DMARC check could disagree about who sent the mail.

    For example 'auto-reply@usps.com <x@evil.example>' or two addresses. Harmless defects such as
    the unquoted dot in 'Amazon.com <no-reply@amazon.com>' are fine.
    """
    try:
        h = msg["From"]
        if h is None:
            return False
        if len(h.addresses) != 1:
            return True
        parsed = h.addresses[0].addr_spec.lower()
        # The header object re-renders what it parsed; judge the untouched original text instead.
        raw = next((str(v) for k, v in msg.raw_items() if k.lower() == "from"), str(h))
        raw = re.sub(r"\s+", " ", raw)
        angle = re.findall(r"<\s*([^<>\s]+@[^<>\s]+)\s*>", raw)
        if angle and angle[-1].lower() != parsed:
            return True
        name = raw.rsplit("<", 1)[0] if angle else ""
        return "@" in name
    except (AttributeError, IndexError, ValueError):
        return True


def _same_org(a: str, b: str) -> bool:
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def sender_verified(msg: EmailMessage, addr: str) -> bool | None:
    """Gmail's verdict on the sender.

    True only if DMARC (or DKIM) passed for the From domain we parsed, False if it failed or
    doesn't match, None when there is no Gmail Authentication-Results header.
    """
    headers = msg.get_all("Authentication-Results") or []
    results = [r for r in headers if "mx.google.com" in str(r)]
    if not results:
        return None
    if _from_is_ambiguous(msg):
        return False
    blob = " ".join(str(r) for r in results).lower()
    if "dmarc=fail" in blob:
        return False
    domain = addr.rsplit("@", 1)[-1]
    for m in re.finditer(r"dmarc=pass[^;]*?header\.from=([\w.-]+)", blob):
        if _same_org(domain, m[1]):
            return True
    for m in re.finditer(r"dkim=pass[^;]*?header\.i=@?([\w.-]+)", blob):
        if _same_org(domain, m[1]):
            return True
    return False


def _lookalike_brand(msg: EmailMessage, addr: str) -> bool:
    """A store email that names a carrier or Amazon in its sender but isn't from that domain."""
    domain = addr.rsplit("@", 1)[-1]
    name = str(msg["From"] or "")
    for word in {w.lower() for w in BRAND_WORDS.findall(name + " " + domain) if w}:
        if not (domain == f"{word}.com" or domain.endswith(f".{word}.com")):
            return True
    return False


# ---------------------------------------------------------------------------
# Sources. First match wins, so specific rows come first.
# frm: exact address, "@domain" (also matches subdomains), or "*".
# ---------------------------------------------------------------------------
S = Status
_FEDEX_PREFIX = r"(?i)^(?:FedEx Shipment (?P<trk2>\d{12,34}):\s*)?.*?"
_FEDEX_TAIL = r"(?:.*?(?P<trk>\d{12,34}))?"


def _fedex_subject(middle: str) -> str:
    """FedEx puts the tracking number in a "FedEx Shipment <n>:" prefix or at the end."""
    return _FEDEX_PREFIX + middle + _FEDEX_TAIL


USPS = ["@usps.com"]
UPS = ["@ups.com"]
FEDEX = ["@fedex.com"]
AMAZON = ["@amazon.com"]
SHOPIFY = ["@t.shopifyemail.com", "@shopifyemail.com", "@parcelpanel.net"]

SOURCES: list[dict[str, Any]] = [
    # USPS Informed Delivery: mail count and package list.
    dict(
        id="usps_digest",
        carrier="usps",
        frm=["@informeddelivery.usps.com", "@usps.gov"],
        subject=r"^Your Daily Digest for (?P<digest_date>.+?)(?: is ready to view)?\s*$",
        status=None,
        handler="digest",
    ),
    # USPS Tracking: every subject is "USPS® <what> <tracking>"; classify <what> by keyword.
    dict(
        id="usps",
        carrier="usps",
        frm=USPS,
        subject=r"^USPS\S*\s+(?P<rest>.*?)\s*(?P<trk>\d{20,34})?\s*$",
        status=None,
        handler="usps",
    ),
    # UPS
    dict(
        id="ups_delivered",
        carrier="ups",
        frm=UPS,
        subject=(
            r"(?i)(?:Your UPS (?:Package|Packages|Parcel|Parcels) (?:was|were) delivered"
            r"|UPS Delivery Notification, Tracking Number\s+(?P<trk>1Z\w{16}))"
        ),
        status=S.DELIVERED,
    ),
    dict(
        id="ups_ofd",
        carrier="ups",
        frm=UPS,
        subject=(
            r"(?i)(?:Scheduled for Delivery Today|Follow Your Delivery on a Live Map"
            r"|Driver is Arriving Soon|out for delivery)"
        ),
        status=S.OUT_FOR_DELIVERY,
    ),
    dict(
        id="ups_exception",
        carrier="ups",
        frm=UPS,
        subject=(
            r"(?i)(?P<exc>New Scheduled Delivery Date|Delivery Attempted|Delivery Exception"
            r"|Delay)"
        ),
        status=None,
    ),
    dict(
        id="ups_ship",
        carrier="ups",
        frm=UPS,
        subject=(
            r"(?i)(?:Ship Notification|Scheduled for Delivery (?:Tomorrow|on)|on the way"
            r"|has shipped)"
        ),
        status=S.IN_TRANSIT,
    ),
    dict(id="ups_other", carrier="ups", frm=UPS, subject=r".", status=None, log_unmatched=True),
    # FedEx
    dict(
        id="fedex_delivered",
        carrier="fedex",
        frm=FEDEX,
        subject=_fedex_subject(
            r"Your (?:package|packages|shipment|shipments) (?:has|have|was|were) "
            r"(?:been )?delivered"
        ),
        status=S.DELIVERED,
    ),
    dict(
        id="fedex_ofd",
        carrier="fedex",
        frm=FEDEX,
        subject=_fedex_subject(
            r"(?:out for delivery|Delivery scheduled for today|scheduled for delivery today)"
        ),
        status=S.OUT_FOR_DELIVERY,
    ),
    dict(
        id="fedex_exception",
        carrier="fedex",
        frm=FEDEX,
        subject=_fedex_subject(r"(?P<exc>Delivery Exception|delayed|delay|attempted)"),
        status=None,
    ),
    dict(
        id="fedex_transit",
        carrier="fedex",
        frm=FEDEX,
        subject=_fedex_subject(
            r"(?:on its way|on the way|scheduled for delivery|shipment is scheduled|has shipped"
            r"|picked up|Delivery scheduled for)"
        ),
        status=S.IN_TRANSIT,
    ),
    dict(
        id="fedex_other", carrier="fedex", frm=FEDEX, subject=r".", status=None, log_unmatched=True
    ),
    # Amazon (keyed by order id; carrier tracking is not in these emails)
    dict(
        id="amazon_delivered",
        carrier="amazon",
        frm=AMAZON,
        subject=(
            r"^(?:Delivered(?::\s*|\s+\d+\s+items?:\s*)(?P<desc>.*)"
            r"|Your Amazon order has arrived!?)"
        ),
        status=S.DELIVERED,
    ),
    dict(
        id="amazon_ofd",
        carrier="amazon",
        frm=AMAZON,
        subject=(
            r"^(?:Out for delivery(?::\s*|\s+\d+\s+items?:\s*)(?P<desc>.*)"
            r"|Arriving [Tt]oday:?\s*(?P<desc2>.*))"
        ),
        status=S.OUT_FOR_DELIVERY,
    ),
    dict(
        id="amazon_exception",
        carrier="amazon",
        frm=AMAZON,
        subject=r"^(?P<exc>Delivery update):\s*(?P<desc>.*)",
        status=None,
    ),
    dict(
        id="amazon_ship_delay",
        carrier="amazon",
        frm=AMAZON,
        subject=r"(?i)^Delay in shipping your order",
        status=None,
        exception="shipping delayed",
    ),
    dict(
        id="amazon_shipped",
        carrier="amazon",
        frm=AMAZON,
        subject=(
            r"^(?:Shipped(?::\s*|\s+\d+\s+items?:\s*)(?P<desc>.*)"
            r"|Your Amazon\.com order #?(?P<order>\d{3}-\d{7}-\d{7}) has shipped"
            r"|Your package was shipped!?)"
        ),
        status=S.IN_TRANSIT,
    ),
    dict(
        id="amazon_ordered",
        carrier="amazon",
        frm=AMAZON,
        subject=r"^(?:Ordered(?::\s*|\s+\d+\s+items?:\s*)(?P<desc>.*)|Your Amazon\.com order)",
        status=S.ORDERED,
    ),
    # Retailers: tracking comes from the body.
    dict(
        id="walmart_delivered",
        carrier=None,
        frm=["@walmart.com"],
        subject=(
            r"(?i)^(?:Delivered:|Arrived:|Your order was delivered"
            r"|Some of your items were delivered)\s*(?P<desc>.*)"
        ),
        status=S.DELIVERED,
    ),
    dict(
        id="walmart_ofd",
        carrier=None,
        frm=["@walmart.com"],
        subject=r"(?i)^(?:Out for delivery|Your (?:package|delivery) should arrive by)",
        status=S.OUT_FOR_DELIVERY,
    ),
    dict(
        id="walmart_shipped",
        carrier=None,
        frm=["@walmart.com"],
        subject=r"(?i)^(?:Shipped|Your order (?:has )?shipped|.*on (?:its|the) way)",
        status=S.IN_TRANSIT,
    ),
    dict(
        id="etsy_delivered",
        carrier=None,
        frm=["@etsy.com"],
        subject=r"(?i)has been delivered",
        status=S.DELIVERED,
    ),
    dict(
        id="etsy_shipped",
        carrier=None,
        frm=["@etsy.com"],
        subject=r"(?i)(?:order is on the way|Order dispatched|has your order|has shipped)",
        status=S.IN_TRANSIT,
    ),
    dict(
        id="shopify_delivered",
        carrier=None,
        frm=SHOPIFY,
        subject=r"(?i)has been delivered",
        status=S.DELIVERED,
    ),
    dict(
        id="shopify_ofd",
        carrier=None,
        frm=SHOPIFY,
        subject=r"(?i)out for delivery",
        status=S.OUT_FOR_DELIVERY,
    ),
    dict(
        id="shopify_ordered",
        carrier=None,
        frm=SHOPIFY,
        subject=r"(?i)^Order\s+#?\S+\s+confirmed",
        status=S.ORDERED,
    ),
    dict(
        id="shopify_shipped",
        carrier=None,
        frm=SHOPIFY,
        subject=r"(?i)(?:on the way|has shipped|shipped)",
        status=S.IN_TRANSIT,
    ),
    # Any other store: only with a labelled tracking number and a verified sender.
    dict(
        id="generic",
        carrier=None,
        frm=["*"],
        subject=(
            r"(?i)(?P<kw>has been delivered|was delivered|delivered:|out for delivery"
            r"|arriving today|has shipped|have shipped|is on (?:its|the) way|shipped:"
            r"|shipping confirmation|order shipped|shipment notification|tracking (?:number|info))"
        ),
        status=None,
        handler="generic",
    ),
]
for _row in SOURCES:
    _row["_subj"] = re.compile(_row["subject"])

GENERIC_KW_STATUS = (
    (re.compile(r"(?i)delivered"), S.DELIVERED),
    (re.compile(r"(?i)out for delivery|arriving today"), S.OUT_FOR_DELIVERY),
    (
        re.compile(r"(?i)shipped|on (?:its|the) way|shipping confirmation|shipment|tracking"),
        S.IN_TRANSIT,
    ),
)
USPS_KW = (
    (re.compile(r"(?i)\bdelivered\b"), S.DELIVERED),
    (re.compile(r"(?i)out for delivery"), S.OUT_FOR_DELIVERY),
    (re.compile(r"(?i)pick ?up"), S.AVAILABLE_FOR_PICKUP),
    (
        re.compile(
            r"(?i)expected delivery|arriving|in transit|accepted|shipping label|pre-shipment"
        ),
        S.IN_TRANSIT,
    ),
)
USPS_EXC = re.compile(
    r"(?i)(exception|attempt|notice left|arriving late|delay|alert|return to sender|undeliverable)"
)
USPS_OFD_BODY = re.compile(r"(?i)Your item is out for delivery")
_ITEM_COUNT = re.compile(r"(?i)^(?:Ordered|Shipped|Delivered|Out for delivery)\s+(\d+)\s+items?\b")
_STATUS_TEXT = re.compile(r"(?i)^(?:now arriving|arriving|your |a one-time password)")

# Header-only pre-filter used before downloading bodies.
_CANDIDATE_SUBJECT = re.compile(
    r"(?i)deliver|shipped|ship notification|on (?:its|the) way|out for delivery|arriving|tracking|"
    r"your (?:order|package|shipment)|daily digest|dispatched|order (?:confirm|update)|ordered"
)
_CANDIDATE_DOMAINS = (
    "usps.com",
    "usps.gov",
    "ups.com",
    "fedex.com",
    "amazon.com",
    "walmart.com",
    "etsy.com",
    "shopifyemail.com",
    "dhl.com",
    "ontrac.com",
    "lasership.com",
)


def _from_matches(addr: str, pats: list[str]) -> bool:
    for p in pats:
        p = p.lower()
        if p == "*":
            return True
        if p.startswith("@"):
            dom = addr.rsplit("@", 1)[-1]
            if dom == p[1:] or dom.endswith("." + p[1:]):
                return True
        elif addr == p:
            return True
    return False


def is_candidate(from_header: str, subject: str) -> bool:
    """Cheap header check deciding whether a message body is worth downloading."""
    addr = from_header.lower()
    if any(d in addr for d in _CANDIDATE_DOMAINS):
        return True
    return bool(_CANDIDATE_SUBJECT.search(subject or ""))


def classify(msg: EmailMessage) -> tuple[dict[str, Any], re.Match[str]] | None:
    addr = from_address(msg)
    subject = " ".join(str(msg["Subject"] or "").split())
    for row in SOURCES:
        if not _from_matches(addr, row["frm"]):
            continue
        if m := row["_subj"].search(subject):
            return row, m
    return None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_message(
    msg: EmailMessage, tz: dt.tzinfo, fallback_date: dt.datetime | None = None
) -> Event | Digest | None:
    """Turn one email into an Event (package update), a Digest, or None (not a shipping email)."""
    hit = classify(msg)
    addr = from_address(msg)
    subject = " ".join(str(msg["Subject"] or "").split())
    if not hit:
        if any(d in addr for d in _CANDIDATE_DOMAINS):
            _LOGGER.debug("No rule for mail from %s: %s", addr, subject[:120])
        return None
    row, m = hit
    verified = sender_verified(msg, addr)
    if verified is False:
        _LOGGER.debug("Ignoring %s mail that failed Gmail sender checks: %s", row["id"], addr)
        return None
    when = email_local_dt(msg, tz, fallback_date)
    if row.get("handler") == "digest":
        return parse_informed_delivery(msg, when)
    if row.get("log_unmatched"):
        _LOGGER.debug("Unrecognized %s subject: %s", row["carrier"], str(msg["Subject"])[:120])
    ref = when.date()
    text = body_text(msg)
    captured = {k: v for k, v in m.groupdict().items() if v}
    ev = Event(source=row["id"], status=row["status"], email_date=when, carrier=row["carrier"])

    if row.get("handler") == "usps":
        rest = captured.get("rest", "")
        ev.status = next((st for p, st in USPS_KW if p.search(rest)), None)
        if exc := USPS_EXC.search(rest):
            ev.exception = exc[1].lower()
        if ev.status == S.IN_TRANSIT and USPS_OFD_BODY.search(text):
            ev.status = S.OUT_FOR_DELIVERY
        if ev.status in (S.IN_TRANSIT, S.OUT_FOR_DELIVERY):
            if d := parse_date_phrase(rest, ref):
                ev.eta_start = ev.eta_end = d
            ev.eta_time = extract_window(rest)
        if sm := re.search(
            r"(?i)Shipped from:\s*([A-Za-z][A-Za-z .,&'-]{1,40}?)(?=\s+\d|\s{2,}|\s*$)", text
        ):
            ev.shipped_from = sm[1].strip(" .,") or None
    elif row.get("handler") == "generic":
        ev.status = next((st for p, st in GENERIC_KW_STATUS if p.search(captured["kw"])), None)
    if row["carrier"] is None:  # store mail: remember which store, for a readable name
        try:
            name = msg["From"].addresses[0].display_name.strip().strip('"')
        except (AttributeError, IndexError, ValueError):
            name = ""
        ev.shipped_from = name or addr.rsplit("@", 1)[-1]
    if "exc" in captured:
        ev.exception = captured["exc"].lower()
    if row.get("exception"):
        ev.exception = row["exception"]
    if row["id"] == "amazon_exception":
        ev.exception = "running late" if re.search(r"(?i)running late", text) else "delivery update"

    # Tracking numbers: subject capture first, then the body.
    for key in ("trk2", "trk"):
        if key in captured and norm(captured[key]) not in ev.tracking:
            ev.tracking.append(norm(captured[key]))
    if not ev.tracking and row["carrier"] != "amazon":
        found = find_tracking(subject + " \n " + text, sender_carrier=row["carrier"])
        if row.get("handler") == "generic":
            # Unknown store: only numbers that are labelled or linked, never bare digits.
            labelled = _labelled_numbers(text.upper())
            found = [(c, n) for c, n in found if n in labelled or any(n in x for x in labelled)]
        ev.tracking = [n for _carrier, n in found]
        if found and ev.carrier is None:
            ev.carrier = found[0][0]
    if (
        not ev.tracking
        and row["carrier"] is None
        and row.get("handler") != "generic"
        and (label := TRACKING_LABEL_RE.search(text))
    ):
        # Store mail: take the labelled number, and the carrier named next to the label.
        ev.tracking.append(norm(label["num"]))
        word = (label["carrier"] or "").strip().lower()
        known = (v for k, v in KNOWN_CARRIERS.items() if word.endswith(k))
        ev.carrier = ev.carrier or next(known, None)
    if row.get("handler") == "generic" and (
        not ev.tracking or verified is not True or _lookalike_brand(msg, addr)
    ):
        return None

    # Order ids
    amazon_order = AMAZON_ORDER_RE.search(subject + " " + text)
    ev.order_id = captured.get("order") or (amazon_order[1] if amazon_order else None)
    if (
        row["id"].startswith("walmart")
        and not ev.order_id
        and (walmart_order := WALMART_ORDER_RE.search(text))
    ):
        ev.order_id = walmart_order[1]
    if (
        row["id"].startswith("etsy")
        and not ev.order_id
        and (etsy_order := ETSY_ORDER_RE.search(subject + " " + text))
    ):
        ev.order_id = "etsy-" + etsy_order[1]
    if (
        row["id"].startswith("shopify")
        and not ev.order_id
        and (shop_order := re.search(r"(?i)\border\s+#?([A-Za-z0-9-]{3,20})\b", subject))
    ):
        # Each Shopify store mails from its own address; ParcelPanel is shared, so add the store.
        sender = addr
        if "parcelpanel" in addr:
            sender = f"{addr}/{str(msg['From'] or '').split('<')[0].strip()}"
        ev.order_id = f"shopify-{sender}-{shop_order[1]}"

    # Amazon boxes: each shipment has its own id in the email's links.
    if row["carrier"] == "amazon":
        decoded = urllib.parse.unquote(urllib.parse.unquote(text))
        shipment_ids = re.findall(r"shipmentId[=:]\s*([A-Za-z0-9]{6,30})", decoded)
        ev.shipment_ids = list(dict.fromkeys(shipment_ids))
        if count := _ITEM_COUNT.search(subject):
            ev.items = int(count[1])

    # Description
    desc = captured.get("desc") or captured.get("desc2")
    if row["carrier"] == "amazon":
        body_plain = msg.get_body(("plain",))
        items = re.findall(r"(?m)^\* (.+)$", body_plain.get_content() if body_plain else "")
        if items:
            desc = "; ".join(i.strip() for i in items)
    if desc and _STATUS_TEXT.match(desc.strip("\"'“ ")):
        desc = None  # status text, not an item name
    ev.description = desc.strip("\"'‘’“”. ") if desc else None

    # ETA and delivery window
    if ev.eta_start is None and row["id"] == "amazon_ordered":
        # One order can arrive in several boxes ("Arriving tomorrow ... Arriving Saturday").
        dates = [d for mm in ETA_SINGLE_RE.finditer(text) if (d := parse_date_phrase(mm["a"], ref))]
        if dates:
            ev.eta_start, ev.eta_end = min(dates), max(dates)
    if ev.eta_start is None:
        ev.eta_start, ev.eta_end = extract_eta(text, ref)
    if ev.eta_time is None and ev.status in (S.IN_TRANSIT, S.OUT_FOR_DELIVERY):
        window = extract_window(subject)
        if not window and ev.carrier in ("fedex", "ups"):
            window = extract_window(text)
        ev.eta_time = window or None
    if ev.status == S.OUT_FOR_DELIVERY and ev.eta_start is None:
        ev.eta_start = ev.eta_end = ref
    if ev.status == S.DELIVERED:
        ev.eta_start = ev.eta_end = None
    ev.ship_to = ship_to(row["id"], row["carrier"], msg, text)

    if not ev.tracking and not ev.order_id:
        _LOGGER.debug("No tracking or order id in %s mail: %s", row["id"], subject[:120])
        return None
    return ev


DIGEST_SUBJECT_RE = re.compile(
    r"^Your Daily Digest for (?:" + _WD + r"),?\s+"
    r"(?:(?P<m>\d{1,2})/(?P<d>\d{1,2})|(?P<mon>" + _MON + r")\s+(?P<d2>\d{1,2}))"
)
_DIGEST_SECTIONS = (
    ("today", r'id="today-package-div"'),
    ("soon", r'id="soon-package-div"'),
    ("awaiting", r'id="awaiting-sender-package-div"'),
    ("outbound", r'id="outbound-package-div"'),
)


def parse_informed_delivery(msg: EmailMessage, when: dt.datetime) -> Digest:
    """Read the mail count and the package list (by section) from a daily digest."""
    ref = when.date()
    digest_date = None
    if m := DIGEST_SUBJECT_RE.search(" ".join(str(msg["Subject"] or "").split())):
        if m["m"]:
            digest_date = _month_day(list(MONTHS)[int(m["m"]) - 1], m["d"], None, ref)
        else:
            digest_date = _month_day(m["mon"], m["d2"], None, ref)
    htm = ""
    for p in msg.walk():
        if p.get_content_type() == "text/html":
            try:
                htm = p.get_content()
            except (LookupError, ValueError):
                htm = ""
            break

    total = re.search(r'id="total-mailpieces"[^>]*>\s*(\d+)\s*<', htm)
    mail = int(total[1]) if total else None
    if mail is None and htm:
        pieces = re.findall(r'id="mailpiece-image-src-id"', htm)
        ads = re.findall(r'id="campaign-representative-image-src-id"', htm)
        mail = len(pieces) + len(ads) or None

    # Each package belongs to the last section heading before it.
    marks = sorted(
        (mark.start(), section)
        for section, pattern in _DIGEST_SECTIONS
        for mark in re.finditer(pattern, htm)
    )
    pkgs: dict[str, str] = {}
    for tm in re.finditer(r'id="pra-tracking-number-id"[^>]*>\s*([0-9A-Z]{13,34})\s*<', htm):
        section = next((name for pos, name in reversed(marks) if pos < tm.start()), "unknown")
        pkgs.setdefault(norm(tm[1]), section)
    return Digest(digest_date=digest_date or ref, email_date=when, mailpieces=mail, packages=pkgs)
