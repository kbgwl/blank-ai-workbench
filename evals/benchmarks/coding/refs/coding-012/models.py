import storage


class User:
    def save(self):
        storage.save_json(f'{self.name}.json', self.data)


class Order:
    def save(self):
        storage.save_json(f'{self.oid}.json', self.data)
