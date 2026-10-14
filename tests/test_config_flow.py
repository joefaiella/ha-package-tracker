"""Setting up a Gmail account, re-entering its app password, and the options."""

import datetime as dt
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.package_tracker import imap_client
from custom_components.package_tracker.const import DOMAIN

from .common import ACCOUNT, CHECK_LOGIN, SCAN, mock_entry, nothing_new, setup_entry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
NUMBERS = {"scan_minutes": 10, "lookback_days": 14, "keep_delivered_days": 2}


async def test_user_flow_creates_entry(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with (
        patch(CHECK_LOGIN, AsyncMock(return_value=None)) as check,
        patch(SCAN, AsyncMock(return_value=nothing_new())),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_EMAIL: " Jane.Doe@Example.com ", CONF_PASSWORD: "abcd efgh ijkl mnop"},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_EMAIL: ACCOUNT, CONF_PASSWORD: "abcdefghijklmnop"}
    assert check.await_args.args[:2] == (ACCOUNT, "abcdefghijklmnop")


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (imap_client.GmailAuthError("NO [AUTHENTICATIONFAILED]"), "invalid_auth"),
        (imap_client.GmailError("no IMAP greeting"), "cannot_connect"),
        (imap_client.GmailError("not_gmail"), "not_gmail"),
        (TimeoutError(), "cannot_connect"),
        (RuntimeError("boom"), "unknown"),
    ],
)
async def test_user_flow_errors(hass: HomeAssistant, error, reason):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(CHECK_LOGIN, AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: ACCOUNT, CONF_PASSWORD: "x"}
        )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": reason}


async def test_same_account_twice_aborts(hass: HomeAssistant):
    mock_entry(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    with patch(CHECK_LOGIN, AsyncMock(return_value=None)):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: "JANE.DOE@example.com", CONF_PASSWORD: "x"}
        )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


@pytest.mark.usefixtures("clock")
async def test_revoked_password_starts_reauth(hass: HomeAssistant):
    entry = await setup_entry(hass)
    with patch(SCAN, AsyncMock(side_effect=imap_client.GmailAuthError("[AUTHENTICATIONFAILED]"))):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    flows = [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["context"]["source"] == SOURCE_REAUTH
    ]
    assert len(flows) == 1
    with patch(CHECK_LOGIN, AsyncMock(side_effect=imap_client.GmailAuthError("NO"))):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_PASSWORD: "still wrong"}
        )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "invalid_auth"}
    with (
        patch(CHECK_LOGIN, AsyncMock(return_value=None)),
        patch(SCAN, AsyncMock(return_value=nothing_new())),
    ):
        result = await hass.config_entries.flow.async_configure(
            flows[0]["flow_id"], {CONF_PASSWORD: "new pass word"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "newpassword"


@pytest.mark.usefixtures("clock")
async def test_options_reload(hass: HomeAssistant):
    entry = await setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SCAN, AsyncMock(return_value=nothing_new())):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_minutes": 15, "lookback_days": 20, "keep_delivered_days": 3}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "scan_minutes": 15,
        "lookback_days": 20,
        "keep_delivered_days": 3,
        "addresses": [],
        "show_unknown_address": True,
        "informed_delivery_is_mine": True,
    }
    assert entry.runtime_data.update_interval == dt.timedelta(minutes=15)


@pytest.mark.usefixtures("clock")
async def test_address_option_round_trip(hass: HomeAssistant):
    entry = await setup_entry(hass)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**NUMBERS, "addresses": ["NY"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"addresses": "address_too_short"}
    assert result["description_placeholders"]["entry"] == "NY"
    with patch(SCAN, AsyncMock(return_value=nothing_new())):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                **NUMBERS,
                "addresses": ["", "Jane Doe, Springfield, IL", "jane doe, springfield, il"],
                "show_unknown_address": False,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["addresses"] == ["Jane Doe, Springfield, IL"]
    assert entry.options["show_unknown_address"] is False
    # Removing every row sends no "addresses" key; the list must end up empty, not come back.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(SCAN, AsyncMock(return_value=nothing_new())):
        await hass.config_entries.options.async_configure(flow["flow_id"], dict(NUMBERS))
        await hass.async_block_till_done()
    assert entry.options["addresses"] == []
    assert entry.runtime_data.address_filter.active is False
