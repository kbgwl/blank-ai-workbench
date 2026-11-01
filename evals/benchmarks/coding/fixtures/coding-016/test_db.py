def test_bench():
    import db
    assert db.user_sql() == 'SELECT * FROM users'
    assert db.order_sql() == 'SELECT * FROM orders'
    print('ok')

