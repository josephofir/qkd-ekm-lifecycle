import os


def env(name: str, default=None, required: bool = False):
    value = os.environ.get(name, default)
    if required and value is None:
        raise RuntimeError(f"missing env {name}")
    return value
