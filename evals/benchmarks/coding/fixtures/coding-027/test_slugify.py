def test_bench():
    from slugify import slugify
    assert slugify('Hello World') == 'hello-world'
    assert slugify('A  B') == 'a-b'
    print('ok')

