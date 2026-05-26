"""Local-keystroke detection via WH_KEYBOARD_LL.

The half of ping-pong attribution that runs in the kernel input
thread: physical (non-injected) keystrokes on the controlled machine
mark ``state.remote_driving = False`` so the audio session unmutes the
moment the local user touches the keyboard.

The OTHER half — telling our state machine that a remote keystroke
just arrived — is **not** done here. It used to be (versions 0.7.0
through 0.7.7), reading ``LLKHF_INJECTED`` to flag "this came from
SendInput, treat as remote". That stopped being reliable in 0.7.7
testing: Windows / NVDA / various text-input plumbing turn out to
echo every physical keystroke with the injected bit set ~30–200 ms
later, producing a phantom "remote" event for every local keystroke.
With ``muted_by_remote`` armed, those phantoms toggled the WASAPI
mute on and off per keystroke and chopped edit-field speech to
silence.

So the LL hook only acts on the non-injected case now:

* physical key → ``state.mark_local_input()``  (``remote_driving = False``)
* injected key → IGNORED (the LL-hook injected bit is too noisy to trust)

The authoritative "remote is driving" signal moved into
``remoteintegration.py``'s ``LocalMachine.sendKey`` wrap, which only
fires when NVDA Remote is actually replaying a remote keystroke on
this machine. Phantom Windows-injected echoes can't pretend to be
NVDA Remote.

Performance discipline
----------------------
A WH_KEYBOARD_LL hook runs on the system input thread and gates every
keystroke system-wide. Anything slow in the hook procedure makes
typing feel laggy in every application. We do the absolute minimum:

* one cheap attribute read to short-circuit when no session has armed mute
* one ``ctypes.cast`` to read the hook struct
* one bitwise-and on the flags
* on the physical branch only, one ``wx.CallAfter``

Then we return through ``CallNextHookEx`` immediately.
"""
from __future__ import annotations

import ctypes
from ctypes import POINTER, c_int, c_long, wintypes

import wx

from . import logger
from . import state as state_module

log = logger.get()


# Win32 constants
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_INJECTED = 0x00000010
LLKHF_LOWER_IL_INJECTED = 0x00000002
HC_ACTION = 0


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


LowLevelKeyboardProc = ctypes.WINFUNCTYPE(
    c_long, c_int, wintypes.WPARAM, wintypes.LPARAM
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_user32.SetWindowsHookExW.argtypes = [c_int, LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD]
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.CallNextHookEx.argtypes = [wintypes.HHOOK, c_int, wintypes.WPARAM, wintypes.LPARAM]
_user32.CallNextHookEx.restype = c_long
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE


_hook_handle: int | None = None
_hook_proc_ref: object | None = None  # keep the WINFUNCTYPE alive


def _hook_proc(nCode, wParam, lParam):
    # MUST NOT raise. A raised exception from a low-level hook proc
    # drops keystrokes system-wide.
    #
    # Short-circuit when no controller has armed muting on this
    # machine: the ping-pong attribution is meaningless when
    # ``muted_by_remote`` is False — ``should_drop_speech`` can never
    # be True regardless of ``remote_driving`` — so skipping the
    # wx.CallAfter on every system keystroke removes any conceivable
    # per-key overhead from this add-on outside of an active muted
    # session. Direct attribute read without acquiring state._lock —
    # a Python boolean read is atomic and a stale read is bounded
    # harm (one keystroke too late at worst).
    if (
        nCode == HC_ACTION
        and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
        and state_module.state.muted_by_remote
    ):
        try:
            kb = ctypes.cast(lParam, POINTER(KBDLLHOOKSTRUCT))[0]
        except Exception:
            kb = None
        if kb is not None:
            try:
                is_injected = bool(kb.flags & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED))
                if not is_injected:
                    # Physical key: confirmed local user activity,
                    # unmute via ping-pong. Injected keys are
                    # ignored here; see module docstring.
                    wx.CallAfter(state_module.state.mark_local_input)
            except Exception:
                pass
    return _user32.CallNextHookEx(_hook_handle or 0, nCode, wParam, lParam)


def install() -> None:
    global _hook_handle, _hook_proc_ref
    if _hook_handle is not None:
        return
    _hook_proc_ref = LowLevelKeyboardProc(_hook_proc)
    hMod = _kernel32.GetModuleHandleW(None)
    handle = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_proc_ref, hMod, 0)
    if not handle:
        err = ctypes.get_last_error()
        log.error("rsc: SetWindowsHookEx(WH_KEYBOARD_LL) failed (GetLastError=%d)", err)
        _hook_proc_ref = None
        return
    _hook_handle = handle
    log.info("rsc: WH_KEYBOARD_LL hook installed for ping-pong attribution (physical-only)")


def uninstall() -> None:
    global _hook_handle, _hook_proc_ref
    if _hook_handle is None:
        return
    try:
        _user32.UnhookWindowsHookEx(_hook_handle)
    except Exception:
        log.exception("rsc: UnhookWindowsHookEx failed")
    _hook_handle = None
    _hook_proc_ref = None
    log.info("rsc: WH_KEYBOARD_LL hook removed")
