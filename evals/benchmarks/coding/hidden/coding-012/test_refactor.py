def test_bench():
    import models
    import storage

    public = [n for n in dir(storage) if not n.startswith('_') and callable(getattr(storage, n))]
    assert public, 'storage.py 需要一个公共保存函数'

    u = models.User(); u.name = 'alice'; u.data = '{"a":1}'
    u.save()
    o = models.Order(); o.oid = 'order123'; o.data = '{"b":2}'
    o.save()

    with open('alice.json', encoding='utf-8') as f:
        assert f.read() == '{"a":1}'
    with open('order123.json', encoding='utf-8') as f:
        assert f.read() == '{"b":2}'

    with open('models.py', encoding='utf-8') as f:
        src = f.read()
    assert 'storage' in src, 'models.py 应调用 storage.py 的公共函数'
    print('ok')
