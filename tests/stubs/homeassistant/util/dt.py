from datetime import datetime, timezone
NOW = [datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)]
def utcnow(): return NOW[0]
def parse_datetime(v):
    try: return datetime.fromisoformat(v)
    except Exception: return None
