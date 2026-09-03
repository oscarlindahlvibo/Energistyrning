import logging
import datetime as dt
from zoneinfo import ZoneInfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    callback,
    Event,
    EventStateChangedData,
)
from homeassistant.const import Platform
from homeassistant.exceptions import ServiceValidationError

# from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
    async_track_time_interval,
)

from .const import (
    DOMAIN,
    DATE_TIME_ENTITIES,
    NUMBER_ENTITIES,
    SWITCH_ENTITIES,
    SENSOR_ENTITIES,
    SELECT_ENTITIES,
    TEXT_ENTITIES,
    TIME_ENTITIES,
)
from .planner import (
    basic_planner,
    dynamic_planner,
    cheapest_hours_planner,
    add_manual_slots,
    clear_passed_slots,
    update_entities,
    price_peak_planner,
    async_run_shadow_planner,
    async_setup_smart_shadow,
    SMART_PLANNER_BATTERY_SOC_ENTITY,
    SMART_PLANNER_PV_FORECAST_ENTITIES,
)
from .store import async_save_to_store, async_load_from_store
from .utils import tz_diff

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    Platform.DATETIME,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.TIME,
]


async def async_setup_data_structure(hass: HomeAssistant):
    """Set up the data structure."""

    async def save():
        for data_store in ["values", "config", "manual_slots"]:
            await async_save_to_store(hass, data_store, hass.data[DOMAIN][data_store])

    hass.data[DOMAIN] = {
        "values": {},
        "config": {},
        "manual_slots": [],
        DATE_TIME_ENTITIES: {},
        TIME_ENTITIES: {},
        NUMBER_ENTITIES: {},
        SWITCH_ENTITIES: {},
        SENSOR_ENTITIES: {},
        SELECT_ENTITIES: {},
        TEXT_ENTITIES: {},
        "save": save,
        "listeners": [],
    }
    hass.data[DOMAIN]["values"] = await async_load_from_store(hass, "values")
    hass.data[DOMAIN]["config"] = await async_load_from_store(hass, "config")
    hass.data[DOMAIN]["manual_slots"] = (
        await async_load_from_store(hass, "manual_slots") or []
    )


async def async_setup(hass: HomeAssistant, config):
    """Set up the Energy Planner component."""
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)
    await async_setup_smart_shadow(hass)

    @callback
    async def get_price_service(call: ServiceCall) -> None:
        """Service to get the current price."""
        pass
        # client = async_get_clientsession(hass)

    @callback
    async def add_slot_service(call: ServiceCall) -> None:
        """Service to add a slot."""
        try:
            start_datetime = dt.datetime.fromisoformat(call.data.get("start"))
            start_datetime = start_datetime.replace(tzinfo=ZoneInfo("Europe/Stockholm"))
            end_datetime = dt.datetime.fromisoformat(call.data.get("end"))
            end_datetime = end_datetime.replace(tzinfo=ZoneInfo("Europe/Stockholm"))
            state = call.data.get("state")
            soc = call.data.get("soc")
            if start_datetime > end_datetime:
                raise ValueError("Start must be before end")
            if state not in [
                "charge",
                "discharge",
                "sell",
                "sell-excess",
                "discard-excess",
                "pause",
                "off",
            ]:
                raise ValueError("Invalid state")
        except Exception as e:
            _LOGGER.error("Error adding slot: %s", e)
            raise ServiceValidationError("Invalid data") from e
        hass.data[DOMAIN]["manual_slots"].append(
            {"start": start_datetime, "end": end_datetime, "state": state, "soc": soc}
        )
        await add_manual_slots(hass)
        await update_entities(hass)
        await hass.data[DOMAIN]["save"]()
        _LOGGER.info("Received data: %s", call.data)

    @callback
    async def run_planner(*args, **kwargs) -> None:
        _LOGGER.info("Run planner args, %s", args)
        _LOGGER.info("Run planner kwargs, %s", kwargs)
        planner_state = hass.data[DOMAIN]["config"].get("planner_state", "basic")
        if planner_state == "off":
            _LOGGER.info("Planner is off")
            return
        if planner_state == "basic":
            _LOGGER.info("Running basic planner")
            await basic_planner(hass)
            return
        if planner_state == "cheapest hours":
            _LOGGER.info("Running cheapest hours planner")
            await cheapest_hours_planner(hass)
            return
        if planner_state == "price peak":
            _LOGGER.info("Running price peak planner")
            await price_peak_planner(hass)
            return
        if planner_state == "dynamic":
            _LOGGER.info("Running dynamic planner")
            await dynamic_planner(hass)
            return
        raise ValueError("Invalid planner state")

    @callback
    async def run_planner_service(call: ServiceCall) -> None:
        """Service to run the planner."""
        _LOGGER.info("Running planner: %s", config)
        _LOGGER.info("Received planning data: %s", call.data)
        await run_planner()

    @callback
    async def clear_manual_slots_service(call: ServiceCall) -> None:
        """Service to run the planner."""
        _LOGGER.info("Running planner: %s", config)
        _LOGGER.info("Received planning data: %s", call.data)
        hass.data[DOMAIN]["manual_slots"] = []
        await hass.data[DOMAIN]["save"]()

    # Register our service with Home Assistant.
    hass.services.async_register(DOMAIN, "add_slot", add_slot_service)
    hass.services.async_register(DOMAIN, "run_planner", run_planner_service)
    hass.services.async_register(
        DOMAIN, "clear_manual_slots", clear_manual_slots_service
    )

    update_schedule_timer = async_track_utc_time_change(
        hass,
        run_planner,
        hour=(15 + int(tz_diff("Europe/Stockholm", "UTC"))) % 24,
        minute=31,
        second=15,
    )
    hass.data[DOMAIN]["listeners"].append(update_schedule_timer)

    @callback
    async def check_schedule(*args, **kwargs):
        _LOGGER.info("Checking schedule args, %s", args)
        _LOGGER.info("Checking schedule kwargs, %s", kwargs)
        await clear_passed_slots(hass)

    check_schedule_timer = async_track_time_interval(
        hass,
        check_schedule,
        dt.timedelta(minutes=1),
    )
    hass.data[DOMAIN]["listeners"].append(check_schedule_timer)

    # --- Fas 1: Smart Planner shadow mode ---
    # Runs independently of `planner_state`/`run_planner` above: it is
    # gated on its own switch (smart_shadow_enabled) inside
    # async_run_shadow_planner, never on which planner is actually
    # driving the battery. It only ever writes to the smart_* shadow
    # sensors, never to slot_N_* or Solis.
    @callback
    async def run_shadow_planner(*args, **kwargs) -> None:
        try:
            await async_run_shadow_planner(hass)
        except Exception:
            _LOGGER.exception("Smart Planner shadow run failed")

    shadow_timer = async_track_time_interval(
        hass,
        run_shadow_planner,
        dt.timedelta(minutes=15),
    )
    hass.data[DOMAIN]["listeners"].append(shadow_timer)

    # Re-run promptly on a big/discrete change, on top of the 15-minute
    # cadence above (continuous power sensors like actual PV/house load
    # are deliberately left to the interval timer -- they change too
    # often for a per-event re-run to be useful).
    watched_entities = [
        SMART_PLANNER_BATTERY_SOC_ENTITY,
        *SMART_PLANNER_PV_FORECAST_ENTITIES,
    ]
    shadow_state_listener = async_track_state_change_event(
        hass, watched_entities, run_shadow_planner
    )
    hass.data[DOMAIN]["listeners"].append(shadow_state_listener)

    # Return boolean to indicate that initialization was successful.
    return True


async def state_automation_listener(event: Event[EventStateChangedData]):
    """Handle state change event."""
    _LOGGER.debug(f"state_automation_listener: {event.data}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up Modbus from a config entry."""
    # Set up the platforms associated with this integration
    if DOMAIN not in hass.data:
        await async_setup_data_structure(hass)
    hass.data[DOMAIN]["config"]["entry_id"] = entry.entry_id
    hass.data[DOMAIN]["config"]["nordpool_entity_id"] = entry.data["nordpool_entity_id"]
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    )

    @callback
    async def run_shadow_planner_on_price_change(*args, **kwargs) -> None:
        try:
            await async_run_shadow_planner(hass)
        except Exception:
            _LOGGER.exception("Smart Planner shadow run failed")

    nordpool_listener = async_track_state_change_event(
        hass, [entry.data["nordpool_entity_id"]], run_shadow_planner_on_price_change
    )
    hass.data[DOMAIN]["listeners"].append(nordpool_listener)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a Modbus config entry."""
    _LOGGER.debug("init async_unload_entry")
    # Unload platforms associated with this integration
    unload_ok = await hass.config_entries.async_forward_entry_unload(entry, DOMAIN)
    if unload_ok:
        for listener in hass.data[DOMAIN]["listeners"]:
            listener()
    return unload_ok
