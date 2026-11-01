def test_bench():
    from report import render
    assert render('alice', 30) == '用户 alice 合计 30 元'
    print('ok')

