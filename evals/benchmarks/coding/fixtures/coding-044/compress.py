def zlib_size(text):
    import zlib
    return len(zlib.compress(text.encode('utf-8')))
