"""Feed the room's temperature to TRVs that accept an external reading.

A TRV regulates its valve on its own sensor, which sits on the radiator and
usually reads warmer than the room. It can then close while ZEAL still sees
the room below target. Some TRVs accept a room reading instead: the Sonoff
TRVZB family exposes, through Zigbee2MQTT, an ``external_temperature_input``
number and a ``temperature_sensor_select`` select on the same device.

For each such TRV, ZEAL writes the room temperature it already uses for demand
and selects the remote input. Without a usable room reading it selects the
built-in sensor again, and on unload it restores the built-in sensor so a TRV
is never left regulating on a frozen value. TRVs without these entities are
left alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import TRV_EXTERNAL_TEMPERATURE_REFRESH_SECONDS

_LOGGER = logging.getLogger(__name__)

EXTERNAL_INPUT_KEY = "external_temperature_input"
SENSOR_SELECT_KEY = "temperature_sensor_select"
SELECT_LOCAL = "local_temperature"
SELECT_REMOTE = "remote_temperature"
# Reported by the TRV when it has stopped trusting the remote input. It
# returns to the remote input by itself once a fresh value arrives.
SELECT_REMOTE_OFFLINE = "remote_source_offline"

_UNAVAILABLE = (None, "unavailable", "unknown")


@dataclass(frozen=True)
class ExternalTemperatureControls:
    """The companion entities that let one TRV use a room reading."""

    number_entity_id: str
    select_entity_id: str


def _matches(entry: er.RegistryEntry, domain: str, key: str) -> bool:
    return entry.domain == domain and (
        key in (entry.unique_id or "") or entry.entity_id.endswith(f"_{key}")
    )


def find_external_temperature_controls(
    hass: HomeAssistant, trv_entity_id: str
) -> ExternalTemperatureControls | None:
    """Return the TRV's external-input entities, or None if it has none."""
    registry = er.async_get(hass)
    trv_entry = registry.async_get(trv_entity_id)
    if trv_entry is None or trv_entry.device_id is None:
        return None
    number_id = select_id = None
    for entry in er.async_entries_for_device(registry, trv_entry.device_id):
        if entry.disabled_by is not None:
            continue
        if _matches(entry, "number", EXTERNAL_INPUT_KEY):
            number_id = entry.entity_id
        elif _matches(entry, "select", SENSOR_SELECT_KEY):
            select_id = entry.entity_id
    if number_id is None or select_id is None:
        return None
    return ExternalTemperatureControls(number_id, select_id)


class TrvExternalTemperature:
    """Keep supported TRVs regulating on the room's own temperature."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        # TRV entity_id -> (last value written, when). Lets an unchanged room
        # temperature be resent only at the refresh interval.
        self._last_written: dict[str, tuple[float, datetime]] = {}
        # TRVs ZEAL has switched to the remote input, restored on unload.
        self._remote_trvs: dict[str, ExternalTemperatureControls] = {}

    async def async_sync_room(
        self, trv_entity_ids: list[str], room_temperature: float | None
    ) -> None:
        """Point each supported TRV at the room reading, or back at its own."""
        for trv in trv_entity_ids:
            controls = find_external_temperature_controls(self.hass, trv)
            if controls is None:
                continue
            if room_temperature is None:
                await self._async_select_local(trv, controls)
                continue
            if await self._async_write_temperature(trv, controls, room_temperature):
                await self._async_select(controls, SELECT_REMOTE)
                self._remote_trvs[trv] = controls

    async def async_restore_local(self) -> None:
        """Return every TRV ZEAL switched to the remote input to its own sensor."""
        for trv, controls in list(self._remote_trvs.items()):
            await self._async_select_local(trv, controls)

    async def _async_select_local(
        self, trv: str, controls: ExternalTemperatureControls
    ) -> None:
        self._last_written.pop(trv, None)
        self._remote_trvs.pop(trv, None)
        await self._async_select(controls, SELECT_LOCAL)

    async def _async_write_temperature(
        self, trv: str, controls: ExternalTemperatureControls, temperature: float
    ) -> bool:
        """Write the reading when it changed or is due; False if not accepted."""
        state = self.hass.states.get(controls.number_entity_id)
        if state is None or state.state in _UNAVAILABLE:
            return False
        try:
            value = round(
                min(
                    float(state.attributes.get("max", 99.9)),
                    max(float(state.attributes.get("min", 0.0)), temperature),
                ),
                1,
            )
        except (TypeError, ValueError):
            return False

        now = dt_util.utcnow()
        last = self._last_written.get(trv)
        if (
            last is not None
            and abs(last[0] - value) < 0.05
            and now - last[1] < timedelta(seconds=TRV_EXTERNAL_TEMPERATURE_REFRESH_SECONDS)
        ):
            return True
        try:
            await self.hass.services.async_call(
                "number",
                "set_value",
                {"entity_id": controls.number_entity_id, "value": value},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not send room temperature %s°C to %s: %s",
                value,
                controls.number_entity_id,
                err,
            )
            return False
        self._last_written[trv] = (value, now)
        return True

    async def _async_select(
        self, controls: ExternalTemperatureControls, option: str
    ) -> None:
        state = self.hass.states.get(controls.select_entity_id)
        if state is None or state.state in _UNAVAILABLE:
            return
        current = state.state
        if current == option or (
            option == SELECT_REMOTE and current == SELECT_REMOTE_OFFLINE
        ):
            return
        if option not in (state.attributes.get("options") or []):
            _LOGGER.warning(
                "%s does not offer the %s option; leaving it on %s",
                controls.select_entity_id,
                option,
                current,
            )
            return
        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": controls.select_entity_id, "option": option},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning(
                "Could not set %s to %s: %s", controls.select_entity_id, option, err
            )
            return
        _LOGGER.info("%s now uses %s", controls.select_entity_id, option)
