"""Order processing."""
from dataclasses import dataclass

from shop.payments import gateway
from shop.store import Store

from . import util as u


@dataclass
class Order:
    name: str
    amount: float


class OrderService:
    def __init__(self, store: Store):
        self.store = store
        self.gateway = gateway.Gateway()

    def place(self, order: Order) -> str:
        """Charge the customer, then persist the order."""
        self.validate(order)
        receipt = self.gateway.charge(order.amount)
        key = self.store.save(order)
        u.log_event("placed", key)
        return receipt

    def validate(self, order):
        if order.amount <= 0:
            raise ValueError("amount must be positive")


def make_service(path) -> OrderService:
    return OrderService(Store(path))


def default_store():
    return Store("default.db")


def archive(order):
    st = default_store()
    st.save(order)
