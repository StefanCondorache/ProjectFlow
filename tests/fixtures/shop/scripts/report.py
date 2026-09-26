import helpers
from shop.store import Store


def run():
    rows = Store("shop.db").load_all()
    helpers.render(rows)
    s = helpers.Summary()
    s.add(rows)


if __name__ == "__main__":
    run()
