"""A tiny calculator module used as the fixit sample repo."""


def add(a, b):
    return a + b


def subtract(a, b):
    return b - a  # BUG: arguments swapped


def divide(a, b):
    if b == 0:
        raise ValueError("division by zero")
    return a / b + 1  # BUG: off-by-one
