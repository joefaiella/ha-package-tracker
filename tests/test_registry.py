"""The package registry: merging emails, retiring packages, "probably delivered" and ticks."""

import datetime as dt
import json

from custom_components.package_tracker import kb, registry

from .common import (
    BOX_KITCHEN,
    BOX_LAMP,
    FEDEX_OFD,
    NOW,
    ORDER_KITCHEN,
    ORDER_LAMP,
    SHIPPING,
    TZ,
    UPS,
    USPS_AWAITING,
    USPS_EXPECTED,
    WALMART_ORDER,
    event,
    fake_mail,
    parse,
)

S = kb.Status


def replay(names, now=NOW) -> registry.Registry:
    reg = registry.Registry()
    reg.apply([(f"m{i}", parse(name)) for i, name in enumerate(names)], now)
    reg.last_ok = now  # as after a successful Gmail check
    reg.expire(now, keep_delivered_days=2)
    return reg


def at(day: int, hour: int = 12, month: int = 10) -> dt.datetime:
    return dt.datetime(2026, month, day, hour, 0, tzinfo=TZ)


# ------------------------------------------------------------ the week ---


def test_replay_of_the_week():
    snap = replay(SHIPPING).snapshot(NOW)
    today = {(v.carrier, v.tracking_last4 or v.item) for v in snap.arriving_today}
    assert today == {
        ("Amazon", "Coffee Beans"),
        ("USPS", "7209"),
        ("UPS", "5675"),
        ("FedEx", "0496"),
    }
    assert [v.carrier for v in snap.out_for_delivery] == ["Amazon", "FedEx", "UPS"]
    assert len(snap.on_the_way) == 10
    assert {v.item for v in snap.ordered} == {"Kitchen", "Wireless Mouse"}
    assert {(v.carrier, v.item) for v in snap.delivered_today} == {
        ("USPS", "USPS package"),
        ("Store", "package from Etsy"),
    }
    assert snap.late == () and snap.mail_pieces_today == 3
    probable = {v.tracking_last4 for v in snap.packages if v.status == "probably delivered"}
    assert probable == {"4189"}  # USPS out for delivery on Tuesday, not in today's digest
    awaiting = next(v for v in snap.packages if v.tracking_last4 == USPS_AWAITING[-4:])
    assert awaiting.status == "label created" and awaiting not in snap.on_the_way
    assert "Desk Lamp" not in {v.item for v in snap.packages}  # delivered two days ago
    assert len(snap.packages) == 19


def test_deleting_emails_changes_nothing():
    reg = replay(SHIPPING)
    before = reg.snapshot(NOW)
    assert reg.apply([], NOW) is False  # next poll: every email deleted, nothing new
    assert reg.snapshot(NOW) == before


def test_storage_round_trip():
    reg = replay(SHIPPING)
    again = registry.Registry.from_dict(json.loads(json.dumps(reg.to_dict())))
    assert again.to_dict() == reg.to_dict()
    assert again.snapshot(NOW) == reg.snapshot(NOW)


def test_bad_stored_package_is_skipped():
    data = {"packages": [{"nokey": 1}, {"key": "t:1", "status": 30, "tracking": ["1"]}]}
    assert list(registry.Registry.from_dict(data).packages) == ["t:1"]


# ----------------------------------------------------- status over time ---


def test_out_for_delivery_only_means_today():
    reg = replay(["fedex_out_for_delivery.eml"])
    view = reg.snapshot(NOW + dt.timedelta(days=1)).packages[0]
    assert view.status == "in transit" and view.late is True


def test_delivered_is_final_and_tombstoned():
    evening = at(8, 20)
    reg = replay(["walmart_shipped.eml", "walmart_delivered.eml"], now=evening)
    assert [p.status for p in reg.snapshot(evening).delivered_today] == ["delivered"]
    later = at(11, 9)
    reg.expire(later, keep_delivered_days=2)
    assert reg.packages == {} and f"o:{WALMART_ORDER}" in reg.tombstones
    # The old "Shipped" email processed again (e.g. found in Trash) must not bring it back.
    reg.apply([("again", parse("walmart_shipped.eml"))], later)
    assert reg.packages == {}


def test_delivered_amazon_box_stays_retired():
    evening = at(7, 20)
    reg = replay(["amazon_delivered.eml"], now=evening)
    assert [v.item for v in reg.snapshot(evening).delivered_today] == ["Desk Lamp"]
    later = at(10, 9)
    reg.expire(later, keep_delivered_days=2)
    assert reg.packages == {} and f"s:{BOX_LAMP}" in reg.tombstones
    shipped = event(
        carrier="amazon", order_id=ORDER_LAMP, shipment_ids=[BOX_LAMP], email_date=at(5)
    )
    assert reg.apply_event(shipped) is False


def test_order_and_tracking_link_up():
    reg = registry.Registry()
    t0 = at(1, 9)
    reg.apply_event(event(status=S.ORDERED, email_date=t0, order_id="etsy-1"))
    reg.apply_event(
        event(email_date=t0 + dt.timedelta(days=1), carrier="usps", tracking=[USPS_EXPECTED])
    )
    reg.apply_event(
        event(email_date=t0 + dt.timedelta(days=2), order_id="etsy-1", tracking=[USPS_EXPECTED])
    )
    assert len(reg.packages) == 1
    pkg = next(iter(reg.packages.values()))
    assert pkg.order_id == "etsy-1" and pkg.tracking == [USPS_EXPECTED]
    assert pkg.status == S.IN_TRANSIT


def test_out_of_order_emails():
    reg = registry.Registry()
    t0 = at(1, 9)
    delivered = event(status=S.DELIVERED, email_date=t0 + dt.timedelta(days=2), tracking=["A1"])
    shipped = event(email_date=t0, tracking=["A1"], eta_start=t0.date())
    reg.apply([("2", delivered), ("1", shipped)], t0 + dt.timedelta(days=2))
    assert next(iter(reg.packages.values())).status == S.DELIVERED


def test_keep_zero_days_still_shows_delivered_today():
    reg = registry.Registry()
    reg.apply_event(event(status=S.DELIVERED, tracking=[FEDEX_OFD]))
    reg.last_ok = NOW
    reg.expire(NOW, keep_delivered_days=0)
    assert len(reg.snapshot(NOW).delivered_today) == 1


def test_same_day_delay_ends_out_for_delivery():
    reg = registry.Registry()
    order = "111-0000000-0000002"
    reg.apply_event(event(status=S.OUT_FOR_DELIVERY, order_id=order, carrier="amazon"))
    reg.apply_event(
        event(
            status=None,
            order_id=order,
            exception="running late",
            email_date=NOW + dt.timedelta(hours=9),
            eta_start=NOW.date() + dt.timedelta(days=1),
        )
    )
    snap = reg.snapshot(NOW + dt.timedelta(hours=10))
    assert snap.out_for_delivery == () and snap.arriving_today == ()


def test_running_late_moves_the_expected_date():
    reg = replay(["amazon_ordered.eml", "amazon_shipped.eml", "amazon_delivery_update.eml"])
    box = next(p for p in reg.packages.values() if p.shipment_ids)
    assert box.exception == "running late"
    assert (box.eta_start, box.eta_end) == (at(10).date(), at(11).date())
    assert reg.snapshot(NOW).arriving_today == ()


# ------------------------------------------------------------ Amazon boxes ---


def test_amazon_boxes_are_separate_packages():
    """One order, two boxes: the first box's delivery must not hide the second."""
    reg = replay(["amazon_ordered.eml", "amazon_shipped.eml"])  # 3 items ordered, 1 shipped
    t1 = NOW + dt.timedelta(hours=3)
    delivered = event(
        status=S.DELIVERED,
        email_date=t1,
        carrier="amazon",
        order_id=ORDER_KITCHEN,
        shipment_ids=[BOX_KITCHEN],
        items=1,
        description="Kitchen",
    )
    reg.apply([("d1", delivered)], t1)
    snap = reg.snapshot(t1)
    assert [v.status for v in snap.delivered_today] == ["delivered"]
    assert any(v.status == "ordered" and v.item == "Kitchen" for v in snap.ordered)  # 2 not shipped
    # The second box ships and is due today: it's a package of its own.
    t2 = t1 + dt.timedelta(days=1)
    second = event(
        email_date=t2,
        carrier="amazon",
        order_id=ORDER_KITCHEN,
        shipment_ids=["SECONDBOX1"],
        items=2,
        eta_start=t2.date(),
        eta_end=t2.date(),
    )
    reg.apply([("s2", second)], t2)
    snap = reg.snapshot(t2)
    box2 = [v for v in snap.arriving_today if v.carrier == "Amazon"]
    assert len(box2) == 1 and box2[0].status == "in transit"
    assert all(v.item != "Kitchen" for v in snap.ordered)  # all 3 items have now shipped
    boxes = [p for p in reg.packages.values() if p.order_id == ORDER_KITCHEN and p.shipment_ids]
    assert len(boxes) == 2


def test_order_number_never_merges_two_tracking_numbers():
    reg = registry.Registry()
    order = "shopify-a@x-1042"
    reg.apply_event(event(order_id=order, tracking=[USPS_EXPECTED], carrier="usps"))
    reg.apply_event(event(order_id=order, tracking=[UPS], carrier="ups"))
    assert len(reg.packages) == 2
    reg.apply_event(event(status=S.DELIVERED, tracking=[USPS_EXPECTED]))
    later = NOW + dt.timedelta(days=5)
    reg.last_ok = later
    reg.expire(later, keep_delivered_days=2)
    # The second box isn't blocked by the first one's tombstone.
    assert reg.apply_event(
        event(status=S.OUT_FOR_DELIVERY, email_date=later, order_id=order, tracking=[UPS])
    )
    assert [p.status for p in reg.packages.values()] == [S.OUT_FOR_DELIVERY]


def test_order_stays_hidden_after_its_box_is_retired():
    reg = registry.Registry()
    t0 = at(27, 20, month=9)
    amazon = {"carrier": "amazon", "order_id": "111-0000000-0000004", "items": 1}
    ordered = event(
        status=S.ORDERED,
        email_date=t0,
        eta_start=dt.date(2026, 9, 28),
        eta_end=dt.date(2026, 9, 28),
        description="Computer Accessories",
        **amazon,
    )
    shipped = event(email_date=t0 + dt.timedelta(hours=10), shipment_ids=["BOX1"], **amazon)
    delivered = event(
        status=S.DELIVERED,
        email_date=t0 + dt.timedelta(days=1),
        shipment_ids=["9999", "BOX1"],
        **amazon,
    )
    reg.apply([("o", ordered), ("s", shipped), ("d", delivered)], t0)
    later = at(9)
    reg.last_ok = later
    reg.expire(later, keep_delivered_days=2)
    snap = reg.snapshot(later)
    assert snap.ordered == () and snap.packages == ()


def test_order_email_after_its_boxes():
    reg = registry.Registry()
    t0 = at(1, 9)
    amazon = {"carrier": "amazon", "order_id": "111-0000000-0000009", "items": 2}
    shipped = event(email_date=t0, shipment_ids=["BOX9"], **amazon)
    ordered = event(status=S.ORDERED, email_date=t0, **amazon)
    reg.apply([("s", shipped), ("o", ordered)], t0)
    assert reg.snapshot(t0).ordered == ()


def test_amazon_box_id_changes_between_shipped_and_delivered():
    """Amazon sometimes links the same box under a different id in the "Delivered" email."""
    reg = registry.Registry()
    t0 = at(5, 21)
    amazon = {
        "carrier": "amazon",
        "order_id": "111-0000000-0000008",
        "items": 2,
        "description": "Tea, Honey",
    }
    shipped = event(email_date=t0, shipment_ids=["BoxA7k2Q1"], **amazon)
    delivered = event(
        status=S.DELIVERED,
        email_date=t0 + dt.timedelta(hours=19),
        shipment_ids=["742913058816204", "BoxR9m4T6"],
        **amazon,
    )
    reg.apply([("s", shipped), ("d", delivered)], t0 + dt.timedelta(hours=20))
    assert [p.status for p in reg.packages.values()] == [S.DELIVERED]


def test_two_open_boxes_pick_by_items():
    reg = registry.Registry()
    order = "111-0000000-0000077"
    reg.apply_event(
        event(carrier="amazon", order_id=order, shipment_ids=["A1"], items=1, description="Lamps")
    )
    reg.apply_event(
        event(carrier="amazon", order_id=order, shipment_ids=["B2"], items=2, description="Rugs")
    )
    reg.apply_event(
        event(
            status=S.DELIVERED,
            email_date=NOW + dt.timedelta(hours=1),
            carrier="amazon",
            order_id=order,
            shipment_ids=["ZZZ"],
            items=2,
            description="Rugs",
        )
    )
    assert {p.description: p.status for p in reg.packages.values()} == {
        "Lamps": S.IN_TRANSIT,
        "Rugs": S.DELIVERED,
    }


def test_preorder_with_future_date_is_kept():
    reg = registry.Registry()
    reg.apply_event(
        event(
            status=S.ORDERED,
            email_date=at(1, 9),
            order_id="111-0000000-0000001",
            carrier="amazon",
            eta_start=dt.date(2026, 11, 14),
            eta_end=dt.date(2026, 11, 14),
        )
    )
    nov1 = at(1, 9, month=11)
    reg.last_ok = nov1
    reg.expire(nov1, keep_delivered_days=2)
    assert reg.packages


# ------------------------------------------------------ probably delivered ---


def test_outage_never_counts_as_no_news():
    reg = replay(["usps_expected.eml"])  # due today
    pkg = next(iter(reg.packages.values()))
    for day in range(1, 6):  # the last good Gmail check stays on the due date
        assert not reg.probably_delivered(pkg, (NOW + dt.timedelta(days=day)).date())


def test_exception_blocks_probably_delivered():
    reg = registry.Registry()
    reg.apply_event(
        event(
            status=S.OUT_FOR_DELIVERY,
            tracking=[FEDEX_OFD],
            eta_start=NOW.date(),
            eta_end=NOW.date(),
        )
    )
    reg.apply_event(
        event(
            status=None,
            tracking=[FEDEX_OFD],
            exception="delivery exception",
            email_date=NOW + dt.timedelta(hours=8),
        )
    )
    later = NOW + dt.timedelta(days=4)
    reg.last_ok = later
    pkg = next(iter(reg.packages.values()))
    assert pkg.status == S.IN_TRANSIT  # the exception also ended "out for delivery"
    assert not reg.probably_delivered(pkg, later.date())
    reg.expire(later, keep_delivered_days=2)
    assert reg.packages  # still waiting, not retired


def test_amazon_order_with_no_shipping_news_eventually_probably_delivered():
    reg = registry.Registry()
    reg.apply_event(
        event(
            status=S.ORDERED,
            email_date=at(27, 21, month=9),
            carrier="amazon",
            order_id="113-0000000-0000005",
            eta_start=dt.date(2026, 9, 28),
            eta_end=dt.date(2026, 9, 28),
        )
    )
    reg.last_ok = NOW
    snap = reg.snapshot(NOW)
    assert snap.ordered == () and snap.packages[0].status == "probably delivered"


def test_stale_awaiting_from_sender_is_dropped_and_label_created_is_not_on_the_way():
    reg = registry.Registry()
    d1 = at(6, 7)
    first = kb.Digest(
        digest_date=d1.date(), email_date=d1, mailpieces=1, packages={USPS_AWAITING: "awaiting"}
    )
    reg.apply([("dg1", first)], d1)
    assert reg.snapshot(d1).on_the_way == ()  # a label isn't a package on its way
    assert len(reg.snapshot(d1).packages) == 1
    d2 = at(9, 7)
    reg.apply(
        [("dg2", kb.Digest(digest_date=d2.date(), email_date=d2, mailpieces=0, packages={}))], d2
    )
    reg.last_ok = d2
    reg.expire(d2, keep_delivered_days=2)
    assert reg.packages == {}


DELAYED_ORDER = "113-0000000-0000006"


def delayed_order() -> registry.Registry:
    """Ordered for next-day delivery, a "Delay in shipping" email the next day, then nothing."""
    reg = registry.Registry()
    t0 = at(27, 21, month=9)
    ordered = event(
        status=S.ORDERED,
        email_date=t0,
        carrier="amazon",
        order_id=DELAYED_ORDER,
        eta_start=dt.date(2026, 9, 28),
        eta_end=dt.date(2026, 9, 28),
        description="Bookshelf",
    )
    delay = event(
        status=None,
        email_date=t0 + dt.timedelta(hours=26),
        carrier="amazon",
        order_id=DELAYED_ORDER,
        exception="shipping delayed",
    )
    reg.apply([("o", ordered), ("d", delay)], t0)
    return reg


def test_shipping_delay_keeps_the_order_open():
    reg = delayed_order()
    reg.last_ok = at(30, month=9)
    view = reg.snapshot(at(30, month=9)).ordered
    assert len(view) == 1 and view[0].exception == "shipping delayed"


def test_delay_with_no_news_for_a_week_lets_go():
    reg = delayed_order()
    pkg = next(iter(reg.packages.values()))
    six_days = at(2)
    reg.last_ok = six_days
    assert not reg.probably_delivered(pkg, six_days.date())  # still waiting on the delay
    reg.expire(six_days, keep_delivered_days=2)
    assert reg.packages
    week = at(9)
    reg.last_ok = week
    assert reg.probably_delivered(pkg, week.date())
    assert reg.snapshot(week).ordered == ()
    reg.expire(week, keep_delivered_days=2)
    assert not reg.packages and f"o:{DELAYED_ORDER}" in reg.tombstones
    # An old email processed later can't bring it back.
    assert not reg.apply_event(
        event(status=S.ORDERED, email_date=week, carrier="amazon", order_id=DELAYED_ORDER)
    )


def test_delay_week_counts_only_mail_actually_read():
    reg = delayed_order()
    pkg = next(iter(reg.packages.values()))
    reg.last_ok = at(29, month=9)  # Gmail unreachable since then
    assert not reg.probably_delivered(pkg, dt.date(2026, 10, 9))


# ------------------------------------------------------------------ ticks ---


def test_tick_marks_received_and_untick_undoes():
    reg = registry.Registry()
    reg.apply_event(event(tracking=[UPS], carrier="ups", eta_start=NOW.date(), eta_end=NOW.date()))
    key = next(iter(reg.packages))
    assert reg.set_received(key, True, NOW)
    snap = reg.snapshot(NOW)
    assert snap.delivered_today == () and snap.on_the_way == () and snap.arriving_today == ()
    assert [v.ticked for v in snap.ticked_today] == [True]
    assert not reg.set_received(key, True, NOW)  # already ticked
    assert reg.set_received(key, False, NOW)
    pkg = reg.packages[key]
    assert pkg.status == S.IN_TRANSIT and pkg.delivered_at is None and pkg.manual is None
    assert len(reg.snapshot(NOW).arriving_today) == 1
    assert not reg.set_received("t:nope", True, NOW)


def test_ticked_package_retires_like_a_delivered_one():
    reg = delayed_order()
    key = next(iter(reg.packages))
    oct1 = at(1)
    reg.set_received(key, True, oct1)
    restored = registry.Registry.from_dict(reg.to_dict())  # the tick survives a restart
    pkg = restored.packages[key]
    assert pkg.manual == "received" and pkg.prev_status == S.ORDERED
    later = oct1 + dt.timedelta(days=2)
    restored.last_ok = later
    restored.expire(later, keep_delivered_days=2)
    assert not restored.packages and f"o:{DELAYED_ORDER}" in restored.tombstones


def test_unticking_a_probable_delivery_keeps_it_open():
    reg = registry.Registry()
    three_days_ago = NOW.date() - dt.timedelta(days=3)
    reg.apply_event(
        event(
            tracking=[UPS],
            carrier="ups",
            eta_start=three_days_ago,
            eta_end=three_days_ago,
            email_date=NOW - dt.timedelta(days=4),
        )
    )
    key = next(iter(reg.packages))
    reg.last_ok = NOW
    pkg = reg.packages[key]
    assert reg.probably_delivered(pkg, NOW.date())
    assert reg.set_received(key, False, NOW)  # "no, it hasn't come"
    assert not reg.probably_delivered(pkg, NOW.date())
    view = reg.snapshot(NOW).packages[0]
    assert view.status == "in transit" and view.late
    reg.expire(NOW, keep_delivered_days=2)
    assert reg.packages  # not retired while marked as not received
    assert reg.set_received(key, True, NOW) and pkg.status == S.DELIVERED  # and then it arrives


# ------------------------------------------------------------- store names ---


def test_store_order_is_named_after_the_store():
    msg = fake_mail(
        "Copper Kettle <store+10000001@t.shopifyemail.com>", "Order #2087 confirmed", "Thanks!"
    )
    reg = registry.Registry()
    reg.apply([("m", kb.parse_message(msg, TZ))], NOW)
    view = reg.snapshot(NOW).ordered[0]
    assert view.item == "order from Copper Kettle" and view.carrier == "Store"
