SCHEDULED = []
def async_track_point_in_time(hass, action, point):
    SCHEDULED.append((point, action))
    def cancel(): 
        if (point, action) in SCHEDULED: SCHEDULED.remove((point, action))
    return cancel
def async_track_state_change_event(hass, entities, action): return lambda: None
def async_track_time_interval(hass, action, interval): return lambda: None
