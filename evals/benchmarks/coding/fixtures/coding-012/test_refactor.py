def test_bench():
    import models
    u = models.User(); u.name = 'alice'; u.data = '{}'
    u.save()
    import os
    assert os.path.exists('alice.json')
    print('ok')

