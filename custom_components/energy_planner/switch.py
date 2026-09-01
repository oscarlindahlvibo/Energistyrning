import logging
from email.policy import default

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry

from custom_components.energy_planner.const import DOMAIN, SWITCH_ENTITIES

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the switch platform."""
    _LOGGER.info("Setting up datetime platform")
    switches = [
        EnergyPlannerSwitchEntity(
            hass,
            {
                "id": f"slot_{i}_active",
                default: False,
                "name": f"Slot {i} active",
                "enabled": True,
            },
        )
        for i in range(1, 50)
    ]
    switches.append(
        EnergyPlannerSwitchEntity(
            hass,
            {
                "id": "smart_shadow_enabled",
                "default": False,
                "name": "Smart Planner shadow mode",
                "icon": "mdi:brain",
                "enabled": True,
                # Runs independently of `planner_state` -- toggling this
                # never affects which planner actually controls the
                # battery, and Smart Planner itself never writes to
                # slot_N_* or Solis in Fas 1.
            },
        )
    )

    hass.data[DOMAIN][SWITCH_ENTITIES] = switches

    for switch in switches:
        if hass.data[DOMAIN][switch.data_store].get(switch.id) is None:
            hass.data[DOMAIN][switch.data_store][switch.id] = switch.is_on
    async_add_devices(switches)
    for switch in switches:
        switch.update()
    # Return boolean to indicate that initialization was successful
    return True


class EnergyPlannerSwitchEntity(SwitchEntity):
    """Representation of a Switch entity."""

    def __init__(self, hass, entity_definition):
        """Initialize the switch entity."""
        self._hass = hass
        self.id = entity_definition["id"]

        self.entity_id = f"switch.{DOMAIN}_{self.id}"
        self._attr_unique_id = "{}_{}".format(DOMAIN, self.id)
        self._attr_has_entity_name = True
        self._attr_name = entity_definition["name"]
        self.data_store = entity_definition.get("data_store", "values")
        self._attr_native_value = entity_definition.get("default", None)
        self._attr_assumed_state = entity_definition.get("assumed", False)
        self._attr_available = True
        self.is_added_to_hass = False
        self._attr_device_class = entity_definition.get("device_class", None)
        self._attr_icon = entity_definition.get("icon", None)
        self._attr_native_step = entity_definition.get("step", 1.0)
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
        value = self._hass.data[DOMAIN][self.data_store].get(self.id, None)
        self._attr_native_value = value
        self.schedule_update_ha_state()

    async def async_turn_on(self, **kwargs):
        """Turn the entity on."""
        self._attr_native_value = True
        self._hass.data[DOMAIN][self.data_store][self.id] = True
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()

    async def async_turn_off(self, **kwargs):
        """Turn the entity off."""
        self._attr_native_value = False
        self._hass.data[DOMAIN][self.data_store][self.id] = False
        await self._hass.data[DOMAIN]["save"]()
        self.schedule_update_ha_state()

    @property
    def is_on(self):
        """Return true if the entity is on."""
        return self._attr_native_value
