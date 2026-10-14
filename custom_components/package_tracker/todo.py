"""To-do list of packages: one item per package, due on its expected date."""

import datetime as dt
import hashlib
from typing import override

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PackageTrackerConfigEntry, PackageTrackerCoordinator
from .entity import PackageTrackerEntity
from .kb import Status
from .registry import PROBABLY_DELIVERED, PackageView

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PackageTrackerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([PackagesTodoList(entry.runtime_data)])


def _uid(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:12]  # no tracking numbers in uids


class PackagesTodoList(PackageTrackerEntity, TodoListEntity):
    """Ticking a package marks it received; adding, renaming and deleting are refused."""

    _attr_translation_key = "packages"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, coordinator: PackageTrackerCoordinator) -> None:
        super().__init__(coordinator, "packages")
        self._keys: dict[str, str] = {}
        self._attr_todo_items = self._items()

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._attr_todo_items = self._items()
        super()._handle_coordinator_update()

    def _shown(self) -> list[PackageView]:
        snap = self.coordinator.data
        # Everything not yet delivered ("probably delivered" shows ticked), today's deliveries,
        # and anything ticked off by hand today so a mis-tick can be undone.
        return (
            [p for p in snap.packages if p.status_code < Status.DELIVERED]
            + list(snap.delivered_today)
            + list(snap.ticked_today)
        )

    def _items(self) -> list[TodoItem]:
        items: list[TodoItem] = []
        self._keys = {}
        for p in self._shown():
            done = p.status_code >= PROBABLY_DELIVERED
            bits = [p.carrier, "ticked off" if p.ticked else p.status]
            if p.window and not done:
                bits.append(p.window)
            if p.late and not done:
                bits.append("running late")
            if p.exception:
                bits.append(p.exception)
            if p.tracking_last4:
                bits.append(f"tracking ends {p.tracking_last4}")
            due = dt.date.fromisoformat(p.expected) if p.expected else None
            uid = _uid(p.key)
            self._keys[uid] = p.key
            items.append(
                TodoItem(
                    uid=uid,
                    summary=p.item,
                    status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION,
                    due=due,
                    description=" · ".join(bits),
                )
            )
        return items

    @override
    async def async_update_todo_item(self, item: TodoItem) -> None:
        key = self._keys.get(item.uid or "")
        current = next((i for i in self.todo_items or [] if i.uid == item.uid), None)
        if key is None or current is None:
            raise HomeAssistantError("That package is no longer on the list")
        if item.summary != current.summary:
            raise HomeAssistantError(
                "Package names come from your email; you can only tick them off"
            )
        if item.status != current.status:
            self.coordinator.async_set_received(key, item.status == TodoItemStatus.COMPLETED)
