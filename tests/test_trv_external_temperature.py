"""TRVs that accept an external room reading regulate on ZEAL's room sensor."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeal.const import (
    CONF_ZONES,
    DOMAIN,
    ROOM_ACTIVE,
    ROOM_ID,
    ROOM_NAME,
    ROOM_OPENING_SENSORS,
    ROOM_SENSORS,
    ROOM_TRVS,
    TRV_EXTERNAL_TEMPERATURE_REFRESH_SECONDS,
    ZONE_ID,
    ZONE_NAME,
    ZONE_ROOMS,
    ZONE_SWITCH,
)
from custom_components.zeal.coordinator import ZealCoordinator
from custom_components.zeal.trv_external_temperature import (
    SELECT_LOCAL,
    SELECT_REMOTE,
    SELECT_REMOTE_OFFLINE,
    TrvExternalTemperature,
    find_external_temperature_controls,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

TRV = "climate.lounge_trv"
NUMBER = "number.lounge_trv_external_temperature_input"
SELECT = "select.lounge_trv_temperature_sensor_select"
OPTIONS = [SELECT_LOCAL, SELECT_REMOTE, SELECT_REMOTE_OFFLINE]


def _register_trv(hass, *, with_controls: bool = True) -> None:
    """Register a TRV the way Zigbee2MQTT discovery does, on one device."""
    source = MockConfigEntry(domain="mqtt")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, identifiers={("mqtt", "0x01")}
    )
    registry = er.async_get(hass)
    entities = [("climate", "0x01_climate_zigbee2mqtt", "lounge_trv")]
    if with_controls:
        entities += [
            (
                "number",
                "0x01_external_temperature_input_zigbee2mqtt",
                "lounge_trv_external_temperature_input",
            ),
            (
                "select",
                "0x01_temperature_sensor_select_zigbee2mqtt",
                "lounge_trv_temperature_sensor_select",
            ),
        ]
    for domain, unique_id, object_id in entities:
        registry.async_get_or_create(
            domain,
            "mqtt",
            unique_id,
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=source,
        )
    hass.states.async_set(TRV, "heat", {"temperature": 20.0})
    if with_controls:
        hass.states.async_set(NUMBER, "0.0", {"min": 0.0, "max": 99.9})
        hass.states.async_set(SELECT, SELECT_LOCAL, {"options": OPTIONS})


@pytest.fixture
def calls(hass) -> list[tuple[str, str, object]]:
    """Record number/select writes in order and reflect them in state."""
    recorded: list[tuple[str, str, object]] = []

    async def set_value(call):
        recorded.append(("number", call.data["entity_id"], call.data["value"]))
        state = hass.states.get(call.data["entity_id"])
        hass.states.async_set(
            call.data["entity_id"], str(call.data["value"]), state.attributes
        )

    async def select_option(call):
        recorded.append(("select", call.data["entity_id"], call.data["option"]))
        state = hass.states.get(call.data["entity_id"])
        hass.states.async_set(
            call.data["entity_id"], call.data["option"], state.attributes
        )

    hass.services.async_register("number", "set_value", set_value)
    hass.services.async_register("select", "select_option", select_option)
    return recorded


async def test_finds_companion_entities_on_the_trv_device(hass):
    _register_trv(hass)
    controls = find_external_temperature_controls(hass, TRV)
    assert controls is not None
    assert controls.number_entity_id == NUMBER
    assert controls.select_entity_id == SELECT


async def test_trv_without_external_input_is_left_alone(hass, calls):
    _register_trv(hass, with_controls=False)
    assert find_external_temperature_controls(hass, TRV) is None

    await TrvExternalTemperature(hass).async_sync_room([TRV], 18.4)

    assert calls == []


async def test_room_reading_is_written_before_selecting_remote_input(hass, calls):
    _register_trv(hass)

    await TrvExternalTemperature(hass).async_sync_room([TRV], 18.44)

    assert calls == [
        ("number", NUMBER, 18.4),
        ("select", SELECT, SELECT_REMOTE),
    ]


async def test_unchanged_reading_is_resent_only_at_the_refresh_interval(
    hass, calls, freezer
):
    _register_trv(hass)
    sync = TrvExternalTemperature(hass)
    await sync.async_sync_room([TRV], 18.4)
    calls.clear()

    await sync.async_sync_room([TRV], 18.4)
    assert calls == []

    await sync.async_sync_room([TRV], 18.6)
    assert calls == [("number", NUMBER, 18.6)]
    calls.clear()

    freezer.tick(timedelta(seconds=TRV_EXTERNAL_TEMPERATURE_REFRESH_SECONDS + 1))
    await sync.async_sync_room([TRV], 18.6)
    assert calls == [("number", NUMBER, 18.6)]


async def test_no_usable_room_reading_returns_trv_to_its_own_sensor(hass, calls):
    _register_trv(hass)
    sync = TrvExternalTemperature(hass)
    await sync.async_sync_room([TRV], 18.4)
    calls.clear()

    await sync.async_sync_room([TRV], None)

    assert calls == [("select", SELECT, SELECT_LOCAL)]


async def test_offline_remote_source_recovers_from_a_fresh_reading(hass, calls):
    _register_trv(hass)
    hass.states.async_set(SELECT, SELECT_REMOTE_OFFLINE, {"options": OPTIONS})

    await TrvExternalTemperature(hass).async_sync_room([TRV], 19.0)

    assert calls == [("number", NUMBER, 19.0)]


async def test_unavailable_input_does_not_select_remote(hass, calls):
    _register_trv(hass)
    hass.states.async_set(NUMBER, "unavailable", {"min": 0.0, "max": 99.9})

    await TrvExternalTemperature(hass).async_sync_room([TRV], 19.0)

    assert calls == []


async def test_restore_local_returns_switched_trvs_to_their_own_sensor(hass, calls):
    _register_trv(hass)
    sync = TrvExternalTemperature(hass)
    await sync.async_sync_room([TRV], 18.4)
    calls.clear()

    await sync.async_restore_local()

    assert calls == [("select", SELECT, SELECT_LOCAL)]


async def test_coordinator_feeds_active_rooms_only(hass, calls):
    _register_trv(hass)
    hass.states.async_set("sensor.lounge", "18.5")

    def room(room_id: str, active: bool) -> dict:
        return {
            ROOM_ID: room_id,
            ROOM_NAME: room_id,
            ROOM_TRVS: [TRV],
            ROOM_SENSORS: ["sensor.lounge"],
            ROOM_OPENING_SENSORS: [],
            ROOM_ACTIVE: active,
        }

    zone = {
        ZONE_ID: "house",
        ZONE_NAME: "House",
        ZONE_SWITCH: "switch.boiler",
        ZONE_ROOMS: [room("inactive", False)],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_ZONES: [zone]})
    entry.add_to_hass(hass)
    coordinator = ZealCoordinator(hass, entry, Store(hass, 1, "zeal_test"))
    coordinator.zones = [zone]

    await coordinator._async_sync_trv_external_temperatures(zone)
    assert calls == []

    zone[ZONE_ROOMS] = [room("lounge", True)]
    await coordinator._async_sync_trv_external_temperatures(zone)
    assert calls == [
        ("number", NUMBER, 18.5),
        ("select", SELECT, SELECT_REMOTE),
    ]


async def test_external_temperature_failure_never_blocks_zone_control(hass):
    zone = {
        ZONE_ID: "house",
        ZONE_NAME: "House",
        ZONE_SWITCH: "switch.boiler",
        ZONE_ROOMS: [
            {
                ROOM_ID: "lounge",
                ROOM_NAME: "Lounge",
                ROOM_TRVS: [TRV],
                ROOM_SENSORS: [],
                ROOM_OPENING_SENSORS: [],
                ROOM_ACTIVE: True,
            }
        ],
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={CONF_ZONES: [zone]})
    entry.add_to_hass(hass)
    coordinator = ZealCoordinator(hass, entry, Store(hass, 1, "zeal_test"))

    async def broken(*_args):
        raise RuntimeError("radio exploded")

    coordinator.trv_external_temperature.async_sync_room = broken

    await coordinator._async_sync_trv_external_temperatures(zone)
