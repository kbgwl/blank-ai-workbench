def test_bench():
    from count_words import count_words
    assert count_words('hello world') == 2
    assert count_words('  a  b ')== 2
    assert count_words('') == 0
    print('ok')

