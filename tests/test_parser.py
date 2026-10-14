"""Reading shipping emails: sources, tracking numbers, dates and sender checks."""

import datetime as dt

import pytest

from custom_components.package_tracker import kb

from .common import (
    ALL_MAIL,
    BOX_HOSE,
    BOX_KITCHEN,
    BOX_LAMP,
    ETSY_ORDER,
    FEDEX_GROUND,
    FEDEX_OFD,
    NOT_SHIPPING,
    ORDER_COFFEE,
    ORDER_HOSE,
    ORDER_KITCHEN,
    ORDER_LAMP,
    ORDER_MOUSE,
    SHIPPING,
    TZ,
    UPS,
    UPS_DELIVERED,
    UPS_PARCELPANEL,
    UPS_STORE,
    USPS_ATTEMPTED,
    USPS_AWAITING,
    USPS_DELIVERED,
    USPS_EXPECTED,
    USPS_OFD,
    USPS_OUTBOUND,
    USPS_SHOPIFY,
    WALMART_ORDER,
    fake_mail,
    load,
    parse,
)

S = kb.Status
OCT = {d: dt.date(2026, 10, d) for d in range(1, 32)}
VERIFIED_AMAZON = "dkim=pass header.i=@amazon.com; dmarc=pass header.from=amazon.com"

# --------------------------------------------------------------- tracking ---


@pytest.mark.parametrize(
    ("number", "check"),
    [
        (USPS_EXPECTED, kb.usps_ok),
        (USPS_SHOPIFY, kb.usps_ok),
        (UPS, kb.ups_1z_ok),
        ("1Z999AA10123456784", kb.ups_1z_ok),
        (FEDEX_OFD, kb.fedex12_ok),
        (FEDEX_GROUND, kb.fedex15_ok),
    ],
)
def test_check_digits(number, check):
    assert check(number)
    typo = number[:-1] + str((int(number[-1]) + 1) % 10)
    assert not check(typo)


def test_impb_with_routing_prefix_is_one_number():
    found = kb.find_tracking(
        f"USPS tracking number: 42062701{USPS_EXPECTED}", sender_carrier="usps"
    )
    assert [n for _, n in found] == [USPS_EXPECTED]


def test_all_zero_numbers_rejected():
    assert kb.find_tracking("Tracking number: 000000000000", sender_carrier="fedex") == []


def test_spaced_labelled_number_found():
    spaced = " ".join(USPS_EXPECTED[i : i + 4] for i in range(0, 22, 4))
    found = kb.find_tracking(f"USPS Tracking Number: {spaced}", sender_carrier="usps")
    assert ("usps", USPS_EXPECTED) in found


def test_amazon_order_number_is_not_a_tracking_number():
    assert kb.find_tracking(f"Order # {ORDER_KITCHEN}", sender_carrier=None) == []


def test_words_are_not_tracking_numbers():
    for text in (
        "Tracking number available soon",
        "Turn on tracking notifications",
        "Tracking ID: Unavailable",
    ):
        assert not list(kb.TRACKING_LABEL_RE.finditer(text.upper())), text


# --------------------------------------------------------- every source ---

EXPECTED = {
    "amazon_ordered.eml": dict(
        source="amazon_ordered",
        status=S.ORDERED,
        order_id=ORDER_KITCHEN,
        items=3,
        description="Kitchen",
        eta=(OCT[9], OCT[10]),
    ),
    "amazon_shipped.eml": dict(
        source="amazon_shipped",
        status=S.IN_TRANSIT,
        order_id=ORDER_KITCHEN,
        shipment_ids=[BOX_KITCHEN],
        items=1,
        eta=(OCT[9], OCT[9]),
    ),
    "amazon_shipped_legacy.eml": dict(
        source="amazon_shipped",
        status=S.IN_TRANSIT,
        order_id=ORDER_HOSE,
        shipment_ids=[BOX_HOSE],
        description="Garden Hose",
        eta=(OCT[10], OCT[10]),
    ),
    "amazon_out_for_delivery.eml": dict(
        source="amazon_ofd",
        status=S.OUT_FOR_DELIVERY,
        order_id=ORDER_COFFEE,
        description="Coffee Beans",
        eta=(OCT[9], OCT[9]),
    ),
    "amazon_delivered.eml": dict(
        source="amazon_delivered",
        status=S.DELIVERED,
        order_id=ORDER_LAMP,
        shipment_ids=[BOX_LAMP],
        description="Desk Lamp",
        eta=(None, None),
    ),
    "amazon_delivery_update.eml": dict(
        source="amazon_exception",
        status=None,
        exception="running late",
        shipment_ids=[BOX_KITCHEN],
        eta=(OCT[10], OCT[11]),
    ),
    "amazon_shipping_delay.eml": dict(
        source="amazon_ship_delay",
        status=None,
        exception="shipping delayed",
        order_id=ORDER_MOUSE,
        description="Wireless Mouse",
    ),
    "usps_expected.eml": dict(
        source="usps",
        status=S.IN_TRANSIT,
        tracking=[USPS_EXPECTED],
        eta=(OCT[9], OCT[9]),
        eta_time="by 9:00pm",
        shipped_from="NORTHWIND BOOKS",
    ),
    "usps_out_for_delivery.eml": dict(
        source="usps",
        status=S.OUT_FOR_DELIVERY,
        tracking=[USPS_OFD],
        eta=(OCT[6], OCT[6]),
        eta_time="between 10:30am and 12:30pm",
    ),
    "usps_delivered.eml": dict(source="usps", status=S.DELIVERED, tracking=[USPS_DELIVERED]),
    "usps_exception.eml": dict(
        source="usps",
        status=None,
        tracking=[USPS_ATTEMPTED],
        exception="attempt",
    ),
    "ups_shipped.eml": dict(
        source="ups_ship",
        status=S.IN_TRANSIT,
        tracking=[UPS],
        eta=(OCT[9], OCT[9]),
    ),
    "ups_out_for_delivery.eml": dict(
        source="ups_ofd",
        status=S.OUT_FOR_DELIVERY,
        tracking=[UPS],
        eta=(OCT[9], OCT[9]),
    ),
    "ups_delivered.eml": dict(source="ups_delivered", status=S.DELIVERED, tracking=[UPS_DELIVERED]),
    "fedex_out_for_delivery.eml": dict(
        source="fedex_ofd",
        status=S.OUT_FOR_DELIVERY,
        tracking=[FEDEX_OFD],
        eta=(OCT[9], OCT[9]),
        eta_time="between 2:00pm and 6:00pm",
    ),
    "fedex_on_the_way.eml": dict(
        source="fedex_transit",
        status=S.IN_TRANSIT,
        tracking=[FEDEX_GROUND],
        eta=(OCT[12], OCT[12]),
    ),
    "walmart_shipped.eml": dict(
        source="walmart_shipped",
        status=S.IN_TRANSIT,
        order_id=WALMART_ORDER,
        eta=(OCT[8], OCT[8]),
        shipped_from="Walmart.com",
    ),
    "walmart_delivered.eml": dict(
        source="walmart_delivered",
        status=S.DELIVERED,
        order_id=WALMART_ORDER,
        description="Coffee Maker and 1 other item",
    ),
    "etsy_shipped.eml": dict(source="etsy_shipped", status=S.IN_TRANSIT, order_id=ETSY_ORDER),
    "etsy_delivered.eml": dict(source="etsy_delivered", status=S.DELIVERED, order_id=ETSY_ORDER),
    "shopify_shipped.eml": dict(
        source="shopify_shipped",
        status=S.IN_TRANSIT,
        tracking=[USPS_SHOPIFY],
        carrier="usps",
        shipped_from="Juniper Home Goods",
    ),
    "parcelpanel_delivered.eml": dict(
        source="shopify_delivered",
        status=S.DELIVERED,
        tracking=[UPS_PARCELPANEL],
        carrier="ups",
        shipped_from="Lakeside Tea Company",
    ),
    "store_shipped.eml": dict(
        source="generic",
        status=S.IN_TRANSIT,
        tracking=[UPS_STORE],
        carrier="ups",
        eta=(OCT[13], OCT[13]),
        shipped_from="Acme Outdoor",
    ),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sources(name):
    ev = parse(name)
    assert isinstance(ev, kb.Event)
    for field, want in EXPECTED[name].items():
        got = (ev.eta_start, ev.eta_end) if field == "eta" else getattr(ev, field)
        assert got == want, field


@pytest.mark.parametrize("name", sorted(NOT_SHIPPING))
def test_mail_that_is_not_a_shipment(name):
    assert parse(name) is None


def test_every_fixture_is_covered():
    assert set(EXPECTED) | {"usps_informed_delivery.eml"} == set(SHIPPING)
    assert set(SHIPPING) | NOT_SHIPPING == set(ALL_MAIL)


def test_store_marketing_is_skipped_before_download():
    msg = load("store_newsletter.eml")
    assert not kb.is_candidate(str(msg["From"]), str(msg["Subject"]))
    amazon = load("amazon_marketing.eml")  # downloaded (Amazon sender) but not a shipment
    assert kb.is_candidate(str(amazon["From"]), str(amazon["Subject"]))


def test_informed_delivery_digest():
    dg = parse("usps_informed_delivery.eml")
    assert isinstance(dg, kb.Digest)
    assert dg.digest_date == OCT[9] and dg.mailpieces == 3
    assert dg.packages == {
        USPS_EXPECTED: "today",
        USPS_DELIVERED: "today",
        USPS_ATTEMPTED: "soon",
        USPS_AWAITING: "awaiting",
        USPS_OUTBOUND: "outbound",
    }


def test_etsy_shipped_and_delivered_share_an_order():
    assert parse("etsy_shipped.eml").order_id == parse("etsy_delivered.eml").order_id == ETSY_ORDER


def test_shopify_order_ids_name_the_store():
    assert parse("shopify_shipped.eml").order_id == (
        "shopify-store+48291037@t.shopifyemail.com-1042"
    )
    # ParcelPanel mails for many stores from one address, so the store's name is part of the id.
    assert parse("parcelpanel_delivered.eml").order_id == (
        "shopify-no-reply@parcelpanel.net/Lakeside Tea Company-2087"
    )


# --------------------------------------------------------- sender checks ---


def test_spoofed_carrier_mail_ignored():
    msg = load("phishing_spoofed_fedex.eml")
    assert kb.classify(msg) is not None  # looks like a FedEx delivery...
    assert kb.sender_verified(msg, kb.from_address(msg)) is False  # ...but failed DMARC


def test_lookalike_brand_store_ignored():
    msg = load("phishing_lookalike_usps.eml")
    assert kb.sender_verified(msg, kb.from_address(msg)) is True  # its own domain checks out
    assert kb.parse_message(msg, TZ) is None


def test_unknown_store_needs_verified_sender_and_label():
    body = f"Your order has shipped. UPS tracking number: {UPS}"
    unverified = fake_mail("Shop <orders@shop.example>", "Your order has shipped", body)
    assert kb.parse_message(unverified, TZ) is None
    verified = fake_mail(
        "Shop <orders@shop.example>",
        "Your order has shipped",
        body,
        "dkim=pass header.i=@shop.example; dmarc=pass header.from=shop.example",
    )
    ev = kb.parse_message(verified, TZ)
    assert ev is not None and ev.tracking == [UPS] and ev.carrier == "ups"
    bare = fake_mail(
        "Shop <orders@shop.example>",
        "Your order has shipped",
        f"Ref {UPS}",
        "dmarc=pass header.from=shop.example",
    )
    assert kb.parse_message(bare, TZ) is None  # an unlabelled number from an unknown store


def test_from_header_tricks_rejected():
    subject = f"USPS\u00ae Item Delivered, In/At Mailbox {USPS_DELIVERED}"
    tricky = fake_mail(
        "auto-reply@usps.com <attacker@evil.example>",
        subject,
        "x",
        "dkim=pass header.i=@evil.example; dmarc=pass (p=NONE) header.from=evil.example",
    )
    assert kb.parse_message(tricky, TZ) is None
    other_domain = fake_mail(
        "USPS <auto-reply@usps.com>", subject, "x", "dmarc=pass header.from=evil.example"
    )
    assert kb.sender_verified(other_domain, "auto-reply@usps.com") is False


def test_bad_date_header_falls_back():
    msg = load("usps_expected.eml")
    msg.replace_header("Date", "not a date")
    fallback = dt.datetime(2026, 10, 9, 12, 0, tzinfo=dt.UTC)
    assert kb.parse_message(msg, TZ, fallback).email_date == fallback.astimezone(TZ)


# ------------------------------------------------------ subject variants ---


def test_fedex_new_layout_on_the_way():
    msg = fake_mail(
        "FedEx <TrackingUpdates@fedex.com>",
        f"Your shipment is on the way {FEDEX_OFD}",
        "x",
        "dkim=pass header.i=@fedex.com",
    )
    ev = kb.parse_message(msg, TZ)
    assert ev.status == S.IN_TRANSIT and ev.tracking == [FEDEX_OFD]


def test_amazon_shipping_delay_subject():
    msg = fake_mail(
        "Amazon.com <no-reply@amazon.com>",
        f"Delay in shipping your order \u200e#{ORDER_MOUSE}\u200e",
        "Sorry for the delay.",
        VERIFIED_AMAZON,
    )
    ev = kb.parse_message(msg, TZ)
    assert ev.order_id == ORDER_MOUSE and ev.exception == "shipping delayed" and ev.status is None


def test_shopify_orders_from_different_stores_differ():
    a = fake_mail(
        "Teas <store+111@t.shopifyemail.com>", "A shipment from order #1042 is on the way", "x"
    )
    b = fake_mail(
        "Socks <store+222@t.shopifyemail.com>", "A shipment from order #1042 is on the way", "x"
    )
    ea, eb = kb.parse_message(a, TZ), kb.parse_message(b, TZ)
    assert ea is not None and eb is not None and ea.order_id != eb.order_id


def test_shopify_order_confirmed():
    msg = fake_mail("Shop <store+10000001@t.shopifyemail.com>", "Order #2087 confirmed", "Thanks!")
    ev = kb.parse_message(msg, TZ)
    assert ev.status == S.ORDERED and ev.order_id.endswith("-2087")
