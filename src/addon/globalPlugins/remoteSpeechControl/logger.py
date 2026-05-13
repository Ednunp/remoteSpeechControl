"""Tagged logger using NVDA's standard log handler.

NVDA add-ons should write through ``logHandler.log`` rather than a
``logging.getLogger(name)`` of our own — only the former is guaranteed to
reach NVDA's log file across all NVDA versions.
"""
from __future__ import annotations

from typing import Any


class _Logger:
    def _delegate(self, level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        try:
            from logHandler import log as _nvda_log
        except Exception:
            return
        getattr(_nvda_log, level)(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._delegate("info", msg, *args, **kwargs)

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._delegate("debug", msg, *args, **kwargs)

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._delegate("warning", msg, *args, **kwargs)

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._delegate("error", msg, *args, **kwargs)

    def exception(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._delegate("exception", msg, *args, **kwargs)


_logger = _Logger()


def get() -> _Logger:
    return _logger


def configure_verbosity(verbose: bool) -> None:
    # NVDA controls its own log level globally; the verbose toggle is
    # retained as a future hook but currently no-ops.
    return
