"""Persistent package registry: remembers every package seen, independent of the mailbox.

Pure Python (no Home Assistant imports). Packages are matched across emails by tracking number
("t:"), Amazon shipment id ("s:", one per box) or order id ("o:"). An order id never merges two
different tracking numbers or boxes; it only links mail that has nothing better. Status moves
forward (a reschedule can take out-for-delivery back to in transit), delivered is final, and
finished packages leave a tombstone so an older email processed later can't bring them back.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
import datetime as dt
from typing import Any

from .address import AddressFilter, Place
from .kb import STATUS_WORDS, Digest, Event, Status

S = Status
PROBABLY_DELIVERED = 65  # view-only code between "ready for pickup" (60) and "delivered" (70)
TOMBSTONE_DAYS = 45
SEEN_DAYS = 45
MAIL_DAYS = 7
EXCEPTION_QUIET_DAYS = 7  # a delay with no news for this long stops blocking "probably delivered"
MAX_PLACES = 4  # delivery addresses remembered per package


def _d(v: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(v) if v else None
    except ValueError:
        return None


def _dtm(v: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(v) if v else None
    except ValueError:
        return None


def _iso(v: dt.date | dt.datetime | None) -> str | None:
    return v.isoformat() if v else None


def _int_or_none(v: Any) -> int | None:
    return int(v) if v is not None else None


def _places(raw: list[Any] | None) -> list[Place]:
    places = (Place.from_dict(p) for p in raw or [] if isinstance(p, dict))
    return [p for p in places if p.located]


@dataclass
class Package:
    key: str
    carrier: str | None = None
    status: Status = S.ORDERED
    status_date: dt.date | None = None
    exception: str | None = None
    eta_start: dt.date | None = None
    eta_end: dt.date | None = None
    eta_time: str | None = None
    description: str | None = None
    shipped_from: str | None = None
    order_id: str | None = None
    tracking: list[str] = field(default_factory=list)
    shipment_ids: list[str] = field(default_factory=list)
    items: int | None = None
    first_seen: dt.datetime | None = None
    last_update: dt.datetime | None = None
    delivered_at: dt.datetime | None = None
    digest_date: dt.date | None = None  # last USPS Informed Delivery digest that listed it
    # Amazon order record: box key -> items
    boxes: dict[str, int | None] = field(default_factory=dict)
    manual: str | None = None  # "received" (ticked by hand) or "not_received" (unticked)
    prev_status: int | None = None  # status before a manual tick, for undo
    ship_to: list[Place] = field(default_factory=list)  # addresses its emails named, newest last
    legacy: bool = False  # tracked before addresses were read: shown until one is known
    msgid: str | None = None  # Gmail X-GM-MSGID of its newest email

    @property
    def strong_aliases(self) -> set[str]:
        return {f"t:{t}" for t in self.tracking} | {f"s:{s}" for s in self.shipment_ids}

    @property
    def order_alias(self) -> str | None:
        return f"o:{self.order_id}" if self.order_id else None

    @property
    def is_order_only(self) -> bool:
        return not self.tracking and not self.shipment_ids

    @property
    def aliases(self) -> set[str]:
        out = self.strong_aliases
        if self.order_alias and self.is_order_only:
            out.add(self.order_alias)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "carrier": self.carrier,
            "status": int(self.status),
            "status_date": _iso(self.status_date),
            "exception": self.exception,
            "eta_start": _iso(self.eta_start),
            "eta_end": _iso(self.eta_end),
            "eta_time": self.eta_time,
            "description": self.description,
            "shipped_from": self.shipped_from,
            "order_id": self.order_id,
            "tracking": list(self.tracking),
            "shipment_ids": list(self.shipment_ids),
            "items": self.items,
            "first_seen": _iso(self.first_seen),
            "last_update": _iso(self.last_update),
            "delivered_at": _iso(self.delivered_at),
            "digest_date": _iso(self.digest_date),
            "boxes": dict(self.boxes),
            "manual": self.manual,
            "prev_status": self.prev_status,
            "ship_to": [p.to_dict() for p in self.ship_to],
            "legacy": self.legacy,
            "msgid": self.msgid,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Package":
        return cls(
            key=str(d["key"]),
            carrier=d.get("carrier"),
            status=Status(int(d.get("status", S.ORDERED))),
            status_date=_d(d.get("status_date")),
            exception=d.get("exception"),
            eta_start=_d(d.get("eta_start")),
            eta_end=_d(d.get("eta_end")),
            eta_time=d.get("eta_time"),
            description=d.get("description"),
            shipped_from=d.get("shipped_from"),
            order_id=d.get("order_id"),
            tracking=[str(t) for t in d.get("tracking") or []],
            shipment_ids=[str(s) for s in d.get("shipment_ids") or []],
            items=_int_or_none(d.get("items")),
            first_seen=_dtm(d.get("first_seen")),
            last_update=_dtm(d.get("last_update")),
            delivered_at=_dtm(d.get("delivered_at")),
            digest_date=_d(d.get("digest_date")),
            boxes={str(k): _int_or_none(v) for k, v in (d.get("boxes") or {}).items()},
            manual=d.get("manual"),
            prev_status=_int_or_none(d.get("prev_status")),
            ship_to=_places(d.get("ship_to")),
            legacy=bool(d.get("legacy")),
            msgid=str(d["msgid"]) if d.get("msgid") else None,
        )


def _merge(pkg: Package, ev: Event, msgid: str | None = None) -> None:
    """Apply one event to a package. Emails may arrive out of order."""
    when = ev.email_date
    newer = pkg.last_update is None or when >= pkg.last_update
    if ev.status is not None and pkg.status != S.DELIVERED:
        if ev.status > pkg.status:
            pkg.status, pkg.status_date = ev.status, when.date()
        elif (
            newer
            and pkg.status == S.OUT_FOR_DELIVERY
            and ev.status == S.IN_TRANSIT
            and pkg.status_date is not None
            and when.date() > pkg.status_date
        ):
            pkg.status, pkg.status_date = S.IN_TRANSIT, when.date()  # missed delivery, rescheduled
        elif ev.status == pkg.status and newer:
            pkg.status_date = when.date()
        if ev.status == S.DELIVERED:
            pkg.delivered_at = when
            pkg.exception = None
    if pkg.status != S.DELIVERED:
        if ev.exception and newer:
            pkg.exception = ev.exception
        elif newer and ev.status is not None and (ev.status >= S.OUT_FOR_DELIVERY or ev.eta_start):
            pkg.exception = None
        if ev.eta_start and (newer or pkg.eta_start is None):
            pkg.eta_start, pkg.eta_end = ev.eta_start, ev.eta_end or ev.eta_start
            pkg.eta_time = ev.eta_time
        elif ev.eta_time and newer and ev.eta_start is None:
            pkg.eta_time = ev.eta_time
        # A newer delay / exception / later date means it's no longer out for delivery today.
        if (
            newer
            and pkg.status == S.OUT_FOR_DELIVERY
            and ev.status in (None, S.IN_TRANSIT)
            and (ev.exception or (ev.eta_start and ev.eta_start > when.date()))
        ):
            pkg.status, pkg.status_date = S.IN_TRANSIT, when.date()
    if ev.carrier and (pkg.carrier is None or pkg.carrier == "unknown"):
        pkg.carrier = ev.carrier
    pkg.order_id = pkg.order_id or ev.order_id
    if ev.description and (not pkg.description or len(ev.description) > len(pkg.description)):
        pkg.description = ev.description
    pkg.shipped_from = pkg.shipped_from or ev.shipped_from
    for t in ev.tracking:
        if t not in pkg.tracking:
            pkg.tracking.append(t)
    for s in ev.shipment_ids:
        if s not in pkg.shipment_ids:
            pkg.shipment_ids.append(s)
    if ev.items is not None and (pkg.items is None or ev.status == S.ORDERED):
        pkg.items = ev.items
    pkg.first_seen = min(x for x in (pkg.first_seen, when) if x is not None)
    pkg.last_update = max(x for x in (pkg.last_update, when) if x is not None)
    _add_places(pkg, ev.ship_to)
    if msgid and (newer or pkg.msgid is None):
        pkg.msgid = msgid


def _add_places(pkg: "Package", places: list[Place]) -> bool:
    before = list(pkg.ship_to)
    for place in places:
        if place.located and place.key not in {p.key for p in pkg.ship_to}:
            pkg.ship_to.append(place)
    del pkg.ship_to[:-MAX_PLACES]
    return pkg.ship_to != before


@dataclass(frozen=True)
class PackageView:
    """One package as the dashboard and the voice assistant see it."""

    key: str
    item: str
    carrier: str
    status: str
    status_code: int
    expected: str | None
    expected_end: str | None
    window: str | None
    late: bool
    exception: str | None
    delivered_at: str | None
    tracking_last4: str | None
    last_update: str | None
    ticked: bool = False  # marked received by hand (left out of the public dict)

    def as_public_dict(self) -> dict[str, Any]:
        out = {
            "item": self.item,
            "carrier": self.carrier,
            "status": self.status,
            "expected": self.expected,
            "window": self.window,
            "late": self.late,
            "exception": self.exception,
            "delivered_at": self.delivered_at,
            "tracking_last4": self.tracking_last4,
            "last_update": self.last_update,
        }
        if self.expected_end and self.expected_end != self.expected:
            out["expected_end"] = self.expected_end
        return {k: v for k, v in out.items() if v not in (None, False)}


@dataclass(frozen=True)
class TrackerSnapshot:
    as_of: dt.date
    packages: tuple[PackageView, ...]
    arriving_today: tuple[PackageView, ...]
    out_for_delivery: tuple[PackageView, ...]
    on_the_way: tuple[PackageView, ...]
    ordered: tuple[PackageView, ...]
    delivered_today: tuple[PackageView, ...]
    ready_for_pickup: tuple[PackageView, ...]
    late: tuple[PackageView, ...]
    mail_pieces_today: int | None
    hidden: int = 0  # packages left out by the "addresses to show" option
    ticked_today: tuple[PackageView, ...] = ()  # ticked by hand today; not in delivered_today
    last_check: dt.datetime | None = None  # filled in by the coordinator
    last_error: str | None = None

    @property
    def active(self) -> tuple[PackageView, ...]:
        return tuple(p for p in self.packages if p.status_code <= S.AVAILABLE_FOR_PICKUP)


CARRIER_NAMES = {
    "usps": "USPS",
    "ups": "UPS",
    "fedex": "FedEx",
    "amazon": "Amazon",
    "dhl": "DHL",
    "ontrac": "OnTrac",
    "gofo": "GOFO",
}


class Registry:
    """All remembered packages plus scan bookkeeping."""

    def __init__(self) -> None:
        self.packages: dict[str, Package] = {}
        self.tombstones: dict[str, str] = {}  # alias -> iso date the package was retired
        self.seen: dict[str, str] = {}  # Gmail X-GM-MSGID -> iso date first processed
        self.cursors: dict[str, dict[str, int]] = {}  # folder role -> {uidvalidity, last_uid}
        self.mail: dict[str, int] = {}  # iso digest date -> mail pieces
        self.last_ok: dt.datetime | None = None  # last time Gmail was actually read
        self.backfill_pending = False  # re-read recent mail once, only to learn delivery addresses

    # ---- persistence -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "packages": [p.to_dict() for p in self.packages.values()],
            "tombstones": dict(self.tombstones),
            "seen": dict(self.seen),
            "cursors": {k: dict(v) for k, v in self.cursors.items()},
            "mail": dict(self.mail),
            "last_ok": _iso(self.last_ok),
            "backfill": self.backfill_pending,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Registry":
        reg = cls()
        if not data:
            return reg
        for raw in data.get("packages") or []:
            try:
                pkg = Package.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                continue
            reg.packages[pkg.key] = pkg
        reg.tombstones = {str(k): str(v) for k, v in (data.get("tombstones") or {}).items()}
        reg.seen = {str(k): str(v) for k, v in (data.get("seen") or {}).items()}
        reg.cursors = {
            str(k): {
                "uidvalidity": int(v.get("uidvalidity", 0)),
                "last_uid": int(v.get("last_uid", 0)),
            }
            for k, v in (data.get("cursors") or {}).items()
            if isinstance(v, dict)
        }
        reg.mail = {str(k): int(v) for k, v in (data.get("mail") or {}).items()}
        reg.last_ok = _dtm(data.get("last_ok"))
        reg.backfill_pending = bool(data.get("backfill"))
        return reg

    # ---- applying mail ---------------------------------------------------
    def _new_key(self, base: str) -> str:
        key, n = base, 2
        while key in self.packages:
            key, n = f"{base}#{n}", n + 1
        return key

    def apply_event(self, ev: Event, msgid: str | None = None) -> bool:
        strong = {f"t:{t}" for t in ev.tracking} | {f"s:{s}" for s in ev.shipment_ids}
        order = f"o:{ev.order_id}" if ev.order_id else None
        if not strong and not order:
            return False
        if strong:
            if strong & self.tombstones.keys():
                return False  # this box was already delivered or retired
            matches = [p for p in self.packages.values() if p.strong_aliases & strong]
            # "Shipped" always announces a new box, so only later emails can be a renamed one.
            if not matches and order and ev.shipment_ids and ev.status != S.IN_TRANSIT:
                matches = self._renamed_box(ev)
            if not matches and order and not ev.shipment_ids:
                # A tracking number for an order we only knew by its order number: upgrade it.
                matches = [
                    p
                    for p in self.packages.values()
                    if p.is_order_only and p.order_alias == order and p.status != S.DELIVERED
                ][:1]
            if not matches:
                base = f"t:{ev.tracking[0]}" if ev.tracking else f"s:{ev.shipment_ids[0]}"
                pkg = Package(key=self._new_key(base), carrier=ev.carrier)
                self.packages[pkg.key] = pkg
                matches = [pkg]
            keep = matches[0]
            for other in matches[1:]:  # one email naming two records' numbers joins them
                self._absorb(keep, other)
            if order and not ev.shipment_ids:
                # A store email naming both the order and its tracking number folds in the
                # order-only record (Amazon orders keep theirs; see _order_covered).
                order_only = [
                    p
                    for p in self.packages.values()
                    if p is not keep
                    and p.is_order_only
                    and p.order_alias == order
                    and p.status != S.DELIVERED
                    and p.carrier != "amazon"
                ]
                for p in order_only:
                    self._absorb(keep, p)
            _merge(keep, ev, msgid)
            if order and ev.shipment_ids:
                self._register_box(keep)
            return True

        # Order number only (e.g. "Ordered 3 items", a store email without tracking).
        if order in self.tombstones:
            return False
        matches = [p for p in self.packages.values() if p.is_order_only and p.order_alias == order]
        if not matches:
            # A store's order-level update for boxes we already track: apply it to those boxes
            # rather than inventing another package.
            boxes = [
                p
                for p in self.packages.values()
                if p.order_id == ev.order_id and p.carrier != "amazon"
            ]
            if boxes:
                for p in boxes:
                    _merge(p, ev, msgid)
                return True
            pkg = Package(key=self._new_key(order), carrier=ev.carrier)
            self.packages[pkg.key] = pkg
            matches = [pkg]
            # Boxes seen before the "Ordered" email.
            for box in self.packages.values():
                if box.order_id == ev.order_id and box.shipment_ids:
                    pkg.boxes[box.key] = box.items
        _merge(matches[0], ev, msgid)
        return True

    def _renamed_box(self, ev: Event) -> list[Package]:
        """The box an Amazon email means when its box id differs from the "Shipped" one.

        Only when the order has exactly one box still on its way (or one with the same items).
        """
        open_boxes = [
            p
            for p in self.packages.values()
            if p.order_id == ev.order_id and p.shipment_ids and p.status < S.DELIVERED
        ]
        if len(open_boxes) > 1:
            same_items = [
                p
                for p in open_boxes
                if (ev.description and p.description == ev.description)
                or (ev.items is not None and p.items == ev.items)
            ]
            open_boxes = same_items or open_boxes
        if len(open_boxes) == 1 and (ev.status is None or ev.status >= open_boxes[0].status):
            return open_boxes
        return []

    def _register_box(self, box: Package) -> None:
        """Record an Amazon box on its order, so the order stays hidden once the box is gone."""
        for parent in self.packages.values():
            if (
                parent.is_order_only
                and parent.order_id == box.order_id
                and parent.carrier == "amazon"
                and parent.boxes.get(box.key) is None
            ):
                parent.boxes[box.key] = box.items

    def _absorb(self, keep: Package, other: Package) -> None:
        self.packages.pop(other.key, None)
        replay = Event(
            source="merge",
            status=other.status,
            email_date=other.last_update or other.first_seen or dt.datetime.now(dt.UTC),
            exception=other.exception,
            carrier=other.carrier,
            tracking=list(other.tracking),
            order_id=other.order_id,
            eta_start=other.eta_start,
            eta_end=other.eta_end,
            eta_time=other.eta_time,
            description=other.description,
            shipped_from=other.shipped_from,
            shipment_ids=list(other.shipment_ids),
            items=other.items,
            ship_to=list(other.ship_to),
        )
        _merge(keep, replay, other.msgid)
        keep.legacy = keep.legacy or other.legacy
        if other.delivered_at and keep.status == S.DELIVERED:
            keep.delivered_at = keep.delivered_at or other.delivered_at

    def apply_digest(self, dg: Digest, msgid: str | None = None) -> bool:
        changed = False
        if dg.digest_date and dg.mailpieces is not None:
            key = dg.digest_date.isoformat()
            if self.mail.get(key) != dg.mailpieces:
                self.mail[key] = dg.mailpieces
                changed = True
        for trk, section in dg.packages.items():
            if section == "outbound":
                continue  # mail being sent, not received
            ev = Event(
                source="usps_digest",
                status=S.LABEL_CREATED if section == "awaiting" else S.IN_TRANSIT,
                email_date=dg.email_date,
                carrier="usps",
                tracking=[trk],
            )
            if dg.digest_date and section == "today":
                ev.eta_start = ev.eta_end = dg.digest_date
            elif dg.digest_date and section == "soon":
                ev.eta_start = dg.digest_date + dt.timedelta(days=1)
                ev.eta_end = dg.digest_date + dt.timedelta(days=2)
            if self.apply_event(ev, msgid):
                changed = True
                for pkg in self.packages.values():
                    if (
                        trk in pkg.tracking
                        and dg.digest_date
                        and (pkg.digest_date is None or dg.digest_date > pkg.digest_date)
                    ):
                        pkg.digest_date = dg.digest_date
        return changed

    def apply(self, results: Iterable[tuple[str, Event | Digest | None]], now: dt.datetime) -> bool:
        """Apply parsed mail (msgid, result), oldest first; remember every msgid as seen."""
        changed = False
        items = list(results)
        stamp = now.date().isoformat()
        for msgid, _ in items:
            if msgid not in self.seen:
                self.seen[msgid] = stamp
                changed = True
        parsed = [(m, r) for m, r in items if r is not None]
        parsed.sort(key=lambda mr: mr[1].email_date)
        for m, r in parsed:
            changed |= self.apply_digest(r, m) if isinstance(r, Digest) else self.apply_event(r, m)
        return changed

    # ---- manual ticks ----------------------------------------------------
    def set_received(self, key: str, received: bool, now: dt.datetime) -> bool:
        """Tick (received) or untick a package by hand. False when nothing changes."""
        pkg = self.packages.get(key)
        if pkg is None:
            return False
        if received:
            if pkg.status == S.DELIVERED:
                if pkg.manual == "not_received":
                    pkg.manual = None
                    return True
                return False
            pkg.prev_status, pkg.manual = int(pkg.status), "received"
            pkg.status, pkg.status_date, pkg.delivered_at, pkg.exception = (
                S.DELIVERED,
                now.date(),
                now,
                None,
            )
            return True
        if pkg.manual == "received" and pkg.status == S.DELIVERED:  # undo a manual tick
            pkg.status = Status(pkg.prev_status or S.IN_TRANSIT)
            pkg.delivered_at, pkg.manual, pkg.prev_status = None, None, None
            return True
        if pkg.status != S.DELIVERED and pkg.manual != "not_received":  # "it hasn't really arrived"
            pkg.manual = "not_received"
            return True
        return False

    # ---- Amazon orders vs boxes -------------------------------------------
    def _order_covered(self, pkg: Package) -> bool:
        """An Amazon "Ordered" record whose items have all shipped as boxes we track."""
        if pkg.carrier != "amazon" or not pkg.is_order_only or pkg.status != S.ORDERED:
            return False
        if not pkg.order_id:
            return False
        boxes = dict(pkg.boxes)
        for p in self.packages.values():  # live boxes too, in case they were never recorded
            if p.order_id == pkg.order_id and p.shipment_ids:
                boxes.setdefault(p.key, p.items)
        if not boxes:
            return False
        if pkg.items is None or any(v is None for v in boxes.values()):
            return True
        return sum(v or 0 for v in boxes.values()) >= pkg.items

    # ---- housekeeping ----------------------------------------------------
    def probably_delivered(self, pkg: Package, today: dt.date) -> bool:
        """Past its date, no news since, and we have actually read mail since then.

        USPS: today's Informed Delivery digest has arrived and no longer lists it.
        Anyone: two or more days past the expected date.
        A delay or problem holds it open until a week passes with no news at all.
        """
        if pkg.status >= S.AVAILABLE_FOR_PICKUP or not pkg.eta_end or pkg.manual == "not_received":
            return False
        if pkg.status < S.LABEL_CREATED and pkg.carrier != "amazon":
            return False  # only Amazon orders count without any shipping news
        if pkg.eta_end >= today:
            return False
        if self.last_ok is None or self.last_ok.date() <= pkg.eta_end:
            return False  # haven't been able to read mail since it was due
        if pkg.exception:
            if pkg.last_update is None:
                return False
            return (self.last_ok.date() - pkg.last_update.date()).days >= EXCEPTION_QUIET_DAYS
        quiet = pkg.last_update is None or pkg.last_update.date() <= pkg.eta_end
        if pkg.carrier == "usps" and today.isoformat() in self.mail and pkg.digest_date != today:
            return quiet
        return quiet and (today - pkg.eta_end).days >= 2

    def _retire(
        self, pkg: Package, now: dt.datetime, keep: int, stale_days: int
    ) -> tuple[bool, bool]:
        """Whether to drop a package, and whether to leave a tombstone for it."""
        today = now.date()
        if pkg.status == S.DELIVERED:
            done = (pkg.delivered_at or now).astimezone(now.tzinfo).date()
            gone = (today - done).days >= keep
            return gone, gone
        if self._order_covered(pkg):
            return True, False  # every item shipped; the boxes carry on
        if self._dropped_from_digest(pkg):
            return True, False
        if pkg.eta_end and self.probably_delivered(pkg, today):
            gone = (today - pkg.eta_end).days > keep + 1
            return gone, gone
        if pkg.eta_end and pkg.eta_end >= today - dt.timedelta(days=7):
            return False, False  # still due soon (pre-orders, slow shipments)
        last = (pkg.last_update or now).astimezone(now.tzinfo).date()
        if (today - last).days > stale_days:
            return True, False
        if pkg.eta_end and (today - pkg.eta_end).days > 14 and (today - last).days > 14:
            return True, False
        return False, False

    def _dropped_from_digest(self, pkg: Package) -> bool:
        """A USPS "awaiting from sender" package that a newer digest no longer lists."""
        if not (pkg.status == S.LABEL_CREATED and pkg.carrier == "usps" and pkg.digest_date):
            return False
        if not self.mail:
            return False
        newest = max(_d(k) or pkg.digest_date for k in self.mail)
        no_news = pkg.last_update is None or pkg.last_update.date() <= pkg.digest_date
        return pkg.digest_date < newest and no_news

    def expire(self, now: dt.datetime, keep_delivered_days: int, stale_days: int = 30) -> bool:
        """Retire finished packages. Only call after a successful Gmail check."""
        today = now.date()
        keep = max(keep_delivered_days, 1)  # never drop a package on its delivery day
        changed = False
        for key, pkg in list(self.packages.items()):
            drop, tombstone = self._retire(pkg, now, keep, stale_days)
            if drop:
                del self.packages[key]
                if tombstone:
                    for alias in pkg.aliases:
                        self.tombstones[alias] = today.isoformat()
                changed = True
        for book, days in ((self.tombstones, TOMBSTONE_DAYS), (self.seen, SEEN_DAYS)):
            for k, v in list(book.items()):
                d = _d(v)
                if d is None or (today - d).days > days:
                    del book[k]
                    changed = True
        for k in list(self.mail):
            d = _d(k)
            if d is None or (today - d).days > MAIL_DAYS:
                del self.mail[k]
                changed = True
        return changed

    # ---- views -----------------------------------------------------------
    def _view(self, pkg: Package, today: dt.date) -> PackageView:
        status: int = pkg.status
        if status == S.OUT_FOR_DELIVERY and (
            (pkg.status_date and pkg.status_date < today)
            or (pkg.eta_start and pkg.eta_start > today)
        ):
            status = S.IN_TRANSIT  # "out for delivery" only means today
        if self.probably_delivered(pkg, today):
            status = PROBABLY_DELIVERED
        late = status < S.AVAILABLE_FOR_PICKUP and bool(pkg.eta_end and pkg.eta_end < today)
        carrier = CARRIER_NAMES.get(pkg.carrier or "", (pkg.carrier or "store").title())
        if pkg.description:
            item = pkg.description
        elif pkg.shipped_from:
            sender = pkg.shipped_from
            if sender == sender.upper():
                sender = sender.title()  # "ACME OUTDOORS" -> "Acme Outdoors"
            kind = "order" if status == S.ORDERED else "package"
            item = f"{kind} from {sender}"
        else:
            item = f"{carrier} package"
        trk = pkg.tracking[0] if pkg.tracking else None
        return PackageView(
            key=pkg.key,
            item=item,
            carrier=carrier,
            status=STATUS_WORDS.get(status, "probably delivered"),  # type: ignore[call-overload]
            status_code=int(status),
            expected=_iso(pkg.eta_start) if status < S.AVAILABLE_FOR_PICKUP else None,
            expected_end=_iso(pkg.eta_end) if status < S.AVAILABLE_FOR_PICKUP else None,
            window=pkg.eta_time if status in (S.IN_TRANSIT, S.OUT_FOR_DELIVERY) else None,
            late=late,
            exception=pkg.exception,
            delivered_at=pkg.delivered_at.isoformat(timespec="minutes")
            if pkg.delivered_at
            else None,
            tracking_last4=trk[-4:] if trk else None,
            last_update=pkg.last_update.isoformat(timespec="minutes") if pkg.last_update else None,
            ticked=pkg.manual == "received",
        )

    # ---- whose package is it ----------------------------------------------
    def places(self, pkg: Package) -> list[Place]:
        """The package's delivery addresses; a box without any borrows its order's (and back)."""
        if pkg.ship_to or not pkg.order_id:
            return pkg.ship_to
        found: list[Place] = []
        for p in self.packages.values():
            if p.order_id == pkg.order_id:
                found.extend(p.ship_to)
        return found

    def visible(self, pkg: Package, flt: AddressFilter) -> bool:
        if not flt.active:
            return True
        if flt.informed_delivery_is_mine and pkg.digest_date is not None:
            return True  # USPS Informed Delivery only lists mail coming to its own address
        verdict = flt.verdict(self.places(pkg))
        if verdict is None:
            return flt.show_unknown or pkg.legacy
        return verdict

    def backfill_addresses(self, results: Iterable[tuple[str, Event | Digest | None]]) -> int:
        """Attach delivery addresses and email ids from re-read mail to packages we track.

        Never creates a package or changes anything else about one, so re-reading old mail can't
        bring back retired orders. Returns how many packages learned something.
        """
        updated: set[str] = set()
        mail: list[tuple[str, Event]] = []
        for msgid, r in results:
            if isinstance(r, Event):
                mail.append((msgid, r))
            elif isinstance(r, Digest):
                # Informed Delivery: a stand-in event (tracking number only) per listed package.
                for t, section in r.packages.items():
                    if section != "outbound":
                        ev = Event(
                            source="usps_digest", status=None, email_date=r.email_date, tracking=[t]
                        )
                        mail.append((msgid, ev))
        mail.sort(key=lambda me: me[1].email_date)
        for msgid, ev in mail:  # oldest first, so the newest email's id is the one kept
            strong = {f"t:{t}" for t in ev.tracking} | {f"s:{s}" for s in ev.shipment_ids}
            targets = [p for p in self.packages.values() if p.strong_aliases & strong]
            if not targets and ev.order_id:
                targets = [p for p in self.packages.values() if p.order_id == ev.order_id]
            for pkg in targets:
                if _add_places(pkg, ev.ship_to):
                    updated.add(pkg.key)
                not_newer = pkg.last_update is None or ev.email_date <= pkg.last_update
                if msgid and pkg.msgid != msgid and not_newer:
                    pkg.msgid = msgid
                    updated.add(pkg.key)
        return len(updated)

    def snapshot(self, now: dt.datetime, flt: AddressFilter | None = None) -> TrackerSnapshot:
        today = now.date()
        flt = flt or AddressFilter()
        current = [p for p in self.packages.values() if not self._order_covered(p)]
        shown = [p for p in current if self.visible(p, flt)]
        pairs = [(self._view(p, today), p) for p in shown]
        # Not yet delivered first, then by expected date, then by name.
        pairs.sort(
            key=lambda vp: (vp[0].status_code >= S.DELIVERED, vp[0].expected or "9999", vp[0].item)
        )
        views = tuple(v for v, _ in pairs)

        def done_today(p: Package) -> bool:
            if p.status != S.DELIVERED or not p.delivered_at:
                return False
            return p.delivered_at.astimezone(now.tzinfo).date() == today

        def arriving(v: PackageView) -> bool:
            if v.status_code == S.OUT_FOR_DELIVERY:
                return True
            return (
                S.LABEL_CREATED <= v.status_code < S.OUT_FOR_DELIVERY  # not unshipped orders
                and v.expected is not None
                and v.expected == today.isoformat()
                and (v.expected_end in (None, v.expected))
            )

        return TrackerSnapshot(
            as_of=today,
            packages=views,
            arriving_today=tuple(v for v in views if arriving(v)),
            out_for_delivery=tuple(v for v in views if v.status_code == S.OUT_FOR_DELIVERY),
            on_the_way=tuple(
                v for v in views if S.IN_TRANSIT <= v.status_code <= S.AVAILABLE_FOR_PICKUP
            ),
            ordered=tuple(v for v in views if v.status_code == S.ORDERED),
            delivered_today=tuple(v for v, p in pairs if done_today(p) and not v.ticked),
            ticked_today=tuple(v for v, p in pairs if done_today(p) and v.ticked),
            ready_for_pickup=tuple(v for v in views if v.status_code == S.AVAILABLE_FOR_PICKUP),
            late=tuple(v for v in views if v.late),
            mail_pieces_today=self.mail.get(today.isoformat()),
            hidden=len(current) - len(shown),
        )
