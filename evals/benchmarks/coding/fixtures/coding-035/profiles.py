def rename(mapping: dict[str, str]) -> dict[str, str]:
    # BUG：这里拿 enumerate 的序号当 key，返回的是 dict[int, str]，
    # 与注解 dict[str, str] 不符。修实现，不要改注解。
    return {index: value for index, (key, value) in enumerate(mapping.items())}
