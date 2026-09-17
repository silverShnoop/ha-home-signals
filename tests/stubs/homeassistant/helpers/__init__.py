class _Reg:
    @staticmethod
    def async_get(arg=None):
        # async_get(hass) returns the registry; registry.async_get(entity_id)
        # returns an entry, and in this harness no entity is registered.
        return _Reg() if not isinstance(arg, str) else None
    def async_get_area(self, _a): return None
area_registry = _Reg
device_registry = _Reg
entity_registry = _Reg
