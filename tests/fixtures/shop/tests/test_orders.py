from shop.orders import make_service


def test_service():
    assert make_service(":memory:")


if __name__ == "__main__":
    test_service()
