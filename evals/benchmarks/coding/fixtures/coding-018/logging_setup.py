import logging

def setup():
    logging.basicConfig(level=logging.INFO)
    return logging.getLogger('app')
