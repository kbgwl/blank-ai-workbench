class User:
    def save(self):
        with open(f'{self.name}.json', 'w', encoding='utf-8') as f:
            f.write(self.data)

class Order:
    def save(self):
        with open(f'{self.oid}.json', 'w', encoding='utf-8') as f:
            f.write(self.data)
