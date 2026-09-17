class ConfigEntry:
    def __init__(self, options=None, data=None):
        self.entry_id = "test_entry"
        self.options = options or {}
        self.data = data or {}
