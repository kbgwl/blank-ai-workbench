def test_bench():
    from shapes import Circle, Square
    import base
    assert issubclass(Circle, base.Shape)
    assert issubclass(Square, base.Shape)
    assert abs(Circle(1).area() - 3.14159) < 1e-5
    assert Square(2).area() == 4
    print('ok')

