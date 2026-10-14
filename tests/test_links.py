"""Where clicking a package goes, and the newest-email id behind the "open the email" link."""

import datetime as dt

import pytest

from custom_components.package_tracker import kb, links, registry

from .common import BOX_HOSE, NOW, ORDER_HOSE, UPS, USPS_EXPECTED, event

S = kb.Status
ACCOUNT = "jane.doe+packages@example.com"
MSGID = "1812345678901234567"  # Gmail's X-GM-MSGID; the web app shows it in hex


@pytest.mark.parametrize(
    ("carrier", "number", "expect"),
    [
        (
            "usps",
            USPS_EXPECTED,
            f"https://tools.usps.com/go/TrackConfirmAction?tLabels={USPS_EXPECTED}",
        ),
        ("ups", UPS, f"https://www.ups.com/track?loc=en_US&tracknum={UPS}"),
        ("fedex", "123456789012", "https://www.fedex.com/fedextrack/?trknbr=123456789012"),
        (
            "dhl",
            "1234567890",
            "https://www.dhl.com/us-en/home/tracking.html?tracking-id=1234567890&submit=1",
        ),
        ("ontrac", "D10011111111111", "https://www.ontrac.com/tracking/?number=D10011111111111"),
        ("gofo", "GFUS01234567890123", "https://www.gofo.com/us/track?searchID=GFUS01234567890123"),
    ],
)
def test_carrier_pages(carrier, number, expect):
    assert links.package_url(carrier, [number], None, [], MSGID, ACCOUNT) == expect


def test_amazon_box_then_order_then_email():
    assert links.package_url("amazon", [], ORDER_HOSE, [BOX_HOSE], None, ACCOUNT) == (
        f"https://www.amazon.com/progress-tracker/package?orderId={ORDER_HOSE}"
        f"&shipmentId={BOX_HOSE}&packageIndex=0"
    )
    assert links.package_url("amazon", [], ORDER_HOSE, [], None, ACCOUNT) == (
        f"https://www.amazon.com/your-orders/order-details?orderID={ORDER_HOSE}"
    )
    # A store order with no tracking number opens the email itself.
    assert links.package_url(None, [], "shopify-x-1042", [], MSGID, ACCOUNT) == (
        "https://mail.google.com/mail/u/?authuser=jane.doe%2Bpackages%40example.com#all/1926becaf11f4b87"
    )
    assert links.package_url(None, [], None, [], None, ACCOUNT) is None


@pytest.mark.parametrize("bad", ["<script>", '1Z99"><img src=x>', "a" * 60, "", "12 34"])
def test_odd_numbers_never_reach_a_link(bad):
    assert links.package_url("ups", [bad], None, [], None, None) is None
    order_page = links.package_url("amazon", [], ORDER_HOSE, [bad], None, None)
    assert order_page.endswith(f"orderID={ORDER_HOSE}")
    assert links.email_url(bad, ACCOUNT) is None


def test_package_remembers_its_newest_email():
    reg = registry.Registry()
    yesterday = NOW - dt.timedelta(days=1)
    reg.apply(
        [
            ("200", event(tracking=[UPS], carrier="ups", email_date=NOW)),
            ("100", event(tracking=[UPS], carrier="ups", email_date=yesterday)),
        ],
        NOW,
    )
    pkg = next(iter(reg.packages.values()))
    assert pkg.msgid == "200"  # the newer email, though it was listed first
    older = event(tracking=[UPS], carrier="ups", email_date=yesterday - dt.timedelta(days=1))
    reg.apply([("50", older)], NOW)
    assert pkg.msgid == "200"  # an older email processed later doesn't take over
    assert registry.Registry.from_dict(reg.to_dict()).packages[pkg.key].msgid == "200"


def test_backfill_fills_in_the_newest_email_id_only():
    reg = registry.Registry()
    reg.apply_event(event(tracking=[UPS], carrier="ups"))
    pkg = next(iter(reg.packages.values()))
    assert pkg.msgid is None
    updated = reg.backfill_addresses(
        [
            ("300", event(tracking=[UPS], carrier="ups")),
            ("299", event(tracking=[UPS], carrier="ups", email_date=NOW - dt.timedelta(hours=5))),
        ]
    )
    assert updated == 1 and pkg.msgid == "300" and pkg.status == S.IN_TRANSIT


def test_backfill_uses_informed_delivery_for_the_email_id():
    reg = registry.Registry()
    reg.apply_event(event(tracking=[USPS_EXPECTED], carrier="usps", status=S.LABEL_CREATED))
    pkg = next(iter(reg.packages.values()))
    digest = kb.Digest(
        digest_date=NOW.date(),
        email_date=NOW - dt.timedelta(hours=1),
        mailpieces=2,
        packages={USPS_EXPECTED: "today", "9400100000000000000009": "outbound"},
    )
    assert reg.backfill_addresses([("777", digest)]) == 1
    assert pkg.msgid == "777" and pkg.status == S.LABEL_CREATED and len(reg.packages) == 1
