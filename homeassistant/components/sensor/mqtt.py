"""
Support for MQTT sensors.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.mqtt/
"""
import logging
import json
from datetime import timedelta
from typing import Optional

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.components.mqtt import (
    CONF_AVAILABILITY_TOPIC, CONF_STATE_TOPIC, CONF_PAYLOAD_AVAILABLE,
    CONF_PAYLOAD_NOT_AVAILABLE, CONF_QOS, MqttAvailability)
from homeassistant.components.sensor import DEVICE_CLASSES_SCHEMA
from homeassistant.const import (
    CONF_FORCE_UPDATE, CONF_NAME, CONF_VALUE_TEMPLATE, STATE_UNKNOWN,
    CONF_UNIT_OF_MEASUREMENT, CONF_ICON, CONF_DEVICE_CLASS)
from homeassistant.helpers.entity import Entity
import homeassistant.components.mqtt as mqtt
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import HomeAssistantType, ConfigType
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

CONF_EXPIRE_AFTER = 'expire_after'
CONF_JSON_ATTRS = 'json_attributes'
CONF_JSON_TEMPLATE_ATTRS = 'json_template_attributes'
CONF_UNIQUE_ID = 'unique_id'

DEFAULT_NAME = 'MQTT Sensor'
DEFAULT_FORCE_UPDATE = False
DEPENDENCIES = ['mqtt']

PLATFORM_SCHEMA = mqtt.MQTT_RO_PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_UNIT_OF_MEASUREMENT): cv.string,
    vol.Optional(CONF_ICON): cv.icon,
    vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    vol.Optional(CONF_JSON_ATTRS, default=[]): cv.ensure_list_csv,
    vol.Optional(CONF_EXPIRE_AFTER): cv.positive_int,
    vol.Optional(CONF_FORCE_UPDATE, default=DEFAULT_FORCE_UPDATE): cv.boolean,
    # Integrations shouldn't never expose unique_id through configuration
    # this here is an exception because MQTT is a msg transport, not a protocol
    vol.Optional(CONF_UNIQUE_ID): cv.string,
    vol.Optional(CONF_JSON_TEMPLATE_ATTRS):
        vol.Schema({cv.string: cv.template})
}).extend(mqtt.MQTT_AVAILABILITY_SCHEMA.schema)


async def async_setup_platform(hass: HomeAssistantType, config: ConfigType,
                               async_add_devices, discovery_info=None):
    """Set up MQTT Sensor."""
    if discovery_info is not None:
        config = PLATFORM_SCHEMA(discovery_info)

    value_template = config.get(CONF_VALUE_TEMPLATE)
    if value_template is not None:
        value_template.hass = hass

    json_template_attrs = config.get(CONF_JSON_TEMPLATE_ATTRS)
    if json_template_attrs is not None:
        for key, value in json_template_attrs.items():
            value.hass = hass
            _LOGGER.warning("test " + key)

    async_add_devices([MqttSensor(
        config.get(CONF_NAME),
        config.get(CONF_STATE_TOPIC),
        config.get(CONF_QOS),
        config.get(CONF_UNIT_OF_MEASUREMENT),
        config.get(CONF_FORCE_UPDATE),
        config.get(CONF_EXPIRE_AFTER),
        config.get(CONF_ICON),
        config.get(CONF_DEVICE_CLASS),
        value_template,
        config.get(CONF_JSON_ATTRS),
        json_template_attrs,
        config.get(CONF_UNIQUE_ID),
        config.get(CONF_AVAILABILITY_TOPIC),
        config.get(CONF_PAYLOAD_AVAILABLE),
        config.get(CONF_PAYLOAD_NOT_AVAILABLE),
    )])


class MqttSensor(MqttAvailability, Entity):
    """Representation of a sensor that can be updated using MQTT."""

    def __init__(self, name, state_topic, qos, unit_of_measurement,
                 force_update, expire_after, icon, device_class: Optional[str],
                 value_template, json_attributes, json_template_attributes,
                 unique_id: Optional[str], availability_topic,
                 payload_available, payload_not_available):
        """Initialize the sensor."""
        super().__init__(availability_topic, qos, payload_available,
                         payload_not_available)
        self._state = STATE_UNKNOWN
        self._name = name
        self._state_topic = state_topic
        self._qos = qos
        self._unit_of_measurement = unit_of_measurement
        self._force_update = force_update
        self._template = value_template
        self._expire_after = expire_after
        self._icon = icon
        self._device_class = device_class
        self._expiration_trigger = None
        self._json_attributes = set(json_attributes)
        self._json_template_attributes = json_template_attributes
        self._unique_id = unique_id
        self._attributes = None

    async def async_added_to_hass(self):
        """Subscribe to MQTT events."""
        await super().async_added_to_hass()

        @callback
        def message_received(topic, payload, qos):
            """Handle new MQTT messages."""
            # auto-expire enabled?
            if self._expire_after is not None and self._expire_after > 0:
                # Reset old trigger
                if self._expiration_trigger:
                    self._expiration_trigger()
                    self._expiration_trigger = None

                # Set new trigger
                expiration_at = (
                    dt_util.utcnow() + timedelta(seconds=self._expire_after))

                self._expiration_trigger = async_track_point_in_utc_time(
                    self.hass, self.value_is_expired, expiration_at)

            if self._json_attributes or self._json_template_attributes:
                print('parsing json!')
                try:
                    json_dict = json.loads(payload)
                    if not isinstance(json_dict, dict):
                        _LOGGER.warning("JSON result was not a dictionary")
                        json_dict = None

                except ValueError:
                    _LOGGER.warning("MQTT payload could not be parsed as JSON")
                    _LOGGER.debug("Erroneous JSON: %s", payload)
                self._attributes = {}

            if self._json_attributes and json_dict:
                print('enumerating json attributes!')
                for key in self._json_attributes:
                    self._attributes[key] = json_dict[key]

            if self._json_template_attributes and json_dict:
                print('enumerating json template attributes!')
                for key, template in self._json_template_attributes.items():
                    result = template.async_render_with_possible_json_value(
                        payload, self._state)
                    self._attributes[key] = result

            if self._template is not None:
                payload = self._template.async_render_with_possible_json_value(
                    payload, self._state)

            self._state = payload
            self.async_schedule_update_ha_state()

        await mqtt.async_subscribe(self.hass, self._state_topic,
                                   message_received, self._qos)

    @callback
    def value_is_expired(self, *_):
        """Triggered when value is expired."""
        self._expiration_trigger = None
        self._state = STATE_UNKNOWN
        self.async_schedule_update_ha_state()

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def unit_of_measurement(self):
        """Return the unit this state is expressed in."""
        return self._unit_of_measurement

    @property
    def force_update(self):
        """Force update."""
        return self._force_update

    @property
    def state(self):
        """Return the state of the entity."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return self._attributes

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def icon(self):
        """Return the icon."""
        return self._icon

    @property
    def device_class(self) -> Optional[str]:
        """Return the device class of the sensor."""
        return self._device_class
