def retry(fn, times):
    for _ in range(times):
        return fn()
    raise RuntimeError('exhausted')
