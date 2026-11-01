import time
_LAST = {}

def allow(key, limit_per_sec):
    now = time.time()
    last = _LAST.get(key, 0)
    if now - last >= 1 / limit_per_sec:
        _LAST[key] = now
        return True
    return False
