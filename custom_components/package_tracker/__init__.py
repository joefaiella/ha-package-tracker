"""Package tracker: remembers packages from shipping emails in Gmail."""

import hashlib
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from . import websocket as card_ws
from .const import DOMAIN, STORAGE_VERSION
from .coordinator import PackageTrackerConfigEntry, PackageTrackerCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.TODO]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
CARD_FILE = Path(__file__).parent / "frontend" / "package-tracker-card.js"
CARD_URL = f"/{DOMAIN}/package-tracker-card.js"


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the card's websocket commands and serve its JavaScript on every dashboard."""
    card_ws.async_register_commands(hass)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=True)]
    )
    if "frontend" in hass.config.components:
        integration = await async_get_integration(hass, DOMAIN)
        digest = await hass.async_add_executor_job(_file_hash, CARD_FILE)
        # A changed file gets a new URL, so browsers never run a stale cached copy.
        add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}-{digest}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PackageTrackerConfigEntry) -> bool:
    coordinator = PackageTrackerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def _push_card() -> None:
        card_ws.async_push(hass)

    entry.async_on_unload(coordinator.async_add_listener(_push_card))
    _push_card()  # cards left open across a reload get the new data straight away
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PackageTrackerConfigEntry) -> bool:
    if ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        card_ws.async_push(hass)  # open cards drop this account's packages
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: PackageTrackerConfigEntry) -> None:
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
