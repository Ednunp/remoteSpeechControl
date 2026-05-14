"""OS-level audio session mute via Windows Core Audio (WASAPI).

Replaces the earlier ``nvwave.WavePlayer.feed`` byte-substitution
approach. That older wedge zero-filled the audio buffer per chunk, and
on NVDA 2026.1 / Python 3.13 / WASAPI it perturbed ``WavePlayer``'s
drain reporting just enough to race the synth-index callback chain in
``speech.sayAll``: the race fired queued ``nextLine`` callbacks after
``_TextReader.stop()`` had already cleared ``textInfo``, producing
this in the log::

    File "speech\\sayAll.pyc", line 280, in collapseLineImpl
    AttributeError: 'NoneType' object has no attribute 'collapse'

This module instead toggles the Windows audio session mute flag on
NVDA's own process via ``ISimpleAudioVolume.SetMute``. The audio data
flows through WavePlayer unchanged, the synth runs end-to-end at real
timing, every index fires on the same WavePlayer drain it would have
fired without the addon — only the speakers stop receiving output.
Say-all, pause-on-Shift, stop-on-Ctrl all behave identically to
addon-not-installed.

State integration
-----------------
The module registers a listener on ``state.MuteState`` so that any
transition of ``should_drop_speech`` toggles ``SetMute`` exactly once,
not per audio chunk. The COM call is marshalled to the main thread via
``wx.CallAfter`` to avoid threading complications with comtypes objects
acquired on the wx thread.
"""
from __future__ import annotations

import threading
from ctypes import POINTER, c_bool, c_float, c_int, c_uint, c_void_p
from ctypes.wintypes import LPCWSTR

import comtypes
from comtypes import COMMETHOD, GUID, CLSCTX_ALL, CoCreateInstance, IUnknown

import wx

from . import logger
from . import state as state_module

log = logger.get()


# ---------------------------------------------------------------------------
# Core Audio COM interface bindings — minimum needed to call SetMute on the
# current process's audio session.
# ---------------------------------------------------------------------------

CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")

# EDataFlow
eRender = 0

# ERole
eConsole = 0


class ISimpleAudioVolume(IUnknown):
    _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "SetMasterVolume",
                  (["in"], c_float, "fLevel"),
                  (["in"], POINTER(GUID), "EventContext")),
        COMMETHOD([], comtypes.HRESULT, "GetMasterVolume",
                  (["out"], POINTER(c_float), "pfLevel")),
        COMMETHOD([], comtypes.HRESULT, "SetMute",
                  (["in"], c_bool, "bMute"),
                  (["in"], POINTER(GUID), "EventContext")),
        COMMETHOD([], comtypes.HRESULT, "GetMute",
                  (["out"], POINTER(c_bool), "pbMute")),
    ]


class IAudioSessionManager2(IUnknown):
    _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "GetAudioSessionControl",
                  (["in"], POINTER(GUID), "AudioSessionGuid"),
                  (["in"], c_uint, "StreamFlags"),
                  (["out"], POINTER(POINTER(IUnknown)), "SessionControl")),
        COMMETHOD([], comtypes.HRESULT, "GetSimpleAudioVolume",
                  (["in"], POINTER(GUID), "AudioSessionGuid"),
                  (["in"], c_uint, "StreamFlags"),
                  (["out"], POINTER(POINTER(ISimpleAudioVolume)), "AudioVolume")),
        # Remaining methods (GetSessionEnumerator, RegisterSessionNotification,
        # etc.) intentionally omitted — we never call them, and trimming the
        # vtable here doesn't matter because comtypes only uses _methods_ for
        # named-method dispatch on this object, not for layout.
    ]


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "Activate",
                  (["in"], POINTER(GUID), "iid"),
                  (["in"], c_uint, "dwClsCtx"),
                  (["in"], c_void_p, "pActivationParams"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], comtypes.HRESULT, "OpenPropertyStore",
                  (["in"], c_uint, "stgmAccess"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppProperties")),
        COMMETHOD([], comtypes.HRESULT, "GetId",
                  (["out"], POINTER(LPCWSTR), "ppstrId")),
        COMMETHOD([], comtypes.HRESULT, "GetState",
                  (["out"], POINTER(c_uint), "pdwState")),
    ]


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = [
        COMMETHOD([], comtypes.HRESULT, "EnumAudioEndpoints",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_uint, "dwStateMask"),
                  (["out"], POINTER(POINTER(IUnknown)), "ppDevices")),
        COMMETHOD([], comtypes.HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"),
                  (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        # GetDevice / RegisterEndpointNotificationCallback /
        # UnregisterEndpointNotificationCallback omitted on purpose.
    ]


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_simple_audio_volume = None
_apply_lock = threading.Lock()
_listener_registered = False


def _acquire_volume():
    """Lazily acquire ISimpleAudioVolume on the current process's audio session.

    Returns the volume control on success, or None if any COM step
    failed (in which case we log once and stop trying for the lifetime
    of this module instance — the user will have to restart NVDA to
    retry).
    """
    global _simple_audio_volume
    if _simple_audio_volume is not None:
        return _simple_audio_volume
    try:
        enumerator = CoCreateInstance(
            CLSID_MMDeviceEnumerator,
            interface=IMMDeviceEnumerator,
            clsctx=CLSCTX_ALL,
        )
        device = enumerator.GetDefaultAudioEndpoint(eRender, eConsole)
        session_mgr_unk = device.Activate(
            IAudioSessionManager2._iid_,
            CLSCTX_ALL,
            None,
        )
        session_mgr = session_mgr_unk.QueryInterface(IAudioSessionManager2)
        # Passing AudioSessionGuid=NULL gives us the default per-process
        # session for our process (nvda.exe). We do not need to enumerate
        # sessions and match on PID — Windows hands us our own session
        # directly.
        volume = session_mgr.GetSimpleAudioVolume(None, 0)
        _simple_audio_volume = volume
        log.info("rsc: acquired ISimpleAudioVolume on NVDA audio session")
        return volume
    except Exception:
        log.exception("rsc: failed to acquire audio session volume")
        return None


def _do_set_mute(desired: bool) -> None:
    """Main-thread COM call. Always runs serialised by _apply_lock."""
    with _apply_lock:
        volume = _acquire_volume()
        if volume is None:
            return
        try:
            volume.SetMute(desired, None)
            log.info("rsc: SetMute(%s) applied to NVDA audio session", desired)
        except Exception:
            log.exception("rsc: SetMute(%s) failed", desired)


def _on_state_changed() -> None:
    """State listener — fires whenever ``state.MuteState`` mutates.

    Schedules the actual ``SetMute`` call on the main thread because the
    state listener may be invoked from any thread (e.g. the WH_KEYBOARD_LL
    hook in inputmonitor.py funnels its events through wx.CallAfter, but
    callers from elsewhere in the addon may not).
    """
    desired = state_module.state.should_drop_speech
    try:
        wx.CallAfter(_do_set_mute, desired)
    except Exception:
        # If wx is unavailable for some reason, fall back to inline. The
        # COM call should be safe to make from most threads given the
        # apartment model COM uses for these interfaces, but we'd much
        # prefer to do it from the main thread.
        log.exception("rsc: wx.CallAfter unavailable; calling SetMute inline")
        _do_set_mute(desired)


def install() -> None:
    global _listener_registered
    if _listener_registered:
        return
    state_module.state.add_listener(_on_state_changed)
    _listener_registered = True
    log.info("rsc: OS-level audio session mute armed")


def uninstall() -> None:
    global _listener_registered, _simple_audio_volume
    if _listener_registered:
        try:
            state_module.state.remove_listener(_on_state_changed)
        except Exception:
            log.exception("rsc: remove_listener failed")
        _listener_registered = False
    # Always unmute on shutdown. Leaving the audio session muted would
    # be a nightmare for the user to discover later, since the addon's
    # UI wouldn't be there to undo it.
    try:
        if _simple_audio_volume is not None:
            _simple_audio_volume.SetMute(False, None)
    except Exception:
        log.exception("rsc: cleanup SetMute(False) failed")
    _simple_audio_volume = None
    log.info("rsc: OS-level audio session mute disarmed")
