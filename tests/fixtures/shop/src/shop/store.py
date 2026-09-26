"""Persistence."""
import sqlite3

from shop.util import slugify


class BaseStore:
    def connect(self):
        return sqlite3.connect(self.path)

    def close(self):
        pass


class Store(BaseStore):
    """Stores orders in SQLite."""

    def __init__(self, path: str):
        self.path = path
        self.conn = self.connect()

    def save(self, order):
        key = slugify(order.name)
        self.conn.execute("insert", (key,))
        return key

    def load_all(self):
        return self.conn.execute("select").fetchall()


class AuditStore(Store):
    def save(self, order):
        key = super().save(order)
        self.audit(key)
        return key

    def audit(self, key):
        print("audit", key)
