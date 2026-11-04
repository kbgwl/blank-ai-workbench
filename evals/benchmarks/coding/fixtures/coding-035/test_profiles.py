def test_bench():
    from profiles import rename
    out = rename({'a': 'x'})
    assert out == {'A': 'x'}, f"期望 {{'A': 'x'}}，实际 {out}"
    assert all(isinstance(k, str) for k in out), f"key 必须是 str，实际 {out}"
    print('ok')


# 任务定义的 testCommand 是 `python -X utf8 test_profiles.py`（直接执行，不走 pytest）。
# 不写这个入口，测试函数永远不会被调用，命令永远退出 0——那等于没有测试。
if __name__ == "__main__":
    test_bench()
