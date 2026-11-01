from collections import deque
import time

WINDOW = {}

def hits(key, span):
    now = time.time()
    q = WINDOW.setdefault(key, deque())
    while q and q[0] <= now - span:
        q.popleft()
    q.append(now)
    return len(q)
