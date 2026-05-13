"""Distinguishes physical keystrokes from NVDA-Remote-injected ones.

NVDA Remote (now bundled in NVDA core as ``_remoteClient``) replays remote
keystrokes via raw ``SendInput()`` with no extra-info sentinel, so by the
time NVDA's own hook sees them they look identical to physical keys at the
content level. Windows itself, however, sets ``LLKHF_INJECTED`` in the
KBDLLHOOKSTRUCT.flags field for any synthesised input. We install our own
``WH_KEYBOARD_LL`` hook to read that flag and combine it with a short
"injection window" opened by ``remoteintegration`` whenever NVDA Remote's
``LocalMachine.sendKey`` is called.

State logic per key-down event:

    physical (not injected)              -> mark_local_input()
    injected within injection window     -> mark_remote_input()
    injected outside the window          -> ignore (some other tool)

The third case matters because NVDA itself, AutoHotkey, the on-screen
keyboard and so on all also produce injected events. Without the window
gate we'd flicker into "remote driving" any time NVDA used SendInput
internally.
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Optional

from . import logger
from . import state as state_module

log = logger.get()

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10

INJECTION_WINDOW_S = 0.300


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


_LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _LowLevelKeyboardProc, wintypes.HMODULE, wintypes.DWORD]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.CallNextHookEx.restype = wintypes.LPARAM
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


_hook_handle: Optional[int] = None
_hook_proc_ref: Optional[object] = None  # keep ctypes wrapper alive

_window_lock = threading.Lock()
_injection_window_until: float = 0.0


def open_injection_window() -> None:
    """Called by remoteintegration immediately before NVDA Remote injects a key.

    Opens a brief window during which the next injected keystroke seen by
    our low-level hook is attributed to NVDA Remote. Outside the window,
    injected keystrokes are ignored (treated as some other tool's traffic
    that should not affect mute state).
    """
    global _injection_window_until
    with _window_lock:
        _injection_window_until = time.monotonic() + INJECTION_WINDOW_S


def _is_injection_window_open() -> bool:
    with _window_lock:
        return time.monotonic() < _injection_window_until


def _hook_callback(nCode: int, wParam: int, lParam: int) -> int:
    if nCode == HC_ACTION and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
        try:
            kbd = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
            injected = bool(kbd.flags & LLKHF_INJECTED)
            if injected:
                if _is_injection_window_open():
                    state_module.state.mark_remote_input()
            else:
                state_module.state.mark_local_input()
        except Exception:
            log.exception("rsc: keyboard hook callback failed")
    return _user32.CallNextHookEx(None, nCode, wParam, lParam)


def install() -> None:
    """Install the WH_KEYBOARD_LL hook. Idempotent."""
    global _hook_handle, _hook_proc_ref
    if _hook_handle is not None:
        return
    _hook_proc_ref = _LowLevelKeyboardProc(_hook_callback)
    module = _kernel32.GetModuleHandleW(None)
    handle = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_proc_ref, module, 0)
    if not handle:
        err = ctypes.WinError(ctypes.get_last_error())
        log.error("rsc: SetWindowsHookExW failed: %s", err)
        _hook_proc_ref = None
        return
    _hook_handle = handle
    log.info("rsc: WH_KEYBOARD_LL hook installed (handle=0x%x)", handle)


def uninstall() -> None:
    global _hook_handle, _hook_proc_ref, _injection_window_until
    if _hook_handle is not None:
        _user32.UnhookWindowsHookEx(_hook_handle)
        _hook_handle = None
    _hook_proc_ref = None
    with _window_lock:
        _injection_window_until = 0.0
