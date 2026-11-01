def run(items):
    out = []
    for item in items:
        if item % 2 == 0:
            out.append(item * 3)
    return out
