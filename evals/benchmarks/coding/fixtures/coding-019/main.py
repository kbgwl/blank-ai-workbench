def process(raw):
    if not raw.isdigit():
        raise ValueError('not a number')
    value = int(raw) * 2
    return f'value={value}'
