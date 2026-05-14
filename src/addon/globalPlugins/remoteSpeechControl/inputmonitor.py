"""Distinguishes physical keystrokes from NVDA-Remote-injected ones.

Previous implementation used a system-wide ``WH_KEYBOARD_LL`` hook to
read Windows' ``LLKHF_INJECTED`` flag. That hook was reliable for
attribution but interacted badly with NVDA's say-all in rich-text /
ebook content — Shift-to-pause stopped behaving correctly while the
hook was installed. So that approach has been removed.

Current implementation: a per-key handoff between
``remoteintegration`` and this module via a small expiring buffer.

1. When NVDA Remote is about to replay a remote keystroke, our wrap on
   ``LocalMachine.sendKey`` (in remoteintegration.py) calls
   ``open_injection_window(vk_code)`` here, adding the vk_code with a
   300 ms expiry.
2. When NVDA's input dispatcher (``inputCore.manager.executeGesture``)
   processes the resulting key event, our wrap consults the buffer.
   If the gesture's vk_code matches a non-expired entry, the entry is
   consumed and the gesture is attributed as remote; otherwise it's a
   local physical key.

This gives us per-key precision without installing any system hook
and keeps all our work inside NVDA's normal Python control flow.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, List, Optional, Tuple

from . import logger
from . import state as state_module

log = logger.get()


INJECTION_WINDOW_S = 0.300
_WILDCARD_VK = -1  # matches any vk_code, used when the wrap can't extract one


_pending_lock = threading.Lock()
_pending_injected: List[Tuple[int, float]] = []  # (vk_code, expire_at)

_original_execute: Optional[Callable[..., Any]] = None


def open_injection_window(vk_code: Optional[int] = None) -> None:
    """Called by remoteintegration just before NVDA Remote injects a key.

    A specific vk_code gives precise per-key attribution. ``None`` falls
    back to a wildcard entry that matches the next keyboard gesture
    within the window.
    """
    now = time.monotonic()
    expire = now + INJECTION_WINDOW_S
    with _pending_lock:
        _pending_injected[:] = [(v, e) for v, e in _pending_injected if e > now]
        _pending_injected.append((vk_code if vk_code is not None else _WILDCARD_VK, expire))


def _consume_injected(vk_code: int) -> bool:
    now = time.monotonic()
    with _pending_lock:
        _pending_injected[:] = [(v, e) for v, e in _pending_injected if e > now]
        for i, (v, _e) in enumerate(_pending_injected):
            if v == vk_code or v == _WILDCARD_VK:
                del _pending_injected[i]
                return True
    return False


def _patched_execute_gesture(gesture, *args, **kwargs):
    try:
        from keyboardHandler import KeyboardInputGesture
    except Exception:
        KeyboardInputGesture = None
    if KeyboardInputGesture is not None and isinstance(gesture, KeyboardInputGesture):
        try:
            vk = getattr(gesture, "vkCode", None)
            if vk is not None and _consume_injected(vk):
                state_module.state.mark_remote_input()
            else:
                state_module.state.mark_local_input()
        except Exception:
            log.exception("rsc: gesture attribution failed")
    return _original_execute(gesture, *args, **kwargs)


def install() -> None:
    global _original_execute
    if _original_execute is not None:
        return
    try:
        import inputCore
    except Exception:
        log.exception("rsc: inputCore unavailable; input attribution disabled")
        return
    manager = getattr(inputCore, "manager", None)
    if manager is None or not hasattr(manager, "executeGesture"):
        log.warning("rsc: inputCore.manager.executeGesture not found")
        return
    _original_execute = manager.executeGesture
    try:
        manager.executeGesture = _patched_execute_gesture
    except (AttributeError, TypeError):
        log.exception("rsc: cannot bind executeGesture override")
        _original_execute = None
        return
    log.info("rsc: inputCore.manager.executeGesture wrapped for key attribution")


def uninstall() -> None:
    global _original_execute
    if _original_execute is None:
        return
    try:
        import inputCore
        manager = getattr(inputCore, "manager", None)
        if manager is not None:
            manager.executeGesture = _original_execute
    except Exception:
        log.exception("rsc: failed restoring inputCore.manager.executeGesture")
    _original_execute = None
    with _pending_lock:
        _pending_injected.clear()
