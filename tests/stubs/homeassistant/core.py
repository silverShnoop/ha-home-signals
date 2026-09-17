CALLBACK_TYPE = object
class Event: pass
class EventStateChangedData: pass
class HomeAssistant:
    def __init__(self): self.states = StateMachine()
class State:
    def __init__(self, entity_id, state, attributes=None, last_changed=None):
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}
        self.last_changed = last_changed
        self.domain = entity_id.split(".")[0]
class StateMachine:
    def __init__(self): self._d = {}
    def set(self, s): self._d[s.entity_id] = s
    def get(self, eid): return self._d.get(eid)
    def async_all(self, domain=None):
        return [s for s in self._d.values() if domain is None or s.domain == domain]
def callback(fn): return fn
