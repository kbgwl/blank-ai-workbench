import time

STORE = {}

def put(k, v, ttl):
    STORE[k] = (v, time.time() + ttl)

def get(k):
    v, exp = STORE.get(k, (None, 0))
    if time.time() > exp:
        return None
    return v
