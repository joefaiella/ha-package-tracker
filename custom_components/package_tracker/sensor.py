"""Package counts (arriving today, out for delivery, ...) and the last Gmail check."""

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PackageTrackerConfigEntry, PackageTrackerCoordinator
from .entity import PackageTrackerEntity
from .registry import PackageView, TrackerSnapshot

PARALLEL_UPDATES = 0
MAX_LISTED = 25


@dataclass(frozen=True, kw_only=True)
class PackageSensorDescription(SensorEntityDescription):
    items_fn: Callable[[TrackerSnapshot], tuple[PackageView, ...]]


SENSORS: tuple[PackageSensorDescription, ...] = (
    PackageSensorDescription(
        key="arriving_today",
        translation_key="arriving_today",
        items_fn=lambda s: s.arriving_today,
    ),
    PackageSensorDescription(
        key="out_for_delivery",
        translation_key="out_for_delivery",
        items_fn=lambda s: s.out_for_delivery,
    ),
    PackageSensorDescription(
        key="on_the_way",
        translation_key="on_the_way",
        items_fn=lambda s: s.on_the_way,
    ),
    PackageSensorDescription(
        key="delivered_today",
        translation_key="delivered_today",
        items_fn=lambda s: s.delivered_today,
    ),
    PackageSensorDescription(
        key="ordered",
        translation_key="ordered",
        items_fn=lambda s: s.ordered,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PackageTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [PackageCountSensor(coordinator, d) for d in SENSORS]
    entities += [MailTodaySensor(coordinator), LastCheckSensor(coordinator)]
    async_add_entities(entities)


class PackageCountSensor(PackageTrackerEntity, SensorEntity):
    entity_description: PackageSensorDescription
    _attr_native_unit_of_measurement = "packages"
    _unrecorded_attributes = frozenset({"packages"})

    def __init__(
        self, coordinator: PackageTrackerCoordinator, description: PackageSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int:
        return len(self.entity_description.items_fn(self.coordinator.data))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items = self.entity_description.items_fn(self.coordinator.data)
        return {"packages": [p.as_public_dict() for p in items[:MAX_LISTED]]}


class MailTodaySensor(PackageTrackerEntity, SensorEntity):
    _attr_translation_key = "mail_today"
    _attr_native_unit_of_measurement = "pieces"

    def __init__(self, coordinator: PackageTrackerCoordinator) -> None:
        super().__init__(coordinator, "mail_today")

    @property
    def native_value(self) -> int:
        return self.coordinator.data.mail_pieces_today or 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"digest_received": self.coordinator.data.mail_pieces_today is not None}


class LastCheckSensor(PackageTrackerEntity, SensorEntity):
    _attr_translation_key = "last_check"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: PackageTrackerCoordinator) -> None:
        super().__init__(coordinator, "last_check")

    @property
    def native_value(self) -> dt.datetime | None:
        return self.coordinator.data.last_check

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        attrs: dict[str, Any] = {"last_error": data.last_error, **self.coordinator.last_stats}
        if self.coordinator.address_filter.active:
            attrs["hidden_by_address_filter"] = data.hidden
        return attrs
