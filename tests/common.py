"""Fixture loading and values shared by the tests.

The emails in fixtures/ describe one week for a household that shares its Amazon account with a
second one: Jane Doe at 123 Main St, Springfield, IL 62701, and Max in Shelbyville, IL. Every
name, address, order id and tracking number in them is made up.
"""

import datetime as dt
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
import re
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.package_tracker import imap_client, kb
from custom_components.package_tracker.const import DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/Chicago")
NOW = dt.datetime(2026, 10, 9, 11, 0, tzinfo=TZ)  # Friday morning, after the week's mail
HOME = "123 Main St, Springfield, IL 62701"

ALL_MAIL = sorted(p.name for p in FIXTURES.glob("*.eml"))
NOT_SHIPPING = {
    "amazon_marketing.eml",
    "phishing_lookalike_usps.eml",
    "phishing_spoofed_fedex.eml",
    "store_newsletter.eml",
}
SHIPPING = [name for name in ALL_MAIL if name not in NOT_SHIPPING]

# Tracking numbers (all pass their carrier's check digit)
USPS_EXPECTED = "9400111899223300517209"
USPS_OFD = "9400111899223300644189"
USPS_DELIVERED = "9400111899223300772066"
USPS_ATTEMPTED = "9400111899223300835914"
USPS_AWAITING = "9400111899223300926131"
USPS_OUTBOUND = "9400111899223301090428"
USPS_SHOPIFY = "9405511899223301205757"
UPS = "1ZA81F270312345675"
UPS_DELIVERED = "1ZA81F270398765435"
UPS_STORE = "1ZW4X9810355501231"
UPS_PARCELPANEL = "1ZW4X9810377702465"
FEDEX_OFD = "771055230496"
FEDEX_GROUND = "398715620041273"

# Order ids
ORDER_KITCHEN = "112-3094415-2208734"  # Max: 3 items, one box shipped so far
ORDER_COFFEE = "113-5571824-6619047"
ORDER_LAMP = "114-7263590-1185432"
ORDER_MOUSE = "116-2058813-4471920"
ORDER_HOSE = "117-6620481-3359024"
BOX_KITCHEN = "Bk7rQm2Lp"
BOX_LAMP = "Dw2Hn8VsK"
BOX_HOSE = "Ld4Wp7NzQ"
WALMART_ORDER = "2000456-71234567"
ETSY_ORDER = "etsy-4102938475"


def raw(name: str) -> bytes:
    """A fixture as IMAP serves it (CRLF line endings)."""
    return re.sub(rb"\r?\n", b"\r\n", (FIXTURES / name).read_bytes())


def load(name: str) -> EmailMessage:
    return message_from_bytes(raw(name), policy=default_policy)


def parse(name: str) -> kb.Event | kb.Digest | None:
    return kb.parse_message(load(name), TZ)


def event(**kwargs) -> kb.Event:
    """An in-transit Event dated NOW, with any field overridden."""
    base = {"source": "test", "status": kb.Status.IN_TRANSIT, "email_date": NOW}
    return kb.Event(**{**base, **kwargs})


def fake_mail(frm: str, subject: str, body: str, auth: str | None = None) -> EmailMessage:
    """A minimal email, with a Gmail Authentication-Results header when `auth` is given."""
    text = f"From: {frm}\r\nSubject: {subject}\r\nDate: Fri, 9 Oct 2026 09:00:00 -0500\r\n"
    if auth:
        text += f"Authentication-Results: mx.google.com; {auth}\r\n"
    return message_from_bytes(f"{text}\r\n{body}\r\n".encode(), policy=default_policy)


# ------------------------------------------------------- Home Assistant ---

SCAN = "custom_components.package_tracker.coordinator.imap_client.scan"
CHECK_LOGIN = "custom_components.package_tracker.config_flow.imap_client.check_login"
ACCOUNT = "jane.doe@example.com"
APP_PASSWORD = "abcdabcdabcdabcd"
FROZEN_NOW = NOW.isoformat()


def week_of_mail() -> imap_client.ScanResult:
    """Every fixture, as one scan would return them."""
    mails = [
        imap_client.RawMail(
            msgid=str(1000 + i), folder="all", uid=100 + i, internaldate=None, raw=raw(name)
        )
        for i, name in enumerate(ALL_MAIL)
    ]
    return imap_client.ScanResult(
        mails=mails,
        cursors={"all": {"uidvalidity": 11, "last_uid": 200}},
        headers_checked=len(mails),
    )


def nothing_new() -> imap_client.ScanResult:
    return imap_client.ScanResult(cursors={"all": {"uidvalidity": 11, "last_uid": 200}})


def mock_entry(hass: HomeAssistant, account: str = ACCOUNT, **kwargs) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=account,
        title=account,
        data={CONF_EMAIL: account, CONF_PASSWORD: APP_PASSWORD},
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


async def setup_entry(
    hass: HomeAssistant,
    *,
    account: str = ACCOUNT,
    scan: imap_client.ScanResult | None = None,
    side_effect=None,
    **entry_kwargs,
) -> MockConfigEntry:
    """Set up one Gmail account; by default its first scan finds the whole week of mail."""
    entry = mock_entry(hass, account, **entry_kwargs)
    with patch(SCAN, AsyncMock(return_value=scan or week_of_mail(), side_effect=side_effect)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return entry
