class Session:
    def __init__(self):
        self.data = {}
        self.dirty = False
    def set(self, k, v):
        self.data[k] = v
        self.dirty = True
    def commit(self):
        self.dirty = False
        return dict(self.data)
