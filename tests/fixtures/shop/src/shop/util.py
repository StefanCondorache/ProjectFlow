def slugify(text):
    return text.lower().replace(" ", "-")


def log_event(kind, key):
    print(kind, key)


def describe(obj):
    return obj.summary()


def total(store):
    return store.load_all()


def outer_fn(items):
    def fmt(x):
        return str(x)

    return [fmt(i) for i in items]
