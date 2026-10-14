"""Shared fixtures for the Home Assistant tests."""

from homeassistant.core import HomeAssistant
import pytest

from .common import FROZEN_NOW, TZ


@pytest.fixture
async def clock(hass: HomeAssistant, freezer) -> None:
    """Home Assistant in the fixtures' time zone, with the clock at NOW."""
    await hass.config.async_set_time_zone(str(TZ))
    freezer.move_to(FROZEN_NOW)
