"""Support for Met.no weather service."""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, override

from homeassistant.components.weather import (
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_TIME,
    ATTR_WEATHER_CLOUD_COVERAGE,
    ATTR_WEATHER_DEW_POINT,
    ATTR_WEATHER_HUMIDITY,
    ATTR_WEATHER_PRESSURE,
    ATTR_WEATHER_TEMPERATURE,
    ATTR_WEATHER_UV_INDEX,
    ATTR_WEATHER_WIND_BEARING,
    ATTR_WEATHER_WIND_GUST_SPEED,
    ATTR_WEATHER_WIND_SPEED,
    DOMAIN as WEATHER_DOMAIN,
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_NAME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, sun
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util.unit_system import METRIC_SYSTEM

from .const import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_SUNNY,
    ATTR_MAP,
    CONDITIONS_MAP,
    CONF_HUMIDITY_ENTITY,
    CONF_PRESSURE_ENTITY,
    CONF_TEMPERATURE_ENTITY,
    CONF_TRACK_HOME,
    DOMAIN,
    FORECAST_MAP,
)
from .coordinator import MetDataUpdateCoordinator, MetWeatherConfigEntry

PARALLEL_UPDATES = 0

DEFAULT_NAME = "Met.no"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MetWeatherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a weather entity from a config_entry."""
    coordinator = config_entry.runtime_data
    entity_registry = er.async_get(hass)

    name: str | None
    is_metric = hass.config.units is METRIC_SYSTEM
    if config_entry.data.get(CONF_TRACK_HOME, False):
        name = hass.config.location_name
    else:
        name = config_entry.data.get(CONF_NAME, DEFAULT_NAME)
        if TYPE_CHECKING:
            assert isinstance(name, str)

    entities = [MetWeather(coordinator, config_entry, name, is_metric)]

    # Remove hourly entity from legacy config entries
    if hourly_entity_id := entity_registry.async_get_entity_id(
        WEATHER_DOMAIN,
        DOMAIN,
        _calculate_unique_id(config_entry.data, True),
    ):
        entity_registry.async_remove(hourly_entity_id)

    async_add_entities(entities)


def _calculate_unique_id(config: Mapping[str, Any], hourly: bool) -> str:
    """Calculate unique ID."""
    name_appendix = ""
    if hourly:
        name_appendix = "-hourly"
    if config.get(CONF_TRACK_HOME):
        return f"home{name_appendix}"

    return f"{config[CONF_LATITUDE]}-{config[CONF_LONGITUDE]}{name_appendix}"


def format_condition(condition: str) -> str:
    """Return condition from dict CONDITIONS_MAP."""
    for key, value in CONDITIONS_MAP.items():
        if condition in value:
            return key
    return condition


class MetWeather(SingleCoordinatorWeatherEntity[MetDataUpdateCoordinator]):
    """Implementation of a Met.no weather condition."""

    _attr_attribution = (
        "Weather forecast from met.no, delivered by the Norwegian "
        "Meteorological Institute."
    )
    _attr_has_entity_name = True
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.KILOMETERS_PER_HOUR
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(
        self,
        coordinator: MetDataUpdateCoordinator,
        config_entry: MetWeatherConfigEntry,
        name: str,
        is_metric: bool,
    ) -> None:
        """Initialise the platform with a data instance and site."""
        super().__init__(coordinator)
        self._attr_unique_id = _calculate_unique_id(config_entry.data, False)
        self._config = config_entry.data
        self._is_metric = is_metric
        self._attr_device_info = DeviceInfo(
            name="Forecast",
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, config_entry.entry_id)},
            manufacturer="Met.no",
            model="Forecast",
            configuration_url="https://www.met.no/en",
        )
        self._attr_track_home = self._config.get(CONF_TRACK_HOME, False)
        self._attr_name = name
        self._temperature_entity_id: str | None = self._config.get(
            CONF_TEMPERATURE_ENTITY
        )
        self._humidity_entity_id: str | None = self._config.get(CONF_HUMIDITY_ENTITY)
        self._pressure_entity_id: str | None = self._config.get(CONF_PRESSURE_ENTITY)

    async def async_added_to_hass(self) -> None:
        """Register callbacks when entity is added, so we react to sensor changes."""
        await super().async_added_to_hass()

        tracked_entities = [
            entity_id
            for entity_id in (
                self._temperature_entity_id,
                self._humidity_entity_id,
                self._pressure_entity_id,
            )
            if entity_id is not None
        ]
        if not tracked_entities:
            return

        @callback
        def _async_sensor_changed(_event: Event[EventStateChangedData]) -> None:
            """Handle a change of the custom temperature/humidity sensor."""
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass, tracked_entities, _async_sensor_changed
            )
        )

    @property
    @override
    def available(self) -> bool:
        """Keep the entity available on temporary API/network failures.

        The last successfully fetched data is kept and displayed until it is
        considered too old (see MAX_DATA_AGE in coordinator.py).
        """
        return self.coordinator.has_fresh_enough_data

    def _get_sensor_value(self, entity_id: str | None) -> float | None:
        """Return the numeric state of a configured override sensor, if valid."""
        if entity_id is None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    @override
    def condition(self) -> str | None:
        """Return the current condition."""
        condition = self.coordinator.data.current_weather_data.get("condition")
        if condition is None:
            return None

        if condition == ATTR_CONDITION_SUNNY and not sun.is_up(self.hass):
            condition = ATTR_CONDITION_CLEAR_NIGHT

        return format_condition(condition)

    @property
    @override
    def native_temperature(self) -> float | None:
        """Return the temperature.

        Uses the configured custom temperature sensor if set and available,
        otherwise falls back to the Met.no forecast value.
        """
        if (value := self._get_sensor_value(self._temperature_entity_id)) is not None:
            return value
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_TEMPERATURE]
        )

    @property
    @override
    def native_pressure(self) -> float | None:
        """Return the pressure.

        Uses the configured custom pressure sensor if set and available,
        otherwise falls back to the Met.no forecast value.
        """
        if (value := self._get_sensor_value(self._pressure_entity_id)) is not None:
            return value
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_PRESSURE]
        )

    @property
    @override
    def humidity(self) -> float | None:
        """Return the humidity.

        Uses the configured custom humidity sensor if set and available,
        otherwise falls back to the Met.no forecast value.
        """
        if (value := self._get_sensor_value(self._humidity_entity_id)) is not None:
            return value
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_HUMIDITY]
        )

    @property
    @override
    def native_wind_speed(self) -> float | None:
        """Return the wind speed."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_WIND_SPEED]
        )

    @property
    @override
    def wind_bearing(self) -> float | str | None:
        """Return the wind direction."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_WIND_BEARING]
        )

    @property
    @override
    def native_wind_gust_speed(self) -> float | None:
        """Return the wind gust speed in native units."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_WIND_GUST_SPEED]
        )

    @property
    @override
    def cloud_coverage(self) -> float | None:
        """Return the cloud coverage."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_CLOUD_COVERAGE]
        )

    @property
    @override
    def native_dew_point(self) -> float | None:
        """Return the dew point."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_DEW_POINT]
        )

    @property
    @override
    def uv_index(self) -> float | None:
        """Return the uv index."""
        return self.coordinator.data.current_weather_data.get(
            ATTR_MAP[ATTR_WEATHER_UV_INDEX]
        )

    def _forecast(self, hourly: bool) -> list[Forecast] | None:
        """Return the forecast array."""
        if hourly:
            met_forecast = self.coordinator.data.hourly_forecast
        else:
            met_forecast = self.coordinator.data.daily_forecast
        required_keys = {"temperature", ATTR_FORECAST_TIME}
        ha_forecast: list[Forecast] = []
        for met_item in met_forecast:
            if not set(met_item).issuperset(required_keys):
                continue
            ha_item = {
                k: met_item[v]
                for k, v in FORECAST_MAP.items()
                if met_item.get(v) is not None
            }
            if ha_item.get(ATTR_FORECAST_CONDITION):
                ha_item[ATTR_FORECAST_CONDITION] = format_condition(
                    ha_item[ATTR_FORECAST_CONDITION]
                )
            ha_forecast.append(ha_item)  # type: ignore[arg-type]
        return ha_forecast

    @callback
    @override
    def _async_forecast_daily(self) -> list[Forecast] | None:
        """Return the daily forecast in native units."""
        return self._forecast(False)

    @callback
    @override
    def _async_forecast_hourly(self) -> list[Forecast] | None:
        """Return the hourly forecast in native units."""
        return self._forecast(True)
