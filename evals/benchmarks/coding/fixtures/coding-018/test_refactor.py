def test_logger_available():
    import importlib

    import api

    assert api.logger is not None
    importlib.import_module("log_factory")
