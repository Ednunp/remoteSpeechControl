"""Tagged logger using NVDA's standard log handler.

NVDA add-ons should write through ``logHandler.log`` rather than a
``logging.getLogger(name)`` of our own — only the former is guaranteed to
reach NVDA's log file across all NVDA versions.

Verbosity
---------
``configure_verbosity(verbose)`` toggles whether our ``info``-level
messages are emitted. ``warning``, ``error`` and ``exception`` always
emit; ``debug`` always defers to NVDA's global log level (it never
appears unless NVDA is itself in debug mode). So the user-facing
"Verbose logging" checkbox controls the everyday operational log noise:
on for diagnosis, off for quiet running. Default on so a freshly
installed addon is self-documenting in the log.
"""
from __future__ import annotations

from typing import Any


class _Logger:
    def __init__(self) -> None:
        self._info_enabled: bool = True

    def set_info_enabled(self, enabled: bool) -> None:
        self._info_enabled = bool(enabled)

    def _delegate(self, level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        try:
            from logHandler import log as _nvda_log
        except Exception:
            return
        getattr(_nvda_log, level)(msg, *args, **kwargs)

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if not self._info_enabled:
            return
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
    """Enable or disable ``info``-level emission from the addon's logger.

    ``warning``, ``error`` and ``exception`` continue to emit regardless,
    so genuine problems are never hidden by this setting.
    """
    _logger.set_info_enabled(verbose)
