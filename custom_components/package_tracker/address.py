"""Delivery addresses: find where a package is going and match it against the addresses to show.

Pure Python (no Home Assistant imports). Each kind of shipping email reveals a different part of
the address: Amazon "Jane - SPRINGFIELD, IL" (name and town), USPS "in SPRINGFIELD, IL 62701"
(town and ZIP), FedEx and UPS the full address. User entries and emails go through the same parser,
and matching only compares the parts both sides have.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
import html as _html
import re
from typing import Any
import unicodedata

# ---------------------------------------------------------------------------
# Normalising
# ---------------------------------------------------------------------------
_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_DASHES = re.compile("[\u2010-\u2015\u2212]")

STATES = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
    "PUERTO RICO": "PR",
    "GUAM": "GU",
    "AMERICAN SAMOA": "AS",
    "VIRGIN ISLANDS": "VI",
    "NORTHERN MARIANA ISLANDS": "MP",
    # Canada (Amazon.ca writes the province out in full)
    "ALBERTA": "AB",
    "BRITISH COLUMBIA": "BC",
    "MANITOBA": "MB",
    "NEW BRUNSWICK": "NB",
    "NEWFOUNDLAND AND LABRADOR": "NL",
    "NOVA SCOTIA": "NS",
    "NORTHWEST TERRITORIES": "NT",
    "NUNAVUT": "NU",
    "ONTARIO": "ON",
    "PRINCE EDWARD ISLAND": "PE",
    "QUEBEC": "QC",
    "SASKATCHEWAN": "SK",
    "YUKON": "YT",
}
STATE_CODES = frozenset(STATES.values()) | {"AA", "AE", "AP", "FM", "MH", "PW"}
_PROVINCES = frozenset(
    {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}
)
# Longest first, so "WEST VIRGINIA" is tried before "VIRGINIA".
_STATE_NAMES = sorted(STATES, key=len, reverse=True)
# Country names with dots and spaces removed.
_COUNTRY_KEYS = frozenset({"US", "USA", "UNITEDSTATES", "UNITEDSTATESOFAMERICA", "CANADA"})
_TRAILING_COUNTRY = re.compile(
    r"\s+(?:U\.?\s?S\.?(?:\s?A\.?)?|UNITED STATES(?: OF AMERICA)?|CANADA)$"
)

# USPS Publication 28 street suffixes (the common ones) and directionals.
SUFFIXES = {
    "ALLEY": "ALY",
    "AVENUE": "AVE",
    "AV": "AVE",
    "AVEN": "AVE",
    "BEND": "BND",
    "BOULEVARD": "BLVD",
    "BOUL": "BLVD",
    "CIRCLE": "CIR",
    "CIRC": "CIR",
    "COURT": "CT",
    "CRT": "CT",
    "COVE": "CV",
    "CREEK": "CRK",
    "CRESCENT": "CRES",
    "CROSSING": "XING",
    "DRIVE": "DR",
    "DRV": "DR",
    "EXPRESSWAY": "EXPY",
    "EXTENSION": "EXT",
    "FREEWAY": "FWY",
    "GARDENS": "GDNS",
    "GROVE": "GRV",
    "HARBOR": "HBR",
    "HEIGHTS": "HTS",
    "HIGHWAY": "HWY",
    "HILL": "HL",
    "HOLLOW": "HOLW",
    "JUNCTION": "JCT",
    "KNOLL": "KNL",
    "LANE": "LN",
    "LOOP": "LOOP",
    "MANOR": "MNR",
    "MEADOWS": "MDWS",
    "PARKWAY": "PKWY",
    "PKY": "PKWY",
    "PIKE": "PIKE",
    "PLACE": "PL",
    "PLAZA": "PLZ",
    "POINT": "PT",
    "RIDGE": "RDG",
    "ROAD": "RD",
    "ROUTE": "RTE",
    "RUN": "RUN",
    "SQUARE": "SQ",
    "STREET": "ST",
    "STR": "ST",
    "STRT": "ST",
    "TERRACE": "TER",
    "TERR": "TER",
    "TRAIL": "TRL",
    "TURNPIKE": "TPKE",
    "VIEW": "VW",
    "VILLAGE": "VLG",
    "WALK": "WALK",
    "WAY": "WAY",
}
_SUFFIX_CODES = frozenset(SUFFIXES.values())
DIRECTIONS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}
_DIR_CODES = frozenset(DIRECTIONS.values())
_UNIT_WORDS = frozenset(
    {
        "APT",
        "APARTMENT",
        "UNIT",
        "STE",
        "SUITE",
        "FL",
        "FLOOR",
        "BLDG",
        "BUILDING",
        "RM",
        "ROOM",
        "LOT",
        "TRLR",
        "SPC",
        "SPACE",
        "DEPT",
    }
)
_UNIT_VALUES = frozenset(
    {
        "PH",
        "PENTHOUSE",
        "UPPER",
        "UPPR",
        "LOWER",
        "LOWR",
        "REAR",
        "FRONT",
        "FRNT",
        "BSMT",
        "BASEMENT",
        "SIDE",
        "LBBY",
        "LOBBY",
        "OFC",
    }
)
_UNIT_SEGMENT = re.compile(
    r"^(?:#\s*[\w-]+|(?:"
    + "|".join(sorted(_UNIT_WORDS))
    + r")\.?\s*#?\s*(?:[\w-]*\d[\w-]*|[A-Z]|"
    + "|".join(sorted(_UNIT_VALUES))
    + r"))$"
)
# One-word town endings that make "12 Broadway Grove City" ambiguous (is GROVE the street suffix?).
_TOWN_TAILS = frozenset(
    {"CITY", "SPRINGS", "PLEASANT", "FALLS", "BEACH", "HILLS", "CENTER", "STATION", "JUNCTION"}
)
_STREET_START = re.compile(r"^(?:\d|P\.?\s?O\.?\s+BOX\b|POST OFFICE BOX)")
_ORDINALS = {
    "FIRST": "1ST",
    "SECOND": "2ND",
    "THIRD": "3RD",
    "FOURTH": "4TH",
    "FIFTH": "5TH",
    "SIXTH": "6TH",
    "SEVENTH": "7TH",
    "EIGHTH": "8TH",
    "NINTH": "9TH",
    "TENTH": "10TH",
}
# Spellings of the same place: "ST LOUIS" = "SAINT LOUIS", "N LAS VEGAS" = "NORTH LAS VEGAS".
_SAME_WORD = {"SAINT": "ST", "SAINTE": "STE", "FORT": "FT", "MOUNT": "MT", **DIRECTIONS}

_ZIP_TAIL = re.compile(r"(?:^|\s)(\d{5})(?:\s*-?\s*\d{4})?\s*$")
_CA_POSTAL_TAIL = re.compile(r"(?:^|\s)([A-Z]\d[A-Z])\s*(\d[A-Z]\d)\s*$")


def clean(text: str) -> str:
    """Uppercase and accent-free, with dashes unified and invisible marks and extra spaces gone."""
    t = unicodedata.normalize("NFKC", text or "")
    t = _INVISIBLE.sub("", t)
    t = _DASHES.sub("-", t)
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return " ".join(t.upper().replace("*", " ").split())


def words(text: str) -> tuple[str, ...]:
    """Whole words for matching ('#4' and '4' are the same, apostrophes split)."""
    return tuple(re.findall(r"[A-Z0-9]+", clean(text)))


def _contains(hay: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Whole-word phrase match: 'SPRINGFIELD' is in 'WEST SPRINGFIELD', 'MAIN' is not in 'MAINE'."""
    n = len(needle)
    return n > 0 and any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def _is_suffix(t: str) -> bool:
    return t in SUFFIXES or t in _SUFFIX_CODES


def _is_direction(t: str) -> bool:
    return t in DIRECTIONS or t in _DIR_CODES


def _canon(ws: tuple[str, ...]) -> tuple[str, ...]:
    """Words compared loosely: directions, Saint/Fort/Mount and street suffixes abbreviated."""
    return tuple(SUFFIXES.get(_SAME_WORD.get(t, t), _SAME_WORD.get(t, t)) for t in ws)


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Place:
    """Whatever we know about one delivery address. Empty fields are unknown, not blank."""

    name: tuple[str, ...] = ()
    house: str | None = None  # house number ("123", "12-34", "BOX 9" for PO boxes)
    street: tuple[str, ...] = ()  # street name without house number, unit, suffix or directions
    city: tuple[str, ...] = ()
    state: str | None = None
    zip: str | None = None  # 5-digit US ZIP or 6-character Canadian postal code
    loose: tuple[str, ...] = ()  # address words we couldn't split into street and town
    keyword: bool = False  # a bare entry like "Jane" or "Springfield": matched as words anywhere

    @property
    def located(self) -> bool:
        return bool(self.zip or self.city or (self.house and self.street) or self.loose)

    @property
    def all_words(self) -> tuple[str, ...]:
        parts = (
            self.name,
            (self.house,) if self.house else (),
            self.street,
            self.city,
            (self.state,) if self.state else (),
            (self.zip,) if self.zip else (),
            self.loose,
        )
        return tuple(w for p in parts for w in p)

    @property
    def key(self) -> str:
        return "|".join(
            (
                " ".join(self.name),
                self.house or "",
                " ".join(self.street),
                " ".join(self.city),
                self.state or "",
                self.zip or "",
                " ".join(self.loose),
                "k" if self.keyword else "",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": " ".join(self.name),
            "house": self.house,
            "street": " ".join(self.street),
            "city": " ".join(self.city),
            "state": self.state,
            "zip": self.zip,
            "loose": " ".join(self.loose),
        }
        return {k: v for k, v in out.items() if v}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Place:
        def w(key: str) -> tuple[str, ...]:
            return tuple(str(d.get(key) or "").split())

        return cls(
            name=w("name"),
            house=d.get("house") or None,
            street=w("street"),
            city=w("city"),
            state=d.get("state") or None,
            zip=d.get("zip") or None,
            loose=w("loose"),
        )


def _street(segment: str) -> tuple[str | None, tuple[str, ...], tuple[str, ...], bool]:
    """Split '123 Main Street Apt 4' into ('123', ('MAIN',), (), False).

    Returns the house number, street words, town words and `unsure`. Words after the street suffix
    are the town when no comma separates them ('123 Main St Springfield'). `unsure` means that
    split is a guess: two suffix-like words ('45 Oak Hill Rd Springfield' vs '1 Main St Grove
    City') or no suffix at all ('123 Main Springfield').
    """
    seg = re.sub(r"(\d)\s*-\s*([A-Z])\b", r"\1\2", clean(segment))  # "Apt 4-B" -> "Apt 4B"
    seg = re.sub(r"#\s*[\w-]+", " ", seg)  # "#4", "# 4B": a unit number
    toks = list(words(seg))
    if len(toks) >= 3 and toks[0] in ("PO", "P") and "BOX" in toks[:3]:
        i = toks.index("BOX")
        return (
            (f"BOX {toks[i + 1]}" if i + 1 < len(toks) else None),
            ("PO", "BOX"),
            tuple(toks[i + 2 :]),
            False,
        )
    if len(toks) >= 4 and toks[:3] == ["POST", "OFFICE", "BOX"]:
        return f"BOX {toks[3]}", ("PO", "BOX"), tuple(toks[4:]), False
    house = toks.pop(0) if toks and toks[0][0].isdigit() else None
    i = 1
    while i < len(toks):  # "Apt 4", "Unit B", "Fl 2", "Apt PH": drop the unit word and its value
        if toks[i] in _UNIT_WORDS:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            value = nxt and (any(c.isdigit() for c in nxt) or len(nxt) == 1 or nxt in _UNIT_VALUES)
            del toks[i : i + (2 if value else 1)]
        else:
            i += 1
    toks = [_ORDINALS.get(t, t) for t in toks]
    suffix_at = [k for k in range(1, len(toks)) if _is_suffix(toks[k])]
    rest: list[str] = []
    unsure = False
    if suffix_at and suffix_at[0] < len(toks) - 1:
        k = suffix_at[0]
        # "Main St W Springfield": an abbreviated direction belongs to the street, a spelled-out
        # one usually starts the town ("Elm Ave North Springfield").
        post = 1 if k + 2 < len(toks) and toks[k + 1] in _DIR_CODES else 0
        # "31 Highway 101 Crescent City", "12 Broadway Grove City": the suffix may start the town.
        unsure = toks[k - 1].isdigit() or _is_suffix(toks[0])
        toks, rest = toks[: k + 1 + post], toks[k + 1 + post :]
    while rest and any(c.isdigit() for c in rest[0]):
        rest.pop(0)  # "#4" or "2nd" (floor) between the street and the town
    if rest and all(_is_direction(t) or _is_suffix(t) for t in rest):
        # "Main Street West", "Oak Hill Rd": still the street, not a town.
        toks, rest = toks + rest, []
    unsure = bool(rest) and (
        unsure or len(suffix_at) >= 2 or (len(rest) == 1 and rest[0] in _TOWN_TAILS)
    )
    unsure = unsure or (not suffix_at and sum(1 for t in toks if not _is_direction(t)) >= 2)
    toks = [_SAME_WORD.get(t, t) for t in toks]
    if len(toks) == 2 and toks[0] in SUFFIXES and (len(toks[1]) == 1 or toks[1].isdigit()):
        toks[0] = SUFFIXES[toks[0]]  # "Avenue A"
    # Drop leading and trailing directions, unless one is the street's name ("West St", "N St NW").
    if any(not _is_direction(t) and not _is_suffix(t) for t in toks):
        while len(toks) > 1 and toks[0] in _DIR_CODES:
            toks.pop(0)
        while len(toks) > 1 and toks[-1] in _DIR_CODES:
            toks.pop()
    if len(toks) > 1 and _is_suffix(toks[-1]):
        toks.pop()
    return house, tuple(toks), tuple(rest), unsure


def _state_of(segment: str) -> tuple[str | None, str]:
    """Split off a trailing state: 'WEST SPRINGFIELD MA' -> ('MA', 'WEST SPRINGFIELD')."""
    seg = segment.strip(" .")
    for name in _STATE_NAMES:
        if seg == name or seg.endswith(" " + name):
            return STATES[name], seg[: len(seg) - len(name)].strip(" .")
    parts = seg.rsplit(" ", 1)
    if parts[-1].replace(".", "") in STATE_CODES:
        return parts[-1].replace(".", ""), (parts[0] if len(parts) == 2 else "").strip(" .")
    return None, seg


def _parse(text: str, from_email: bool) -> tuple[Place, bool]:
    """parse(), plus whether the street/town split was a guess (entries should then use commas)."""
    t = clean(text)
    t = re.sub(r"\b(?:SHIP(?:PING)? TO|DELIVER(?:Y|ED|ING)? TO|DELIVERY ADDRESS)\s*:?\s*", "", t)
    name: tuple[str, ...] = ()
    if m := re.match(r"^(?P<name>[^,0-9]{1,60})\s+-\s+(?P<rest>[^,]*,.*)$", t):  # "NAME - CITY, ST"
        name, t = words(m["name"]), m["rest"]
    segs = [s.strip(" .") for s in t.split(",")]
    if segs:
        segs[-1] = _TRAILING_COUNTRY.sub("", segs[-1]).strip(" .")  # "IL 62701 USA"
    segs = [s for s in segs if s and re.sub(r"[ .]", "", s) not in _COUNTRY_KEYS]  # ", U.S.A."
    if (
        len(segs) >= 3
        and segs[-2] == "CA"
        and segs[-3] in _PROVINCES
        and _CA_POSTAL_TAIL.search(" " + segs[-1])
    ):
        del segs[-2]  # FedEx Canada: "TORONTO, ON, CA, M5V2T6" (CA = Canada, not California)
    if not segs:
        return Place(name=name), False
    zip_ = state = None
    # ZIP / postal code at the very end (its own segment, or after the state).
    if m := _ZIP_TAIL.search(segs[-1]):
        zip_, segs[-1] = m[1], segs[-1][: m.start()].strip(" .")
    elif m := _CA_POSTAL_TAIL.search(segs[-1]):
        zip_, segs[-1] = m[1] + m[2], segs[-1][: m.start()].strip(" .")
    if not segs[-1]:
        segs.pop()
    segs = [s for s in segs if not _UNIT_SEGMENT.match(s)]  # "Apt 4", "#4" are never the town
    if not segs:
        return Place(name=name, zip=zip_), False
    if (
        len(segs) == 1
        and not zip_
        and not name
        and not re.match(r"^\d", segs[0])
        and not from_email
    ):
        return Place(loose=words(segs[0]), keyword=True), False  # "Jane", "Springfield"
    city: tuple[str, ...] = ()
    st, rest = _state_of(segs[-1])
    # "12 Oak Ct" is a street, not the state CT; with a ZIP after it, a trailing code is a state.
    take = (
        bool(st)
        and (len(segs) > 1 or bool(zip_) or bool(name))
        and (bool(zip_) or not _STREET_START.match(rest))
    )
    if st and not take and len(segs) == 1 and _STREET_START.match(rest) and _street(rest)[2]:
        take = True  # "123 Main St Springfield IL": a town sits between the street and the state
    if take:
        state = st
        if rest:
            segs[-1] = rest  # "SPRINGFIELD IL" written without a comma
        else:
            segs.pop()
    if segs and not _STREET_START.match(segs[-1]) and (state or zip_ or name or len(segs) > 1):
        city = words(segs.pop())
    house: str | None = None
    street: tuple[str, ...] = ()
    loose: tuple[str, ...] = ()
    unsure = False
    street_at = next((i for i, s in enumerate(segs) if _STREET_START.match(s)), None)
    if street_at is not None:
        if not name:
            name = tuple(w for s in segs[:street_at] for w in words(s))
        house, street, town, guess = _street(segs[street_at])
        if not city:
            city = town
            unsure = guess and bool(town or state or zip_)  # a street on its own needs no commas
            if from_email and (guess or not (city or state or zip_)):
                # Street and town run together and can't be told apart: keep the words loose.
                loose, house, street, city = words(segs[street_at]), None, (), ()
    elif segs and not name:
        name = tuple(w for s in segs for w in words(s))
    # Names repeat in carrier mail ("Jane Doe / Jane Doe"); keep one copy.
    half = len(name) // 2
    if name and len(name) % 2 == 0 and name[:half] == name[half:]:
        name = name[:half]
    place = Place(
        name=name, house=house, street=street, city=city, state=state, zip=zip_, loose=loose
    )
    return place, unsure


def parse(text: str, *, from_email: bool = False) -> Place:
    """Parse an address written the usual way.

    Accepts 'Jane Doe, 123 Main St, Springfield, IL 62701', 'Jane - SPRINGFIELD, IL', '62701',
    'Springfield, IL' and so on. A user entry with no comma, ZIP, state or house number ('Jane',
    'Springfield') is a keyword. Email text whose street and town can't be told apart keeps those
    words loose.
    """
    return _parse(text, from_email)[0]


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _same_person(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """First names agree; Amazon sometimes shows only an initial ('N')."""
    x, y = a[0], b[0]
    return x == y or (len(y) == 1 and x.startswith(y)) or (len(x) == 1 and y.startswith(x))


def _keyword_forms(kw: tuple[str, ...]) -> set[tuple[str, ...]]:
    """'Springfield Illinois' also as 'SPRINGFIELD IL'; 'Washington D C' also as 'WASHINGTON DC'."""
    joined: list[str] = []
    for w in kw:  # "D C" -> "DC"
        if len(w) == 1 and joined and len(joined[-1]) == 1 and w.isalpha() and joined[-1].isalpha():
            joined[-1] += w
        else:
            joined.append(w)
    forms = {_canon(kw), _canon(tuple(joined))}
    text = " ".join(kw)
    for name in _STATE_NAMES:
        if text.endswith(" " + name):
            forms.add(_canon(tuple(text[: -len(name)].split()) + (STATES[name],)))
            break
    return forms


def _street_hay(ws: tuple[str, ...]) -> tuple[str, ...]:
    """Email words normalised the way streets are: '9 N FIRST AVE' -> '9 1ST AVE'."""
    out = (_SAME_WORD.get(_ORDINALS.get(w, w), _ORDINALS.get(w, w)) for w in ws)
    return tuple(w for w in out if w not in _DIR_CODES)


def compare(entry: Place, pkg: Place) -> bool | None:
    """True if the package goes to that address, False if somewhere else, None if unknown."""
    if entry.keyword:  # "Jane" or "Springfield": found anywhere = match
        if not pkg.located:
            return None
        hay = _canon(pkg.all_words)
        if any(_contains(hay, form) for form in _keyword_forms(entry.loose)):
            return True
        kw = entry.loose
        if len(kw) > 1 and _is_suffix(kw[-1]) and _contains(_canon(pkg.street), _canon(kw[:-1])):
            return True  # "Elm Street": streets are stored without their suffix
        if len(kw) == 2 and len(pkg.name) == 1 and len(pkg.name[0]) > 1 and pkg.name[0] == kw[0]:
            return True  # "Jane Doe" vs Amazon's first-name-only "JANE"
        return False if pkg.name else None  # it may be a name, and this email didn't give one
    if pkg.loose and not (pkg.city or pkg.zip):
        # Street and town run together ("123 MAIN ST SPRINGFIELD"): look for the entry's parts.
        if entry.house and entry.street:
            needle = (entry.house,) + entry.street[:1]
            in_street = _contains(_street_hay(pkg.all_words), needle)
            return in_street or _contains(_canon(pkg.all_words), needle)
        if entry.city:
            return _contains(_canon(pkg.all_words), _canon(entry.city))
        return None
    checks: list[bool] = []
    same_zip = bool(entry.zip and pkg.zip and entry.zip == pkg.zip)
    if entry.zip and pkg.zip:
        checks.append(same_zip)
    if entry.city and pkg.city and not same_zip:  # a matching ZIP beats a town spelled differently
        checks.append(_canon(entry.city) == _canon(pkg.city))
    if entry.state and pkg.state:
        checks.append(entry.state == pkg.state)
    street_known = bool(entry.house and pkg.house and entry.street and pkg.street)
    if street_known:
        checks.append(entry.house == pkg.house and _canon(entry.street) == _canon(pkg.street))
    located = bool((entry.zip and pkg.zip) or (entry.city and pkg.city) or street_known)
    if not located:
        return None
    if entry.name and pkg.name and not (entry.zip and pkg.zip) and not street_known:
        # Only a town to go on (Amazon): the name tells apart households in the same town.
        checks.append(_same_person(entry.name, pkg.name))
    return all(checks)


@dataclass(frozen=True)
class AddressFilter:
    """The user's 'addresses to show' option, parsed once."""

    entries: tuple[Place, ...] = ()
    show_unknown: bool = True
    informed_delivery_is_mine: bool = True

    @property
    def active(self) -> bool:
        return bool(self.entries)

    @classmethod
    def build(
        cls,
        addresses: list[str] | tuple[str, ...] | None,
        show_unknown: bool = True,
        informed_delivery_is_mine: bool = True,
    ) -> AddressFilter:
        entries = tuple(p for p in (parse(a) for a in addresses or ()) if p.located or p.name)
        return cls(entries, show_unknown, informed_delivery_is_mine)

    def verdict(self, places: list[Place] | tuple[Place, ...]) -> bool | None:
        """True if any known address matches an entry; None when nothing can be compared."""
        results = [compare(e, p) for p in places for e in self.entries]
        if any(r is True for r in results):
            return True
        if any(r is False for r in results):
            return False
        return None


def clean_entries(
    raw: list[str] | None, max_entries: int = 10
) -> tuple[list[str], str | None, str]:
    """Tidy the option before saving: drop blanks and duplicates, reject unusable entries.

    Returns (entries, error key or None, the entry the error is about).
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in raw or []:
        text = " ".join(str(item).split())
        if not text:
            continue
        if len(text) > 150:
            return out, "address_too_long", text[:40]
        p, unsure = _parse(text, False)
        if len("".join(p.all_words)) < 3 or not (p.located or p.name):
            return out, "address_too_short", text
        if unsure:
            return out, "address_needs_commas", text
        if p.key in seen:
            continue
        seen.add(p.key)
        out.append(text)
    if len(out) > max_entries:
        return out, "too_many_addresses", str(max_entries)
    return out, None, ""


# ---------------------------------------------------------------------------
# Finding the ship-to in an email
# ---------------------------------------------------------------------------
_BLOCK_END = re.compile(
    r"(?i)</(?:p|div|td|th|tr|li|h[1-6]|table|span)>|<br\s*/?>|<(?:p|div|tr|li|table)\b[^>]*>"
)


def html_lines(msg: EmailMessage) -> list[str]:
    """The HTML part as lines, breaking where the email visibly breaks (cells, paragraphs, <br>)."""
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            htm = part.get_content()
        except (LookupError, ValueError, AttributeError):
            return []
        htm = re.sub(r"(?is)<!--.*?-->|<(script|style|head)\b.*?</\1>", " ", htm)
        htm = _BLOCK_END.sub("\n", htm)
        htm = _html.unescape(re.sub(r"(?s)<[^>]+>", " ", htm))
        return [" ".join(_INVISIBLE.sub("", ln).split()) for ln in htm.splitlines() if ln.strip()]
    return []


def plain_lines(msg: EmailMessage) -> list[str]:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            try:
                txt = part.get_content()
            except (LookupError, ValueError, AttributeError):
                return []
            return [" ".join(_INVISIBLE.sub("", ln).split()) for ln in txt.splitlines()]
    return []


def _state_code(text: str) -> str | None:
    """A whole state/province ('IL', 'Illinois', 'Ontario') -> its code; anything else -> None."""
    st, rest = _state_of(clean(text))
    return st if st and not rest else None


_AMAZON_LINE = re.compile(
    r"^[\s*]*(?P<name>[^\n]{1,60})\s+[-–—]\s+(?P<city>[A-Za-z][A-Za-z .'-]{0,40}?),"
    r"\s*(?P<st>[A-Za-z][A-Za-z .]{1,30}?)[\s*]*$"
)
_CITY_ST = re.compile(
    r"^(?P<city>[A-Za-z][A-Za-z .'-]{0,40}?),\s*(?P<st>[A-Za-z][A-Za-z .]{1,30}?)\s*$"
)
_ORDER_LINE = re.compile(r"^\s*Order\s*#")
_SHIP_TO_LINE = re.compile(r"(?i)^\s*Ship(?:ping)?\s+to:?\s*$")


def _amazon(msg: EmailMessage) -> list[Place]:
    plain = [ln for ln in plain_lines(msg) if ln.strip(" *")]
    lines = plain or [ln for ln in html_lines(msg) if ln.strip(" *")]
    out: list[Place] = []
    for i, ln in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        prev = lines[i - 1] if i else ""
        # In the HTML copy the ship-to line follows the item count ("1 item: Coffee", "2 Kitchen
        # items"), so a product title like "Wall Art - Austin, Texas" isn't taken for one.
        in_place = bool(plain) or bool(re.match(r"(?i)^\d+\s+(?:\S+\s+)*?items?\b", prev))
        if in_place and _ORDER_LINE.match(nxt) and not ln.rstrip(" *").endswith(("...", "…")):
            # "Jane - SPRINGFIELD, IL" right before "Order #". Split at the last dash so names like
            # "Amazon Hub Locker - Maple" or "Doe, Jane" stay whole.
            if (m := _AMAZON_LINE.match(ln)) and (st := _state_code(m["st"])):
                out.append(Place(name=words(m["name"]), city=words(m["city"]), state=st))
        elif (
            # Older layout: "Ship to:" / name / "CITY, ST"
            _SHIP_TO_LINE.match(ln)
            and i + 2 < len(lines)
            and (m := _CITY_ST.match(lines[i + 2]))
            and (st := _state_code(m["st"]))
        ):
            out.append(Place(name=words(lines[i + 1]), city=words(m["city"]), state=st))
    return out


_FEDEX_STOP = re.compile(
    r"(?i)^(?:ship date|reference|purchase order|number of pieces|total shipment weight|"
    r"origin|destination|service|packaging|tracking|scheduled delivery|standard transit|"
    r"signed for|delivered to)\b"
)
_CITY_ST_US = re.compile(
    r"^(?P<city>[A-Za-z][A-Za-z .'-]*?),\s*(?P<st>[A-Za-z]{2}),?\s*(?:US|USA|CA)?,?\s*"
    r"(?P<zip>\d{5}(?:-?\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)?$"
)
_ZIP_LINE = re.compile(r"^(?:\d{5}(?:-?\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)$")


def _fedex(msg: EmailMessage) -> list[Place]:
    lines = html_lines(msg) or [ln for ln in plain_lines(msg) if ln]
    for i, ln in enumerate(lines):
        if ln.strip(" :").upper() != "TO":
            continue
        block: list[str] = []
        for j in range(i + 1, min(i + 9, len(lines))):
            nxt = lines[j]
            if _FEDEX_STOP.match(nxt):
                break
            block.append(nxt)
            if m := _CITY_ST_US.match(nxt):
                following = lines[j + 1] if j + 1 < len(lines) else ""
                if not m["zip"] and _ZIP_LINE.match(following):
                    block.append(following)  # the ZIP on a line of its own
                break
        place = parse(", ".join(block), from_email=True)
        if place.city and (place.state or place.zip) and not _placeholder(place):
            return [place]
    for i, ln in enumerate(lines):  # older layout: "DESTINATION" / "CITY, ST, US, ZIP"
        if (
            ln.strip(" :").upper() == "DESTINATION"
            and i + 1 < len(lines)
            and (m := _CITY_ST_US.match(lines[i + 1]))
        ):
            return [parse(f"{m['city']}, {m['st']} {m['zip'] or ''}", from_email=True)]
    return []


def _placeholder(p: Place) -> bool:
    return p.city in (("CITY",), ("SOME", "PLACE")) and p.state in (None, "ST")


_UPS_LABEL = re.compile(r"(?i)^\s*(?:Ship\s*To|Delivery\s+Location)\s*:?\s*(?P<inline>.*)$")
_LOCALITY_TAIL_RE = re.compile(
    r"(?i)\b(?P<st>[A-Za-z]{2}),?\s*(?:US,?\s*)?(?:\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)"
    r"(?:\s*,?\s*(?:US|USA|United States|Canada))?\s*$"
)


def _has_locality(line: str) -> bool:
    """The line ends in a real state or province code plus a ZIP or postal code."""
    m = _LOCALITY_TAIL_RE.search(line)
    return bool(m and m["st"].upper() in STATE_CODES)


def _ups(msg: EmailMessage) -> list[Place]:
    for lines in (html_lines(msg), [ln for ln in plain_lines(msg) if ln]):
        for i, ln in enumerate(lines):
            if not (m := _UPS_LABEL.match(ln)):
                continue
            block = [m["inline"]] if m["inline"].strip() else []
            for nxt in lines[i + 1 : i + 6]:
                if block and _has_locality(block[-1]):
                    break
                block.append(nxt)
            if block and _has_locality(block[-1]):
                place = parse(", ".join(block), from_email=True)
                if place.city or place.zip:
                    return [place]
    return []


_USPS_WHERE = re.compile(
    r"(?i)\b(?:is out for (?:re)?delivery|was delivered|has been delivered|attempted to deliver"
    r"|delivery was attempted|could not be delivered|arrived at the Post Office"
    r"|is available for pickup|is ready for pickup"
    r"|was picked up at (?:a|the) (?:postal facility|post office))"
    r"\b[^.\n]*?\bin\s+(?P<city>[A-Z][A-Za-z .'-]*?),\s*(?P<st>[A-Z]{2}),?\s+"
    r"(?P<zip>\d{5})(?:-\d{4})?"
    r"(?:\.|\s+(?:and|on)\b)"
)
_USPS_NOT_DESTINATION = re.compile(
    r"(?i)shipping partner|(?<!picked up at a postal )facility|distribution|network|processing"
    r"|departed|in transit|\bsort|accepted"
)


def _usps(text: str) -> list[Place]:
    for m in _USPS_WHERE.finditer(text):
        if not _USPS_NOT_DESTINATION.search(m[0]):
            return [parse(f"{m['city']}, {m['st']} {m['zip']}", from_email=True)]
    return []


def _with_name(name: str, p: Place) -> Place:
    if p.name or not name:
        return p
    return Place(
        name=words(name),
        house=p.house,
        street=p.street,
        city=p.city,
        state=p.state,
        zip=p.zip,
        loose=p.loose,
    )


_ETSY_LABEL = re.compile(r"(?i)^\s*(?P<label>Shipping to|Delivered to)\s*:?\s*$")


def _etsy(msg: EmailMessage) -> list[Place]:
    """'Shipping to' / name / address, or 'Delivered to' / address. Nothing else is read."""
    lines = plain_lines(msg) or html_lines(msg)
    for i, ln in enumerate(lines):
        if not (m := _ETSY_LABEL.match(ln)):
            continue
        rest = lines[i + 1 : i + 4]
        while rest and not rest[0].strip():
            rest.pop(0)
        if m["label"].lower() == "shipping to":
            if len(rest) >= 2 and rest[1].strip():
                p = parse(rest[1], from_email=True)
                return [_with_name(rest[0], p)] if p.located else []
        elif rest:
            p = parse(rest[0], from_email=True)
            return [p] if p.located else []
    return []


_SHOPIFY_STOP = re.compile(
    r"(?i)^(?:billing address|shipping method|payment|customer information|order summary"
    r"|subtotal|if you have any questions|united states|usa|us|canada)\b"
)


def _shopify(msg: EmailMessage) -> list[Place]:
    lines = html_lines(msg) or [ln for ln in plain_lines(msg) if ln]
    for ln in lines:
        if m := re.match(r"(?i)^\s*Ship to:\s*(?P<name>[^,]+),\s*(?P<rest>.+)$", ln):
            p = parse(m["rest"], from_email=True)
            return [_with_name(m["name"], p)] if p.located else []
    for i, ln in enumerate(lines):
        if not re.match(r"(?i)^\s*(?:Shipping address|Ship to)\s*:?\s*$", ln):
            continue
        block: list[str] = []
        for nxt in lines[i + 1 : i + 8]:
            if _SHOPIFY_STOP.match(nxt.strip()):
                break
            block.append(nxt)
            if _has_locality(nxt):
                name = block[0] if len(block) > 1 and not re.match(r"^\d", block[0]) else ""
                p = parse(", ".join(block[1:] if name else block), from_email=True)
                return [_with_name(name, p)] if p.located else []
        return []
    return []


def ship_to(row_id: str, carrier: str | None, msg: EmailMessage, text: str) -> list[Place]:
    """The delivery address(es) named in one shipping email, or [] when it doesn't say."""
    try:
        if carrier == "amazon":
            found = _amazon(msg)
        elif carrier == "fedex" and row_id.startswith("fedex"):
            found = _fedex(msg)
        elif carrier == "ups" and row_id.startswith("ups"):
            found = _ups(msg)
        elif row_id == "usps":
            found = _usps(text)
        elif row_id.startswith("etsy"):
            found = _etsy(msg)
        elif row_id.startswith("shopify"):
            found = _shopify(msg)
        else:
            found = []
    except (LookupError, ValueError, AttributeError, IndexError):
        found = []
    out: list[Place] = []
    for p in found:
        if p.located and p.key not in {q.key for q in out}:
            out.append(p)
    return out[:4]
