"""Upgrading saved data: reading recent mail once more to learn addresses and email ids."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.package_tracker import imap_client

from .common import FEDEX_GROUND, SCAN, nothing_new, raw, setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "clock")
ENTRY_ID = "01PKGTEST"
STORE_KEY = f"package_tracker.{ENTRY_ID}"
OLD_PACKAGE = {
    "key": f"t:{FEDEX_GROUND}",
    "status": 30,
    "carrier": "fedex",
    "tracking": [FEDEX_GROUND],
}
ELSEWHERE = {"addresses": ["Nowhere, ZZ 00000"], "show_unknown_address": False}


def saved(data: dict) -> dict:
    """Storage as version 0.1 wrote it."""
    return {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": data}


def lookup(kwargs: dict) -> bool:
    """The one-off address look-up reads 30 days from scratch."""
    return kwargs["lookback_days"] == 30


async def test_upgrade_learns_addresses_without_replaying_old_mail(
    hass: HomeAssistant, hass_storage
):
    """Old mail only adds addresses: no package is created and a retired order stays retired."""
    hass_storage[STORE_KEY] = saved(
        {
            "packages": [OLD_PACKAGE],
            "tombstones": {"o:111-0000000-0000003": "2026-10-08"},
            "seen": {"123": "2026-10-08"},
            "cursors": {"all": {"uidvalidity": 11, "last_uid": 200}},
            "mail": {},
            "last_ok": None,
        }
    )
    old_mail = imap_client.ScanResult(
        mails=[
            imap_client.RawMail(msgid=str(i), folder="all", uid=i, internaldate=None, raw=raw(name))
            for i, name in enumerate(
                ["fedex_on_the_way.eml", "amazon_shipped.eml", "etsy_shipped.eml"]
            )
        ],
        cursors={"all": {"uidvalidity": 11, "last_uid": 300}},
        headers_checked=3,
    )
    calls: list[dict] = []

    async def fake_scan(*args, **kwargs):
        calls.append({**kwargs, "seen": set(kwargs["seen"]), "cursors": dict(kwargs["cursors"])})
        return old_mail if kwargs["cursors"] == {} else nothing_new()

    entry = await setup_entry(hass, side_effect=fake_scan, entry_id=ENTRY_ID, options=ELSEWHERE)
    normal, backfill = calls[:2]
    assert normal["cursors"] == {"all": {"uidvalidity": 11, "last_uid": 200}} and normal[
        "seen"
    ] == {"123"}
    assert backfill["cursors"] == {} and backfill["seen"] == set() and lookup(backfill)
    reg = entry.runtime_data.registry
    assert list(reg.packages) == [f"t:{FEDEX_GROUND}"]
    assert reg.packages[f"t:{FEDEX_GROUND}"].ship_to[0].city == ("SPRINGFIELD",)
    assert reg.seen == {"123": "2026-10-08"} and reg.cursors["all"]["last_uid"] == 200
    assert not reg.backfill_pending and "o:111-0000000-0000003" in reg.tombstones
    # Its address is known now and isn't one of the addresses to show.
    assert hass.states.get("sensor.package_tracker_on_the_way").state == "0"
    assert hass_storage[STORE_KEY]["minor_version"] == 3


async def test_upgrade_keeps_old_packages_visible_until_an_address_is_known(
    hass: HomeAssistant, hass_storage
):
    hass_storage[STORE_KEY] = saved({"packages": [OLD_PACKAGE], "seen": {}, "cursors": {}})
    await setup_entry(hass, scan=nothing_new(), entry_id=ENTRY_ID, options=ELSEWHERE)
    assert hass.states.get("sensor.package_tracker_on_the_way").state == "1"


async def test_failed_address_lookup_retries_then_gives_up(hass: HomeAssistant, hass_storage):
    hass_storage[STORE_KEY] = saved({"packages": []})

    async def fake_scan(*args, **kwargs):
        if kwargs["cursors"] == {} and not kwargs["seen"] and lookup(kwargs):
            raise imap_client.GmailError("IMAP CommandTimeout")
        return nothing_new()

    scan = AsyncMock(side_effect=fake_scan)
    entry = await setup_entry(hass, side_effect=fake_scan, entry_id=ENTRY_ID)
    coordinator = entry.runtime_data
    assert coordinator.registry.backfill_pending
    with patch(SCAN, scan):
        for _ in range(2):
            await coordinator.async_refresh()
            await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.registry.backfill_pending


async def test_address_lookup_reads_a_busy_mailbox_in_several_passes(
    hass: HomeAssistant, hass_storage
):
    hass_storage[STORE_KEY] = saved(
        {
            "packages": [OLD_PACKAGE],
            "seen": {},
            "cursors": {"all": {"uidvalidity": 11, "last_uid": 9000}},
        }
    )
    first = imap_client.ScanResult(
        cursors={"all": {"uidvalidity": 11, "last_uid": 1500}}, headers_checked=1500, truncated=True
    )
    second = imap_client.ScanResult(
        mails=[
            imap_client.RawMail(
                msgid="9",
                folder="all",
                uid=1600,
                internaldate=None,
                raw=raw("fedex_on_the_way.eml"),
            )
        ],
        cursors={"all": {"uidvalidity": 11, "last_uid": 9000}},
        headers_checked=7500,
    )
    passes: list[dict] = []

    async def fake_scan(*args, **kwargs):
        if not lookup(kwargs):
            return nothing_new()
        passes.append(dict(kwargs["cursors"]))
        return first if not kwargs["cursors"] else second

    entry = await setup_entry(hass, side_effect=fake_scan, entry_id=ENTRY_ID)
    assert passes == [{}, {"all": {"uidvalidity": 11, "last_uid": 1500}}]
    reg = entry.runtime_data.registry
    assert reg.packages[f"t:{FEDEX_GROUND}"].ship_to and not reg.backfill_pending


async def test_unexpected_error_in_address_lookup_gives_up(hass: HomeAssistant, hass_storage):
    hass_storage[STORE_KEY] = saved({"packages": []})

    async def fake_scan(*args, **kwargs):
        if lookup(kwargs):
            raise RuntimeError("boom")
        return nothing_new()

    scan = AsyncMock(side_effect=fake_scan)
    entry = await setup_entry(hass, side_effect=fake_scan, entry_id=ENTRY_ID)
    coordinator = entry.runtime_data
    with patch(SCAN, scan):
        for _ in range(3):
            await coordinator.async_refresh()
            await hass.async_block_till_done(wait_background_tasks=True)
    assert not coordinator.registry.backfill_pending and coordinator._backfill_failures == 3
