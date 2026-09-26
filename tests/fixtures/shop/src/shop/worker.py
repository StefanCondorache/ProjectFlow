"""Nightly batch worker."""
from shop.orders import archive, default_store


def work():
    store = default_store()
    for order in store.load_all():
        archive(order)


if __name__ == "__main__":
    work()
