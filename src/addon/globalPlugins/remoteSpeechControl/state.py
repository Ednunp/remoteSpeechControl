"""Mute state machine for the controlled side.

muted_by_remote
    True when a controller has successfully armed muting via an authenticated
    request. Cleared on successful unmute_request, on session disconnect, or
    by the local force-unmute hotkey.

remote_driving
    True between a remote-injected keyboard gesture and the next physical
    local key. Ping-pongs strictly with no decay.

A speak() call is dropped iff both flags are True at the moment of the call.
"""
from __future__ import annotations

import threading
from typing import Callable, List


class MuteState:
    def __init__(self):
        self._lock = threading.Lock()
        self.muted_by_remote: bool = False
        self.remote_driving: bool = False
        self._listeners: List[Callable[[], None]] = []

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable[[], None]) -> None:
        try:
            self._listeners.remove(fn)
        except ValueError:
            pass

    def _notify(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def set_muted_by_remote(self, on: bool) -> None:
        changed = False
        with self._lock:
            if self.muted_by_remote != on:
                self.muted_by_remote = on
                if not on:
                    self.remote_driving = False
                changed = True
        if changed:
            self._notify()

    def mark_remote_input(self) -> None:
        changed = False
        with self._lock:
            if not self.remote_driving:
                self.remote_driving = True
                changed = True
        if changed:
            self._notify()

    def mark_local_input(self) -> None:
        changed = False
        with self._lock:
            if self.remote_driving:
                self.remote_driving = False
                changed = True
        if changed:
            self._notify()

    @property
    def should_drop_speech(self) -> bool:
        with self._lock:
            return self.muted_by_remote and self.remote_driving


state = MuteState()
