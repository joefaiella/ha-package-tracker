"""The Packages card: its live feed, ticking packages off, and serving its JavaScript."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import CLIENT_ID

from custom_components.package_tracker import imap_client
from custom_components.package_tracker.kb import Status
from custom_components.package_tracker.websocket import row_id

from .common import (
    FEDEX_GROUND,
    FEDEX_OFD,
    HOME,
    NOW,
    SCAN,
    nothing_new,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "clock")
SECOND_ACCOUNT = "jane.doe.work@example.com"


async def ws_client(hass: HomeAssistant, hass_ws_client, user):
    """A websocket client whose token is issued after the clock was frozen."""
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    return await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))


async def subscribe(hass: HomeAssistant, hass_ws_client, user) -> tuple:
    client = await ws_client(hass, hass_ws_client, user)
    await client.send_json_auto_id({"type": "package_tracker/subscribe"})
    msg = await client.receive_json()
    assert msg["type"] == "result" and msg["success"] is True
    first = await client.receive_json()
    assert first["id"] == msg["id"] and first["type"] == "event"
    return client, msg["id"], first["event"]


async def tick(client, package_id: str, received: bool = True) -> dict:
    await client.send_json_auto_id(
        {"type": "package_tracker/set_received", "package": package_id, "received": received}
    )
    return await client.receive_json()


async def test_card_feed_and_ticking(hass: HomeAssistant, hass_ws_client, hass_admin_user):
    entry = await setup_entry(hass)
    client, sub_id, data = await subscribe(hass, hass_ws_client, hass_admin_user)
    assert data["loaded"] is True
    assert data["counts"]["arriving_today"] == 4 and data["mail_today"] == 3
    hose = next(p for p in data["packages"] if p["item"] == "Garden Hose")
    assert hose["url"].startswith("https://www.amazon.com/progress-tracker/package?orderId=")
    assert hose["email_url"].startswith(
        "https://mail.google.com/mail/u/?authuser=jane.doe%40example.com#all/"
    )
    assert "on_the_way" in hose["groups"] and hose["done"] is False
    fedex = {p["url"] for p in data["packages"] if p["carrier"] == "FedEx"}
    assert fedex == {
        f"https://www.fedex.com/fedextrack/?trknbr={FEDEX_OFD}",
        f"https://www.fedex.com/fedextrack/?trknbr={FEDEX_GROUND}",
    }
    assert all(p["url"] is None or p["url"].startswith("https://") for p in data["packages"])
    assert not any(p["done"] and "delivered_today" not in p["groups"] for p in data["packages"])
    assert "key" not in hose and len(hose["id"]) == 16  # no tracking or order numbers as row ids
    # Tick it off from the card: a result and an updated list arrive.
    await client.send_json_auto_id(
        {"type": "package_tracker/set_received", "package": hose["id"], "received": True}
    )
    got = {m["type"]: m for m in (await client.receive_json(), await client.receive_json())}
    assert got["result"]["success"] is True
    row = next(p for p in got["event"]["event"]["packages"] if p["id"] == hose["id"])
    assert row["done"] and row["ticked"] and row["can_tick"]
    assert row["groups"] == ["delivered_today"]
    # A reload (e.g. after changing options) says "not loaded", then sends the new data.
    with patch(SCAN, AsyncMock(return_value=nothing_new())):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert (await client.receive_json())["event"] == {"loaded": False, "packages": []}
    assert (await client.receive_json())["event"]["loaded"] is True
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    assert (await client.receive_json())["success"] is True


async def test_card_feed_respects_the_address_filter(
    hass: HomeAssistant, hass_ws_client, hass_admin_user
):
    await setup_entry(hass, options={"addresses": [f"Jane Doe, {HOME}"]})
    _client, _sub, data = await subscribe(hass, hass_ws_client, hass_admin_user)
    items = {p["item"] for p in data["packages"]}
    assert "Garden Hose" in items and "Kitchen" not in items
    assert data["hidden"] == 2


async def test_card_commands_reject_bad_input(hass: HomeAssistant, hass_ws_client, hass_admin_user):
    await setup_entry(hass)
    client = await ws_client(hass, hass_ws_client, hass_admin_user)
    await client.send_json_auto_id({"type": "package_tracker/set_received", "package": "abc"})
    msg = await client.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "invalid_format"
    msg = await tick(client, "t:nope")
    assert msg["success"] is False and msg["error"]["code"] == "not_found"


async def test_card_javascript_is_served_and_loaded(hass: HomeAssistant, hass_client):
    hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *_: None, [])
    hass.config.components.add("frontend")
    await setup_entry(hass)
    (url,) = hass.data[DATA_EXTRA_MODULE_URL].urls
    assert url.startswith("/package_tracker/package-tracker-card.js?v=0.3.0-")
    resp = await (await hass_client()).get(url)
    assert resp.status == 200 and "customElements.define" in await resp.text()


async def test_card_cannot_untick_a_carrier_delivery(
    hass: HomeAssistant, hass_ws_client, hass_admin_user
):
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    key = f"t:{FEDEX_OFD}"
    pkg = coordinator.registry.packages[key]
    pkg.status, pkg.delivered_at = Status.DELIVERED, NOW
    coordinator.async_set_updated_data(coordinator.snapshot(NOW))
    client, _sub, data = await subscribe(hass, hass_ws_client, hass_admin_user)
    row = next(p for p in data["packages"] if p["id"] == row_id(entry.entry_id, key))
    assert row["done"] and row["can_tick"] is False
    msg = await tick(client, row["id"], received=False)
    assert msg["success"] is False and msg["error"]["code"] == "not_supported"


async def test_card_tick_needs_control_of_the_todo_list(
    hass: HomeAssistant, hass_ws_client, hass_read_only_user
):
    await setup_entry(hass)
    client, _sub, data = await subscribe(hass, hass_ws_client, hass_read_only_user)
    msg = await tick(client, data["packages"][0]["id"])
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"


async def test_card_cannot_tick_a_hidden_package(
    hass: HomeAssistant, hass_ws_client, hass_admin_user
):
    entry = await setup_entry(hass, options={"addresses": [f"Jane Doe, {HOME}"]})
    client, _sub, data = await subscribe(hass, hass_ws_client, hass_admin_user)
    shown = {p["id"] for p in data["packages"]}
    registry = entry.runtime_data.registry
    hidden = next(k for k in registry.packages if row_id(entry.entry_id, k) not in shown)
    msg = await tick(client, row_id(entry.entry_id, hidden))
    assert msg["success"] is False and msg["error"]["code"] == "not_found"
    assert registry.packages[hidden].manual is None


async def test_card_shows_a_gmail_login_problem(
    hass: HomeAssistant, hass_ws_client, hass_admin_user
):
    entry = await setup_entry(hass)
    client, _sub, _data = await subscribe(hass, hass_ws_client, hass_admin_user)
    with patch(SCAN, AsyncMock(side_effect=imap_client.GmailAuthError("[AUTHENTICATIONFAILED]"))):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    msg = await client.receive_json()
    assert "AUTHENTICATIONFAILED" in msg["event"]["last_error"] and msg["event"]["packages"]


async def test_card_with_two_gmail_accounts(hass: HomeAssistant, hass_ws_client, hass_admin_user):
    first = await setup_entry(hass)
    second = await setup_entry(hass, account=SECOND_ACCOUNT)
    client, _sub, data = await subscribe(hass, hass_ws_client, hass_admin_user)
    ids = [p["id"] for p in data["packages"]]
    assert len(set(ids)) == len(ids)  # both accounts, ids never collide
    assert data["counts"]["arriving_today"] == 8
    accounts = {p["email_url"].split("#")[0] for p in data["packages"] if p.get("email_url")}
    assert accounts == {
        "https://mail.google.com/mail/u/?authuser=jane.doe%40example.com",
        "https://mail.google.com/mail/u/?authuser=jane.doe.work%40example.com",
    }
    # Ticking one of the second account's packages changes only that account.
    first_ids = {row_id(first.entry_id, k) for k in first.runtime_data.registry.packages}
    target = next(p for p in data["packages"] if p["id"] not in first_ids and not p["done"])
    await client.send_json_auto_id(
        {"type": "package_tracker/set_received", "package": target["id"], "received": True}
    )
    got = {m["type"]: m for m in (await client.receive_json(), await client.receive_json())}
    assert got["result"]["success"] is True
    assert (
        sum(1 for p in second.runtime_data.registry.packages.values() if p.manual == "received")
        == 1
    )
    assert not any(p.manual for p in first.runtime_data.registry.packages.values())
    # Unloading one account leaves the other on the card.
    assert await hass.config_entries.async_unload(second.entry_id)
    await hass.async_block_till_done()
    msg = await client.receive_json()
    assert msg["event"]["loaded"] is True and msg["event"]["counts"]["arriving_today"] == 4
