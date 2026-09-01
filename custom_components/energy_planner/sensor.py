"""Platform for sensor integration.

FAS 1: exposes the Smart Planner shadow-mode result as read-only sensors.
These sensors are purely informational -- nothing here ever writes to
`slot_N_*` entities or Solis. The underlying data is published into
`hass.data[DOMAIN]["smart_shadow_last_outcome"]` by
`planner/smart_planner.py`.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy

from custom_components.energy_planner.const import DOMAIN, SENSOR_ENTITIES

_LOGGER = logging.getLogger(__name__)

_SHADOW_KEY = "smart_shadow_last_outcome"


def _shadow_data(hass) -> dict:
    return hass.data[DOMAIN].get(
        _SHADOW_KEY, {"available": False, "error_code": "not_run_yet"}
    )


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_devices):
    """Set up the sensor platform."""
    _LOGGER.info("Setting up sensor platform")
    sensors = [
        SmartShadowStatusSensor(hass),
        SmartShadowProjectedCostSensor(hass),
        SmartShadowPlannedChargeSensor(hass),
        SmartShadowPlannedDischargeSensor(hass),
        SmartShadowPvForecastSensor(hass),
        SmartShadowLoadForecastSensor(hass),
        SmartShadowGridImportSensor(hass),
        SmartShadowGridExportSensor(hass),
        SmartShadowNextActionSensor(hass),
        SmartShadowPlanSensor(hass),
    ]
    hass.data[DOMAIN][SENSOR_ENTITIES] = sensors
    async_add_devices(sensors)
    for sensor in sensors:
        sensor.update()
    return True


class _SmartShadowBaseSensor(SensorEntity):
    """Shared plumbing for the Smart Planner shadow-mode sensors."""

    _id_suffix = "base"
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass) -> None:
        """Wire up entity id/unique id from `_id_suffix`."""
        self._hass = hass
        self.entity_id = f"sensor.{DOMAIN}_smart_{self._id_suffix}"
        self._attr_unique_id = f"{DOMAIN}_smart_{self._id_suffix}"
        self._attr_available = True

    def update(self) -> None:
        """Refresh from the latest shadow-mode outcome and push to HA."""
        data = _shadow_data(self._hass)
        self._update_from_shadow(data)
        self.schedule_update_ha_state()

    def _update_from_shadow(self, data: dict) -> None:
        raise NotImplementedError


class SmartShadowStatusSensor(_SmartShadowBaseSensor):
    """Whether the last shadow-mode run produced a usable plan."""

    _id_suffix = "status"
    _attr_name = "Smart Planner status"
    _attr_icon = "mdi:brain"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = (
            "available" if data.get("available") else "unavailable"
        )
        self._attr_extra_state_attributes = {
            "error_code": data.get("error_code"),
            "error_message": data.get("error_message"),
            "generated_at": data.get("generated_at"),
            "any_degraded": data.get("any_degraded"),
        }


class SmartShadowProjectedCostSensor(_SmartShadowBaseSensor):
    """Total projected cost (negative = income) for the planned horizon."""

    _id_suffix = "projected_cost"
    _attr_name = "Smart Planner projected cost"
    _attr_native_unit_of_measurement = "SEK"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:cash"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_cost_sek")
        self._attr_extra_state_attributes = {
            "soc_start_kwh": data.get("soc_start_kwh"),
            "generated_at": data.get("generated_at"),
        }


class SmartShadowPlannedChargeSensor(_SmartShadowBaseSensor):
    """Total kWh Smart Planner would charge into the battery this horizon."""

    _id_suffix = "planned_charge"
    _attr_name = "Smart Planner planned charge"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:battery-charging"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_charge_kwh")


class SmartShadowPlannedDischargeSensor(_SmartShadowBaseSensor):
    """Total kWh Smart Planner would discharge (self-use + sell) this horizon."""

    _id_suffix = "planned_discharge"
    _attr_name = "Smart Planner planned discharge"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:battery-arrow-down"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_discharge_kwh")


class SmartShadowPvForecastSensor(_SmartShadowBaseSensor):
    """Total forecasted PV production over the planning horizon."""

    _id_suffix = "pv_forecast"
    _attr_name = "Smart Planner PV forecast"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:solar-power"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_pv_forecast_kwh")


class SmartShadowLoadForecastSensor(_SmartShadowBaseSensor):
    """Total forecasted house consumption over the planning horizon."""

    _id_suffix = "load_forecast"
    _attr_name = "Smart Planner load forecast"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:home-lightning-bolt"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_load_forecast_kwh")


class SmartShadowGridImportSensor(_SmartShadowBaseSensor):
    """Total planned grid import over the planning horizon."""

    _id_suffix = "grid_import"
    _attr_name = "Smart Planner planned grid import"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:transmission-tower-import"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_grid_import_kwh")


class SmartShadowGridExportSensor(_SmartShadowBaseSensor):
    """Total planned grid export over the planning horizon."""

    _id_suffix = "grid_export"
    _attr_name = "Smart Planner planned grid export"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:transmission-tower-export"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = data.get("total_grid_export_kwh")


class SmartShadowNextActionSensor(_SmartShadowBaseSensor):
    """The first upcoming slot's planned state and reason, for a quick glance."""

    _id_suffix = "next_action"
    _attr_name = "Smart Planner next action"
    _attr_icon = "mdi:calendar-clock"

    def _update_from_shadow(self, data: dict) -> None:
        slots = data.get("slots") or []
        if not slots:
            self._attr_native_value = "unavailable"
            self._attr_extra_state_attributes = {}
            return
        next_slot = slots[0]
        self._attr_native_value = next_slot["state"]
        self._attr_extra_state_attributes = {
            "start": next_slot["start"],
            "end": next_slot["end"],
            "target_soc_kwh": next_slot["target_soc_kwh"],
            "reason": next_slot["reason"],
        }


class SmartShadowPlanSensor(_SmartShadowBaseSensor):
    """Full plan as a JSON-serializable attribute, for building a Lovelace card."""

    _id_suffix = "plan"
    _attr_name = "Smart Planner plan"
    _attr_icon = "mdi:calendar-text"

    def _update_from_shadow(self, data: dict) -> None:
        self._attr_native_value = (
            "available" if data.get("available") else "unavailable"
        )
        self._attr_extra_state_attributes = {
            "generated_at": data.get("generated_at"),
            "slots": data.get("slots", []),
        }
