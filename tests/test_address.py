"""Delivery addresses: reading them from shipping emails, and the "addresses to show" filter."""

from email.message import EmailMessage

import pytest

from custom_components.package_tracker import address, kb, registry

from .common import ALL_MAIL, HOME, NOW, SHIPPING, TZ, UPS, event, load

S = kb.Status
P = address.parse
JANE_HOME = f"Jane Doe, {HOME}"


def places(name: str) -> list[dict]:
    ev = kb.parse_message(load(name), TZ)
    return [p.to_dict() for p in ev.ship_to] if isinstance(ev, kb.Event) else []


# ------------------------------------------------------------ user entries ---


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        (HOME, dict(house="123", street="MAIN", city="SPRINGFIELD", state="IL", zip="62701")),
        (
            "Jane Doe, 123 Main Street Apt 4, Springfield, Illinois 62701-1234",
            dict(
                name="JANE DOE",
                house="123",
                street="MAIN",
                city="SPRINGFIELD",
                state="IL",
                zip="62701",
            ),
        ),
        (
            "123 Main St Springfield IL 62701",
            dict(house="123", street="MAIN", city="SPRINGFIELD", state="IL", zip="62701"),
        ),
        (
            "12 Elm Ave West Springfield MA 01089",
            dict(house="12", street="ELM", city="WEST SPRINGFIELD", state="MA", zip="01089"),
        ),
        (
            "1 N Main Street W, Springfield, IL",
            dict(house="1", street="MAIN", city="SPRINGFIELD", state="IL"),
        ),
        ("Jane - Springfield, IL", dict(name="JANE", city="SPRINGFIELD", state="IL")),
        ("Springfield, IL", dict(city="SPRINGFIELD", state="IL")),
        ("Brooklyn, New York", dict(city="BROOKLYN", state="NY")),
        ("62701", dict(zip="62701")),
        ("12 Oak Ct", dict(house="12", street="OAK")),  # Ct is Court here, not a state
        (
            "PO Box 12, Springfield, IL",
            dict(house="BOX 12", street="PO BOX", city="SPRINGFIELD", state="IL"),
        ),
        ("John \u2013 Springfield, Ontario", dict(name="JOHN", city="SPRINGFIELD", state="ON")),
        ("Springfield, ON K1A 0B1", dict(city="SPRINGFIELD", state="ON", zip="K1A0B1")),
        (
            "123 Pennsylvania Ave NW, Washington, DC 20003",
            dict(house="123", street="PENNSYLVANIA", city="WASHINGTON", state="DC", zip="20003"),
        ),
    ],
)
def test_parse_entries(text, expect):
    assert P(text).to_dict() == expect


@pytest.mark.parametrize("text", ["Jane", "Springfield", "West Springfield"])
def test_bare_words_are_keywords(text):
    p = P(text)
    assert p.keyword and p.loose == address.words(text)


def test_oak_hill_rd_keeps_its_name():
    assert P("45 Oak Hill Rd").to_dict() == dict(house="45", street="OAK HILL")


def test_highway_town_split_is_left_loose():
    assert P("31 Highway 101 Crescent City", from_email=True).to_dict() == dict(
        loose="31 HIGHWAY 101 CRESCENT CITY"
    )


def test_clean_entries():
    assert address.clean_entries(
        ["", "  ", "Jane Doe,  Springfield, IL", "jane doe, springfield, il"]
    ) == (
        ["Jane Doe, Springfield, IL"],
        None,
        "",
    )
    assert address.clean_entries(None) == ([], None, "")
    assert address.clean_entries(["NY"])[1] == "address_too_short"
    assert address.clean_entries(["x" * 200])[1] == "address_too_long"
    assert (
        address.clean_entries([f"{i} Main St, Springfield, IL" for i in range(11)])[1]
        == "too_many_addresses"
    )


@pytest.mark.parametrize(
    "text",
    [
        "123 Spring Hill Rd Springfield IL 62701",
        "123 Main Springfield IL 62701",
        "123 Broadway New York NY 10001",
        "1 Main St Grove City PA 16127",
    ],
)
def test_entries_that_need_commas(text):
    assert address.clean_entries([text])[1] == "address_needs_commas"


@pytest.mark.parametrize(
    "text",
    [
        "123 Main St Springfield IL 62701",
        "12 Oak Ct",
        "123 Broadway, New York, NY",
        "Jane - Springfield, IL",
        "62701",
        "West Springfield",
        "45 Oak Hill Rd",
        "123 Lake View Dr",
        "12 N Broadway",
        "88 Route 9",
        "31 Highway 101",
        "1211 Avenue of the Americas",
        "45 Oak Hill Rd, Apt 2",
        "Jane Doe, 45 Oak Hill Rd",
        "123 Main St, Springfield, IL 62701, U.S.A.",
    ],
)
def test_entries_that_are_fine(text):
    assert address.clean_entries([text]) == ([text], None, "")


# ---------------------------------------------------------------- matching ---


@pytest.mark.parametrize(
    ("entry", "pkg", "expect"),
    [
        (HOME, "Jane - SPRINGFIELD, IL", True),  # Amazon: town level
        (HOME, "S - SHELBYVILLE, IL", False),  # Amazon, another household
        (HOME, "Jane Doe, 123 MAIN STREET, SPRINGFIELD, IL, US, 62701", True),  # FedEx, full
        (HOME, "Jane Doe, 99 OAK RD, SPRINGFIELD, IL, US, 62701", False),  # same town, other street
        (HOME, "SPRINGFIELD, IL 62704", False),  # USPS: another ZIP
        (HOME, "SPRINGFIELD, IL 62701", True),
        (HOME, "SPRINGFIELD, MA 01101", False),  # same town name, another state
        ("62701", "Jane - SPRINGFIELD, IL", None),  # ZIP only vs Amazon: can't tell
        ("Jane - Springfield, IL", "R - SPRINGFIELD, IL", False),  # same town, another person
        ("Jane - Springfield, IL", "J - SPRINGFIELD, IL", True),  # Amazon shows an initial
        ("Jane - Springfield, IL", "Jim Doe, 123 MAIN ST, SPRINGFIELD, IL, 62701", False),
        (HOME, "Jim Doe, 123 MAIN ST, SPRINGFIELD, IL, 62701", True),  # anyone at a full address
        (JANE_HOME, "Jane - SPRINGFIELD, IL", True),  # name + full address: Amazon by name
        (JANE_HOME, "R - SPRINGFIELD, IL", False),
        (JANE_HOME, "Jim Doe, 123 MAIN ST, SPRINGFIELD, IL, 62701", True),  # carriers by address
        ("Springfield", "Jane - SPRINGFIELD, IL", True),  # keyword
        ("Springfield", "Jane - WEST SPRINGFIELD, MA", True),  # whole words
        ("Spring", "Jane - SPRINGFIELD, IL", False),  # not part of a word
        ("Jane", "SPRINGFIELD, IL 62701", None),  # a name, and this email gives none
        ("Saint Louis, MO", "Jane - ST LOUIS, MO", True),
        ("West Springfield, MA", "Jane - W SPRINGFIELD, MA", True),
        ("123 Main St, Springfield, IL 62701 USA", "Jane - SPRINGFIELD, IL", True),
        ("123 Main St, Springfield, IL 62701 USA", "SPRINGFIELD, IL 62701", True),
        ("123 Main St, Apt 4", "Jane Doe, 123 MAIN ST APT 4, SPRINGFIELD, IL, US, 62701", True),
        ("123 Main St, #4", "Jane Doe, 123 MAIN ST, SPRINGFIELD, IL, US, 62701", True),
        ("123 West St, Springfield, IL", "Bob, 123 WEST STREET, SPRINGFIELD, IL, US, 62701", True),
        ("123 West St, Springfield, IL", "Bob, 123 EAST ST, SPRINGFIELD, IL, US, 62701", False),
        (
            "1234 N St NW, Washington, DC 20001",
            "Bob, 1234 S ST NW, WASHINGTON, DC, US, 20001",
            False,
        ),
        ("123 Avenue A, New York, NY 10009", "Bob, 123 AVE A, NEW YORK, NY, US, 10009", True),
        ("Port St. Lucie, FL 34952", "PORT SAINT LUCIE, FL 34952", True),
        (
            "123 Saint James St, Springfield, IL",
            "Bob, 123 ST JAMES ST, SPRINGFIELD, IL, US, 62701",
            True,
        ),
        ("Springfield Illinois", "Jane - SPRINGFIELD, IL", True),
        ("Elm Street", "Bob, 123 ELM STREET, SPRINGFIELD, IL, US, 62701", True),
        ("Jane Doe", "Jane - SPRINGFIELD, IL", True),  # Amazon shows only the first name
        ("Mount Vernon", "M - SHELBYVILLE, IL", False),
        (
            "123 Main St 2nd Fl, Springfield, IL",
            "Bob, 123 MAIN ST, SPRINGFIELD, IL, US, 62701",
            True,
        ),
        ("123 Main St, Springfield, IL 62701, U.S.A.", "Jane - SPRINGFIELD, IL", True),
        (
            "123 Main St, Springfield, IL 62701, U.S.",
            "Jane Doe, 123 MAIN ST, SPRINGFIELD, IL, US, 62701",
            True,
        ),
        (
            "12 Broadway #4, Springfield, IL 62701",
            "Jane Doe, 12 BROADWAY, SPRINGFIELD, IL, US, 62701",
            True,
        ),
        (
            "2 Avenue A #4, New York, NY 10009",
            "Jane Doe, 2 AVENUE A APT 4, NEW YORK, NY, US, 10009",
            True,
        ),
        ("123 Main St, Apt 4-B", "Jane Doe, 123 MAIN ST APT 4B, SPRINGFIELD, IL, US, 62701", True),
        (
            "12 Broadway Apt PH, Springfield, IL 62701",
            "Jane Doe, 12 BROADWAY, SPRINGFIELD, IL, US, 62701",
            True,
        ),
        (HOME, "123 Main St. Apt PH Springfield", True),  # Etsy writes one line
        (HOME, "123 Main St. Apt 4-B Springfield", True),
        ("9 First Ave, Grove City, PA 16127", "9 First Ave Grove City", True),
        ("9 N Main St, Grove City, PA 16127", "9 N Main St Grove City", True),
        ("12 N Broadway, Springfield, IL 62701", "12 N Broadway Springfield", True),
        ("45 N Oak Hill Rd, Springfield, IL 62701", "45 N Oak Hill Rd Springfield", True),
        ("Crescent City, CA", "31 Highway 101 Crescent City", True),
        ("123 Main St Springfield IL", "Jane - SPRINGFIELD, IL", True),
        ("123 Main St Springfield Illinois", "SPRINGFIELD, IL 62701", True),
        ("PO Box 123 Springfield, IL 62701", "Jane - SPRINGFIELD, IL", True),
        ("PO Box 123, Springfield, IL 62701", "Jane - SHELBYVILLE, IL", False),
        ("Washington D.C.", "Jane - WASHINGTON, DC", True),
        ("Court Street", "R - HARTFORD, CT", False),
        ("Lake Street", "R - LAKE FOREST, IL", False),
    ],
)
def test_compare(entry, pkg, expect):
    assert address.compare(P(entry), P(pkg, from_email=True)) is expect


def test_loose_email_address_matches_street_or_town():
    pkg = P("123 Main St. Springfield", from_email=True)
    assert address.compare(P(HOME), pkg) is True
    assert address.compare(P("9 Oak Rd, Shelbyville, IL"), pkg) is False


def test_filter_verdict_any_entry_any_place():
    flt = address.AddressFilter.build(["Shelbyville, IL", HOME])
    assert flt.verdict([P("S - SHELBYVILLE, IL", from_email=True)]) is True
    assert flt.verdict([P("Q - OGDENVILLE, IL", from_email=True)]) is False
    assert flt.verdict([]) is None
    # One order shipped to two places: shown if either one matches.
    assert flt.verdict(
        [P("Q - OGDENVILLE, IL", from_email=True), P("J - SPRINGFIELD, IL", from_email=True)]
    )


# ----------------------------------------------------- reading the emails ---

SPRINGFIELD = dict(city="SPRINGFIELD", state="IL", zip="62701")
MAIN_ST = dict(house="123", street="MAIN", **SPRINGFIELD)


@pytest.mark.parametrize(
    ("name", "expect"),
    [
        ("amazon_ordered.eml", [dict(name="MAX", city="SHELBYVILLE", state="IL")]),
        ("amazon_shipped.eml", [dict(name="MAX", city="SHELBYVILLE", state="IL")]),
        ("amazon_out_for_delivery.eml", [dict(name="JANE", city="SPRINGFIELD", state="IL")]),
        ("amazon_shipped_legacy.eml", [dict(name="JANE DOE", city="SPRINGFIELD", state="IL")]),
        ("fedex_out_for_delivery.eml", [dict(name="JANE DOE", **MAIN_ST)]),  # "To", not "From"
        ("fedex_on_the_way.eml", [SPRINGFIELD]),  # masked "TO", so the DESTINATION line
        ("ups_shipped.eml", [MAIN_ST]),  # plain text "Delivery Location:"
        ("ups_out_for_delivery.eml", [MAIN_ST]),  # HTML "Ship To"
        ("usps_out_for_delivery.eml", [SPRINGFIELD]),
        ("usps_delivered.eml", [SPRINGFIELD]),
        ("usps_exception.eml", [SPRINGFIELD]),
        (
            "etsy_shipped.eml",
            [dict(name="JANE DOE", house="123", street="MAIN", city="SPRINGFIELD")],
        ),
        ("etsy_delivered.eml", [dict(house="123", street="MAIN", city="SPRINGFIELD")]),
        ("shopify_shipped.eml", [dict(name="JANE DOE", **MAIN_ST)]),  # not the billing address
        (
            "parcelpanel_delivered.eml",
            [dict(name="JANE DOE", house="123", street="MAIN", city="SPRINGFIELD")],
        ),
        # These don't say where the package is going.
        ("usps_expected.eml", []),
        ("ups_delivered.eml", []),
        ("walmart_delivered.eml", []),
        ("store_shipped.eml", []),
        ("usps_informed_delivery.eml", []),
    ],
)
def test_ship_to_in_emails(name, expect):
    assert places(name) == expect


def test_footer_and_sender_addresses_never_taken():
    for name in ALL_MAIL:
        for p in places(name):
            # Amazon and USPS head offices, Etsy's office, FedEx shippers.
            assert p.get("city") not in (
                "SEATTLE",
                "WASHINGTON",
                "BROOKLYN",
                "PEORIA",
                "DECATUR",
            ), name


def test_households_are_told_apart():
    theirs = ["amazon_ordered.eml", "amazon_shipped.eml", "amazon_delivery_update.eml"]
    jane = ["amazon_out_for_delivery.eml", "amazon_delivered.eml", "amazon_shipping_delay.eml"]
    assert {places(n)[0]["city"] for n in theirs} == {"SHELBYVILLE"}
    assert {places(n)[0]["city"] for n in jane} == {"SPRINGFIELD"}
    # Carrier emails for the same household name the same town, with the full address.
    fedex, usps = places("fedex_out_for_delivery.eml")[0], places("usps_out_for_delivery.eml")[0]
    assert fedex["city"] == usps["city"] == "SPRINGFIELD" and fedex["zip"] == usps["zip"]
    assert all(k in fedex for k in ("name", "house", "street", "state"))


def test_usps_facility_city_is_not_the_destination():
    text = (
        "Your item arrived at our USPS facility in PEORIA, IL 61601 on October 8, 2026 at 3:12 am. "
        "The item is currently in transit to the destination."
    )
    assert address._usps(text) == []
    delivered = address._usps(
        "Your item was delivered in or at the mailbox at 1:07 pm on July 27, 2026 in "
        "SPRINGFIELD, IL 62701."
    )
    assert delivered[0].to_dict() == SPRINGFIELD


@pytest.mark.parametrize(
    "sentence",
    [
        "We attempted to deliver your item at 12:22 pm on October 8, 2026 in "
        "SPRINGFIELD, IL 62701 and a notice was left.",
        "Your item arrived at the Post Office at 9:53 am on October 8, 2026 in "
        "SPRINGFIELD, IL 62701 and is ready for pickup.",
        "Your item has been delivered to the front desk or mail room in SPRINGFIELD, IL 62701 on "
        "October 8, 2026 at 9:48 am.",
        "Your item was picked up at a postal facility at 7:22 am on October 8, 2026 in "
        "SPRINGFIELD, IL 62701.",
    ],
)
def test_usps_destination_sentences(sentence):
    assert address._usps(sentence)[0].to_dict() == SPRINGFIELD


def test_usps_shipping_partner_pickup_is_the_origin():
    text = (
        "Your item was picked up by a shipping partner at 3:33 pm on October 5, 2026 in "
        "DALLAS, TX 75261. USPS is awaiting item."
    )
    assert address._usps(text) == []


# ------------------------------------------------- layouts, built inline ---

ORDER = "111-5550123-4567890"


def mail(plain: str | None = None, html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "x@example.com", "x"
    if plain is not None:
        msg.set_content(plain)
        if html is not None:
            msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(html or "", subtype="html")
    return msg


def ship_to(row: str, carrier: str | None, msg: EmailMessage) -> list[dict]:
    return [p.to_dict() for p in address.ship_to(row, carrier, msg, kb.body_text(msg))]


@pytest.mark.parametrize(
    ("line", "expect"),
    [
        (
            "Amazon Hub Locker - Maple - SPRINGFIELD, IL",
            dict(name="AMAZON HUB LOCKER MAPLE", city="SPRINGFIELD", state="IL"),
        ),
        ("Doe, Jane - SPRINGFIELD, IL", dict(name="DOE JANE", city="SPRINGFIELD", state="IL")),
        (
            "John Smith, Jr. - SPRINGFIELD, Illinois",
            dict(name="JOHN SMITH JR", city="SPRINGFIELD", state="IL"),
        ),
        ("Jane - WILKES-BARRE, PA", dict(name="JANE", city="WILKES BARRE", state="PA")),
    ],
)
def test_amazon_names_with_dashes_and_commas(line, expect):
    body = f"Arriving tomorrow\n\n{line}\n\nOrder #\n{ORDER}\n"
    assert ship_to("amazon_shipped", "amazon", mail(body)) == [expect]


@pytest.mark.parametrize(
    "title",
    [
        "Gildan Tee - Black, XL",
        "Hat - Texas Rangers, Texas...",
        "Skyline Map Poster - Denver, Colorado",
        "Vintage Pennant - Dallas, TX",
    ],
)
def test_amazon_product_title_is_not_an_address(title):
    html = f"<p>Delivery update</p><p>{title}</p><p>Order #{ORDER}</p>"
    assert ship_to("amazon_exception", "amazon", mail(html=html)) == []


def test_amazon_html_only_ship_to_after_item_count():
    html = (
        "<p>Arriving tomorrow</p><p>1 item: Coffee</p><p>Jane - SPRINGFIELD, IL</p>"
        f"<p>Order #{ORDER}</p>"
    )
    assert ship_to("amazon_shipped", "amazon", mail(html=html)) == [
        dict(name="JANE", city="SPRINGFIELD", state="IL")
    ]


def test_fedex_canada():
    html = "<p>To</p><p>Jane Doe<br>45 MAIN ST<br>TORONTO, ON, CA, M5V2T6</p><p>REFERENCE</p>"
    assert ship_to("fedex_ofd", "fedex", mail(html=html)) == [
        dict(name="JANE DOE", house="45", street="MAIN", city="TORONTO", state="ON", zip="M5V2T6")
    ]
    entry = P("45 Main St, Toronto, ON M5V 2T6")
    assert address.compare(
        entry, P("JANE DOE, 45 MAIN ST, TORONTO, ON, CA, M5V2T6", from_email=True)
    )


@pytest.mark.parametrize(
    "plain",
    [
        "Ship To: JANE DOE, 45 MAIN ST, SPRINGFIELD, IL 62701 US\nUPS Ground",
        "Ship To\n45 MAIN ST\nSPRINGFIELD, IL, US, 62701\nUPS Ground",
    ],
)
def test_ups_other_layouts(plain):
    got = ship_to("ups_ofd", "ups", mail(plain))
    assert (
        got
        and got[0]["city"] == "SPRINGFIELD"
        and got[0]["zip"] == "62701"
        and got[0]["house"] == "45"
    )


def test_etsy_reads_only_the_address_line():
    html = (
        "<p>Delivered to</p><p>45 Main St. Springfield</p>"
        "<p>Was everything okay with your order?</p>"
        "<p>Etsy, Inc.</p><p>117 Adams Street, Brooklyn, NY 11201</p>"
    )
    assert ship_to("etsy_delivered", None, mail(html=html)) == [
        dict(house="45", street="MAIN", city="SPRINGFIELD")
    ]
    plain = "Shipping to\nJane Doe\n45 Oak Hill Rd. Springfield\n\nTrack your package"
    got = ship_to("etsy_shipped", None, mail(plain))
    assert got == [dict(name="JANE DOE", loose="45 OAK HILL RD SPRINGFIELD")]  # street/town unclear
    pkg = P("45 Oak Hill Rd. Springfield", from_email=True)
    assert address.compare(P("45 Oak Hill Rd, Springfield, IL 62701"), pkg) is True
    assert address.compare(P("Springfield, IL"), pkg) is True
    assert address.compare(P("12 Oak Hill Rd, Springfield, IL"), pkg) is False


def test_shopify_block_stops_at_the_town_line():
    html = (
        "<h4>Shipping address</h4><p>Jane Doe<br>Acme Design LLC<br>45 Main St<br>Apt 4<br>"
        "Springfield IL 62701<br>United States</p><h4>Billing address</h4><p>9 Elm St<br>"
        "Shelbyville IL 62565</p>"
    )
    got = ship_to("shopify_shipped", None, mail(html=html))
    assert (
        got
        and got[0]["city"] == "SPRINGFIELD"
        and got[0]["zip"] == "62701"
        and got[0]["house"] == "45"
    )


# --------------------------------------------------------------- the filter ---


def replay_week() -> registry.Registry:
    reg = registry.Registry()
    reg.apply([(name, kb.parse_message(load(name), TZ)) for name in SHIPPING], NOW)
    reg.last_ok = NOW
    return reg


def test_filter_shows_only_matching_packages():
    reg = replay_week()
    everything = reg.snapshot(NOW)
    mine = reg.snapshot(NOW, address.AddressFilter.build([JANE_HOME]))
    assert everything.hidden == 0 and mine.hidden == 2
    assert len(mine.packages) + mine.hidden == len(everything.packages)
    for view in mine.packages:
        known = reg.places(reg.packages[view.key])
        assert not known or any(pl.city in (("SPRINGFIELD",), ()) for pl in known), view.item
    items = {v.item for v in mine.packages}
    assert {"Garden Hose", "Coffee Beans", "Wireless Mouse"} <= items  # Jane's Amazon orders
    assert "Kitchen" not in items  # the other household's order and box
    # USPS packages with no destination in their emails are shown by default...
    assert any(v.carrier == "USPS" and not reg.places(reg.packages[v.key]) for v in mine.packages)
    # ...and with unknowns off, only the ones Informed Delivery lists stay.
    strict = reg.snapshot(NOW, address.AddressFilter.build([JANE_HOME], show_unknown=False))
    for view in strict.packages:
        pkg = reg.packages[view.key]
        assert reg.places(pkg) or pkg.digest_date is not None, view.item
    no_digest = reg.snapshot(
        NOW,
        address.AddressFilter.build(
            [JANE_HOME], show_unknown=False, informed_delivery_is_mine=False
        ),
    )
    assert len(no_digest.packages) < len(strict.packages) < len(mine.packages)


def test_town_only_entry_matches_the_same_packages():
    reg = replay_week()
    by_town = reg.snapshot(NOW, address.AddressFilter.build(["Springfield, IL"]))
    by_address = reg.snapshot(NOW, address.AddressFilter.build([JANE_HOME]))
    assert {v.key for v in by_town.packages} == {v.key for v in by_address.packages}


def test_no_filter_shows_everything():
    reg = registry.Registry()
    reg.apply_event(
        event(
            tracking=[UPS],
            carrier="ups",
            ship_to=[P("9 OAK RD, SHELBYVILLE, IL 62565", from_email=True)],
        )
    )
    snap = reg.snapshot(NOW, address.AddressFilter.build([]))
    assert len(snap.packages) == 1 and snap.hidden == 0


def test_box_borrows_its_orders_address_and_back():
    reg = registry.Registry()
    order = "111-0000000-0000001"
    reg.apply_event(
        event(
            status=S.ORDERED,
            carrier="amazon",
            order_id=order,
            items=2,
            ship_to=[P("S - SHELBYVILLE, IL", from_email=True)],
        )
    )
    reg.apply_event(event(carrier="amazon", order_id=order, shipment_ids=["BOX1"], items=1))
    flt = address.AddressFilter.build([HOME])
    snap = reg.snapshot(NOW, flt)
    assert snap.packages == () and snap.hidden == 2
    box = next(p for p in reg.packages.values() if p.shipment_ids)
    assert [pl.city for pl in reg.places(box)] == [("SHELBYVILLE",)]
    # A box with its own address wins over its order's.
    reg.apply_event(
        event(
            carrier="amazon",
            order_id=order,
            shipment_ids=["BOX1"],
            ship_to=[P("J - SPRINGFIELD, IL", from_email=True)],
        )
    )
    assert [v.carrier for v in reg.snapshot(NOW, flt).packages] == ["Amazon"]


def test_addresses_survive_restart_and_merge():
    reg = registry.Registry()
    home = P("123 MAIN ST, SPRINGFIELD, IL 62701", from_email=True)
    reg.apply_event(event(tracking=[UPS], carrier="ups", ship_to=[home]))
    reg.apply_event(event(tracking=[UPS], carrier="ups", ship_to=[home]))  # the same again
    pkg = next(iter(reg.packages.values()))
    assert len(pkg.ship_to) == 1
    assert registry.Registry.from_dict(reg.to_dict()).packages[pkg.key].ship_to == pkg.ship_to
    assert registry.Registry.from_dict(
        {"packages": [{"key": "t:1", "status": 30, "ship_to": ["junk", {}]}]}
    )


def test_backfill_only_adds_addresses():
    reg = registry.Registry()
    reg.apply_event(event(tracking=[UPS], carrier="ups"))
    reg.apply_event(
        event(status=S.ORDERED, carrier="amazon", order_id="111-0000000-0000001", items=1)
    )
    before = {k: (p.status, p.last_update) for k, p in reg.packages.items()}
    updated = reg.backfill_addresses(
        [
            (
                "a",
                event(
                    tracking=[UPS],
                    carrier="ups",
                    status=S.DELIVERED,
                    ship_to=[P(HOME, from_email=True)],
                ),
            ),
            (
                "b",
                event(
                    carrier="amazon",
                    order_id="111-0000000-0000001",
                    shipment_ids=["NEWBOX"],
                    ship_to=[P("J - SPRINGFIELD, IL", from_email=True)],
                ),
            ),
            (
                "c",  # an order that isn't tracked
                event(
                    carrier="amazon",
                    order_id="111-9999999-0000001",
                    status=S.ORDERED,
                    ship_to=[P("S - SHELBYVILLE, IL", from_email=True)],
                ),
            ),
            ("d", None),
        ]
    )
    assert updated == 2
    assert {k: (p.status, p.last_update) for k, p in reg.packages.items()} == before
    assert all(p.ship_to for p in reg.packages.values()) and len(reg.packages) == 2


def test_legacy_package_shown_until_its_address_is_known():
    reg = registry.Registry()
    reg.apply_event(event(tracking=[UPS], carrier="ups"))
    pkg = next(iter(reg.packages.values()))
    strict = address.AddressFilter.build([HOME], show_unknown=False)
    assert reg.snapshot(NOW, strict).hidden == 1
    pkg.legacy = True
    assert reg.snapshot(NOW, strict).hidden == 0
    pkg.ship_to.append(P("9 OAK RD, SHELBYVILLE, IL 62565", from_email=True))
    assert reg.snapshot(NOW, strict).hidden == 1
    assert registry.Registry.from_dict(reg.to_dict()).packages[pkg.key].legacy
