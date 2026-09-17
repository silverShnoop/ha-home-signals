class SensorDeviceClass:
    ENUM = "enum"
class SensorEntity:
    hass = None
    def async_write_ha_state(self): pass
    def async_on_remove(self, fn): pass
    async def async_added_to_hass(self): pass
