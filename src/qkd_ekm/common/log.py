import datetime as dt
import logging
import os
import re
import sys

LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [A-Za-z]+: .+$")


class _Fmt(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        # NOTE: intentionally local time (naive), matching the paper's log timestamps.
        return dt.datetime.fromtimestamp(record.created).strftime(  # noqa: DTZ006
            "%Y-%m-%d %H:%M:%S."
        ) + f"{int(record.msecs):03d}"


class _StdoutProxy:
    """Resolves sys.stdout at each write instead of once at handler creation.

    Loggers are process-wide singletons (get_logger only builds a handler the
    first time a given component name is seen), but pytest's capsys fixture
    replaces sys.stdout per-test. Binding the real object up front would make
    a component's very first logger construction -- whichever test happens to
    trigger it -- the only test capsys could ever observe it from.
    """

    def write(self, msg: str) -> int:
        return sys.stdout.write(msg)

    def flush(self) -> None:
        sys.stdout.flush()


def get_logger(component: str) -> logging.Logger:
    lg = logging.getLogger(component)
    if not lg.handlers:
        fmt = _Fmt("%(asctime)s %(name)s: %(message)s")
        h = logging.StreamHandler(_StdoutProxy())
        h.setFormatter(fmt)
        lg.addHandler(h)
        if os.environ.get("LOG_FILE"):
            fh = logging.FileHandler(os.environ["LOG_FILE"])
            fh.setFormatter(fmt)
            lg.addHandler(fh)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg
