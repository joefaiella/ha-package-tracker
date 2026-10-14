"""Running the integration: sensors, the to-do list, the voice assistant tool and diagnostics."""

from unittest.mock import AsyncMock, patch

from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm
from homeassistant.setup import async_setup_component
import pytest

from custom_components.package_tracker import imap_client
from custom_components.package_tracker.diagnostics import async_get_config_entry_diagnostics

from .common import (
    ACCOUNT,
    APP_PASSWORD,
    HOME,
    ORDER_HOSE,
    SCAN,
    SHIPPING,
    USPS_EXPECTED,
    nothing_new,
    setup_entry,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "clock")
TODO = "todo.package_tracker_packages"
TOOL = "package_tracker__get_packages"
JANE_HOME = f"Jane Doe, {HOME}"
ASSIST = llm.LLMContext(
    platform="test", context=None, language="en", assistant="conversation", device_id=None
)


def sensor(hass: HomeAssistant, key: str):
    return hass.states.get(f"sensor.package_tracker_{key}")


async def todo_items(hass: HomeAssistant) -> list[dict]:
    resp = await hass.services.async_call(
        "todo", "get_items", {"entity_id": TODO}, blocking=True, return_response=True
    )
    return resp[TODO]["items"]


async def assist_tools(hass: HomeAssistant) -> set[str]:
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, ASSIST)
    return {t.name for t in api.tools}


async def voice_tool(hass: HomeAssistant, when: str) -> dict:
    api = await llm.async_get_api(hass, llm.LLM_API_ASSIST, ASSIST)
    result = await api.async_call_tool(llm.ToolInput(tool_name=TOOL, tool_args={"when": when}))
    return result.data if hasattr(result, "data") else result


# ---------------------------------------------------------------- sensors ---


async def test_counts_for_the_week(hass: HomeAssistant):
    await setup_entry(hass)
    assert sensor(hass, "arriving_today").state == "4"
    assert sensor(hass, "out_for_delivery").state == "3"
    assert sensor(hass, "on_the_way").state == "10"
    assert sensor(hass, "ordered").state == "2"
    assert sensor(hass, "delivered_today").state == "2"
    assert sensor(hass, "mail_today").state == "3"
    fedex = next(
        p
        for p in sensor(hass, "out_for_delivery").attributes["packages"]
        if p["carrier"] == "FedEx"
    )
    assert fedex["window"] == "between 2:00pm and 6:00pm" and fedex["tracking_last4"] == "0496"
    last_check = sensor(hass, "last_email_check")
    assert last_check.state == "2026-10-09T16:00:00+00:00"
    assert last_check.attributes["shipping_emails"] == len(SHIPPING)


async def test_gmail_outage_keeps_packages(hass: HomeAssistant):
    entry = await setup_entry(hass)
    with patch(SCAN, AsyncMock(side_effect=imap_client.GmailError("no IMAP greeting: timeout"))):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    assert sensor(hass, "arriving_today").state == "4"
    assert "no IMAP greeting" in sensor(hass, "last_email_check").attributes["last_error"]


async def test_gmail_down_at_startup_still_sets_up(hass: HomeAssistant):
    entry = await setup_entry(hass, side_effect=TimeoutError())
    assert entry.state is ConfigEntryState.LOADED
    assert sensor(hass, "on_the_way").state == "0"


async def test_packages_survive_restart(hass: HomeAssistant):
    entry = await setup_entry(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with patch(SCAN, AsyncMock(return_value=nothing_new())):  # every email deleted meanwhile
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert sensor(hass, "arriving_today").state == "4"


async def test_multi_day_outage_retires_nothing(hass: HomeAssistant, freezer):
    """Days of failed Gmail checks aren't "no news": nothing is retired until mail is read."""
    entry = await setup_entry(hass)
    coordinator = entry.runtime_data
    before = len(coordinator.registry.packages)
    for day in range(10, 15):
        freezer.move_to(f"2026-10-{day} 11:00:00-05:00")
        with patch(SCAN, AsyncMock(side_effect=imap_client.GmailError("IMAP CommandTimeout"))):
            await coordinator.async_refresh()
            await hass.async_block_till_done()
    assert len(coordinator.registry.packages) == before
    assert sensor(hass, "on_the_way").state != "unavailable"
    with patch(SCAN, AsyncMock(return_value=nothing_new())):  # Gmail is back, nothing new
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert len(coordinator.registry.packages) < before


# ------------------------------------------------------------- to-do list ---


async def test_todo_list_reads_and_refuses_adding(hass: HomeAssistant):
    await setup_entry(hass)
    items = await todo_items(hass)
    assert len(items) == 16  # 14 not yet delivered (one probably delivered) + 2 delivered today
    assert sum(1 for i in items if i["status"] == "completed") == 3
    assert any(i["summary"] == "Garden Hose" and i["due"] == "2026-10-10" for i in items)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "todo", "add_item", {"entity_id": TODO, "item": "x"}, blocking=True
        )


async def test_ticking_a_package_marks_it_received(hass: HomeAssistant):
    await setup_entry(hass)

    async def hose(service: str = "update_item", **data) -> dict:
        await hass.services.async_call(
            "todo", service, {"entity_id": TODO, "item": "Garden Hose", **data}, blocking=True
        )
        return next(i for i in await todo_items(hass) if i["summary"] == "Garden Hose")

    item = await hose(status="completed")
    assert item["status"] == "completed" and "ticked off" in item["description"]
    assert sensor(hass, "on_the_way").state == "9"
    assert sensor(hass, "delivered_today").state == "2"  # ticked, not delivered by the carrier
    item = await hose(status="needs_action")
    assert item["status"] == "needs_action"
    assert sensor(hass, "on_the_way").state == "10"
    with pytest.raises(HomeAssistantError):
        await hose(rename="Hose")
    with pytest.raises(HomeAssistantError):
        await hose("remove_item")


# ------------------------------------------------------- voice assistant ---


async def test_voice_tool(hass: HomeAssistant):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    await setup_entry(hass)
    # To-do lists are exposed to Assist by default; with this one unexposed, nothing is.
    async_expose_entity(hass, "conversation", TODO, False)
    assert TOOL not in await assist_tools(hass)
    async_expose_entity(hass, "conversation", "sensor.package_tracker_arriving_today", True)
    assert TOOL in await assist_tools(hass)
    data = await voice_tool(hass, "today")
    assert data["count"] == 6 and data["mail_pieces_today"] == 3  # 4 arriving + 2 delivered
    assert all("tracking" not in k or k == "tracking_last4" for p in data["packages"] for k in p)


# ------------------------------------------------------------ diagnostics ---


async def test_diagnostics_hold_no_mail_contents(hass: HomeAssistant):
    entry = await setup_entry(hass)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["counts"]["arriving_today"] == 4
    text = str(diag)
    for secret in (ACCOUNT, APP_PASSWORD, USPS_EXPECTED, ORDER_HOSE, "Garden Hose"):
        assert secret not in text


# --------------------------------------------------------- address filter ---


async def test_address_filter_applies_everywhere(hass: HomeAssistant):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "llm", {})
    entry = await setup_entry(hass, options={"addresses": [JANE_HOME]})
    assert sensor(hass, "ordered").state == "1"  # the other household's order is hidden
    assert [p["item"] for p in sensor(hass, "ordered").attributes["packages"]] == ["Wireless Mouse"]
    hidden = sensor(hass, "last_email_check").attributes["hidden_by_address_filter"]
    assert hidden == 2
    summaries = {i["summary"] for i in await todo_items(hass)}
    assert "Garden Hose" in summaries and "Kitchen" not in summaries
    async_expose_entity(hass, "conversation", "sensor.package_tracker_arriving_today", True)
    data = await voice_tool(hass, "all")
    assert "Kitchen" not in {p["item"] for p in data["packages"]}
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["options"]["addresses"] == "1 address(es)"
    assert diag["hidden_by_address_filter"] == hidden
    text = str(diag).upper()
    assert all(part.strip().upper() not in text for part in JANE_HOME.split(",")[:3])
