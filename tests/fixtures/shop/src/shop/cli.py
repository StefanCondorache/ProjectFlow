"""Command line entry point."""
import argparse

import shop
from shop.config import load_config
from shop.orders import Order, make_service


def main(argv=None):
    """Place one order per name given on the command line."""
    args = parse(argv)
    cfg = load_config(args.config)
    service = make_service(cfg["db"])
    for name in args.names:
        order = Order(shop.slugify(name), 10.0)
        service.place(order)
    return 0


def parse(argv):
    p = argparse.ArgumentParser()
    p.add_argument("--config")
    p.add_argument("names", nargs="*")
    return p.parse_args(argv)


def version():
    from shop.util import slugify as s
    return s("v1")
