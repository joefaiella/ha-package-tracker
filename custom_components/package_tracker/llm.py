"""Voice assistant tool: lets Assist answer questions about expected packages and mail."""

from typing import Any, override

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.components.llm import LLMTools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.llm import (
    LLM_API_ASSIST,
    LLMContext,
    Tool,
    ToolAnnotations,
    ToolInput,
    ToolResult,
)
from homeassistant.util import dt as dt_util
import probatio

from .const import DOMAIN
from .coordinator import PackageTrackerConfigEntry
from .registry import PROBABLY_DELIVERED

WHEN = ["today", "coming", "delivered", "all"]


class GetPackagesTool(Tool):
    name = f"{DOMAIN}__get_packages"
    title = "Get packages"
    description = (
        "Packages and deliveries the user is expecting, remembered from their shipping emails "
        "(Amazon, USPS, UPS, FedEx and stores), plus today's USPS mail count. Use for questions "
        "like 'what packages are coming today?', 'did my headphones ship?', 'when is the FedEx "
        "package coming?', 'what was delivered today?', 'do I have mail today?'. 'expected' is the "
        "carrier's estimate (local date); 'window' is the delivery time window when known; 'item' "
        "is what Amazon or the sender calls it. Speak naturally and briefly; don't read tracking "
        "numbers unless asked."
    )
    annotations = ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=False
    )
    integration = DOMAIN
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "when",
                default="coming",
                description=(
                    "today = arriving or delivered today; coming = everything not yet delivered "
                    "(including ordered but not shipped); delivered = recently delivered; "
                    "all = everything"
                ),
            ): probatio.In(WHEN),
        }
    )

    def __init__(self, entries: list[PackageTrackerConfigEntry]) -> None:
        self._entries = entries

    @override
    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> ToolResult:
        when = self.parameters(tool_input.tool_args)["when"]
        now = dt_util.now()
        packages: list[dict[str, Any]] = []
        mail: int | None = None
        last_check = None
        for entry in self._entries:
            coordinator = entry.runtime_data
            snap = coordinator.snapshot(now)  # only the packages the address filter lets through
            chosen = {
                "today": snap.arriving_today + snap.delivered_today,
                "coming": snap.active,
                "delivered": tuple(p for p in snap.packages if p.status_code >= PROBABLY_DELIVERED),
                "all": snap.packages,
            }[when]
            packages.extend(p.as_public_dict() for p in chosen)
            if snap.mail_pieces_today is not None:
                mail = (mail or 0) + snap.mail_pieces_today
            if coordinator.last_check:
                last_check = coordinator.last_check.isoformat(timespec="minutes")
        return ToolResult(
            data={
                "now": now.isoformat(timespec="minutes"),
                "when": when,
                "count": len(packages),
                "packages": packages,
                "mail_pieces_today": mail,
                "emails_last_checked": last_check,
            }
        )


@callback
def async_get_tools(hass: HomeAssistant, llm_context: LLMContext, api_id: str) -> LLMTools | None:
    if api_id != LLM_API_ASSIST:
        return None
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        return None
    ent_reg = er.async_get(hass)
    if not any(
        async_should_expose(hass, llm_context.assistant, reg.entity_id)
        for entry in entries
        for reg in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    ):
        return None  # only offered when a Package tracker entity is exposed to this assistant
    return LLMTools(
        tools=[GetPackagesTool(entries)],
        prompt=(
            "For questions about packages, deliveries, orders or today's mail, "
            f"call {DOMAIN}__get_packages."
        ),
    )
