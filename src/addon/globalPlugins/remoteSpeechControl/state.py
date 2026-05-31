"""Mute state machine for the controlled side.

Two flags drive ``should_drop_speech``:

muted_by_remote
    True when a controller has successfully armed muting via an authenticated
    ``mute_request``. Cleared on a successful ``unmute_request`` or on session
    disconnect. While set, audio is muted in conjunction with
    ``remote_driving`` — that's the ping-pong path.

remote_driving
    True between a remote-injected keystroke and the next physical local
    keystroke. Ping-pongs strictly: the WH_KEYBOARD_LL hook in
    inputmonitor.py reads ``KBDLLHOOKSTRUCT.flags`` for each event and flips
    this either way on every keydown. There is no decay timer. Also cleared
    whenever ``muted_by_remote`` goes False, so a session disconnect drops
    both flags atomically.

A speak call is silenced (via OS-level audio session mute, see audiomute.py)
when ``should_drop_speech`` is True, i.e. iff both flags are set. Listeners
on this state see every transition and toggle ``ISimpleAudioVolume.SetMute``
exactly on those transitions.

The controlled side has no local mute hotkey by design — there's nothing for
the local user to do that ping-pong doesn't already cover. Any physical
keypress unmutes immediately via the LL hook; to mute persistently the user
asks the controller (via the controller's toggle-mute hotkey), who sends a
``mute_request``.
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

    def restore_for_reload(self, muted_by_remote: bool, remote_driving: bool) -> None:
        """Set both flags directly without firing listeners.

        Used by ``audiomute.install()`` after a mid-session plugin reload
        when the OS-level audio session mute indicates the previous
        incarnation had armed muting. Bypasses ``_notify`` because the
        listener hasn't been registered yet at this point — and even if
        it had, we wouldn't want it to react: the OS audio mute is
        already in the desired state, the state singleton is the one
        that's stale, and the whole point is to bring the singleton
        back into agreement with reality without round-tripping through
        ``SetMute`` again.
        """
        with self._lock:
            self.muted_by_remote = muted_by_remote
            self.remote_driving = remote_driving


state = MuteState()
