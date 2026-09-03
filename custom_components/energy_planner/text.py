import logging

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry

from custom_components.energy_planner.const import DOMAIN, TEXT_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the text platform.

    Currently only used for Smart Planner entity-id inputs that this
    installation's real Home Assistant instance must supply, since the
    integration cannot guess a real sensor/weather entity name for
    something it has never seen (see docs/smart-planner.md, temperature
    forecasting gap). Empty string (unconfigured) means the feature it
    backs falls back gracefully rather than failing the whole plan.
    """
    _LOGGER.info("Setting up text platform")
    texts = [
        EnergyPlannerTextEntity(
            hass,
            {
                "id": "outdoor_temperature_entity_id",
                "name": "Smart Planner outdoor temperature sensor entity id",
                "default": "",
                "enabled": True,
                "data_store": "config",
            },
        ),
        EnergyPlannerTextEntity(
            hass,
            {
                "id": "weather_forecast_entity_id",
                "name": "Smart Planner weather forecast entity id",
                "default": "",
                "enabled": True,
                "data_store": "config",
            },
        ),
    ]

    hass.data[DOMAIN][TEXT_ENTITIES] = texts
    for text in texts:
        if hass.data[DOMAIN][text.data_store].get(text.id) is None:
            hass.data[DOMAIN][text.data_store][text.id] = text.native_value
    async_add_devices(texts)
    for text in texts:
        text.update()

    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerTextEntity(TextEntity):
    """Representation of a Text entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the Text entity."""
        self._hass = hass
        self.id = entity_definition["id"]
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self.entity_id = f"text.{DOMAIN}_{self.id}"
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self.data_store = entity_definition.get("data_store", "values")
        self._attr_native_value = entity_definition.get("default", "")
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_should_poll = False
        self._attr_entity_registry_enabled_default = entity_definition.get(
            "enabled", False
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        self.is_added_to_hass = True

    def update(self):
        """Update data."""
        self._attr_available = True
        value = self._hass.data[DOMAIN][self.data_store].get(self.id, "")
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_set_value(self, value: str) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._hass.data[DOMAIN][self.data_store][self.id] = value
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()
