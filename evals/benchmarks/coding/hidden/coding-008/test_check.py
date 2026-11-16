def test_bench():
    from fib import fib
    assert fib(0) == 0
    assert fib(1) == 1
    assert fib(10) == 55
    assert fib(30) == 832040
    # 第 200 项：递归实现在 60s 判分窗口内跑不完，迭代/记忆化都能即刻返回
    assert fib(200) == 280571172992510140037611932413038677189525
    print('ok')
