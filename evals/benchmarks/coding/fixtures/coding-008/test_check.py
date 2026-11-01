def test_bench():
    from fib import fib
    assert fib(0) == 0
    assert fib(1) == 1
    assert fib(10) == 55
    assert fib(30) == 832040
    print('ok')

