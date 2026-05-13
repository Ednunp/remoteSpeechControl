"""Synth-driver-level wedge that drops local audio synthesis while muted.

Why at the synth driver and not at speech.speak
-----------------------------------------------
NVDA Remote intercepts speech upstream at speech.speak, captures the speech
sequence, forwards it to the controller, then continues the call into NVDA's
normal pipeline. By the time control reaches the active SynthDriver's speak()
method, NVDA Remote has already shipped the speech to the controller. So if
we drop calls here, the controller still hears NVDA — only the local audio
on the controlled machine goes silent. Wrapping speech.speak instead would
also block the upstream forward, which is exactly wrong.

Pretending to take real time to "speak"
---------------------------------------
NVDA's say-all (used for "read all", and for any continuous reading of
rich-text or HTML content) chunks the text and queues the next chunk only
when the synth signals it has reached the IndexCommand at the end of the
current chunk. If we drop a speak() and fire synthIndexReached straight
away, NVDA bursts through the whole document in microseconds — and when
the user hits Shift to pause, the cursor is at the end with nothing left
to cancel.

So we estimate how long a real synth would take to read the sequence
(characters times approximate rate of the active synth) and schedule the
index and done-speaking events for that future time via wx.CallLater. To
make pause/stop behave correctly, we also wrap synth.cancel() and abort
every pending timer when it fires.

We re-wrap on every synthChanged event because changing synth (driver or
voice) replaces the live SynthDriver instance.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, List, Optional

from . import logger
from . import state as state_module

log = logger.get()


_original_speak: Optional[Callable[..., Any]] = None
_original_cancel: Optional[Callable[..., Any]] = None
_wrapped_synth: Optional[Any] = None
_synth_changed_handler: Optional[Callable[..., None]] = None

_pending_timers: List[Any] = []
_pending_lock = threading.Lock()

_DEFAULT_CHARS_PER_SEC = 15.0


def _compute_chars_per_second(synth: Any) -> float:
    """Map the synth's rate to an approximate speech speed.

    NVDA synths expose ``rate`` on 0-100 (50 ≈ default ≈ ~150 wpm ≈ 12-15
    chars/sec). Linear interpolate 5 cps at rate 0 to 50 cps at rate 100,
    which is in the ballpark for most voices.
    """
    try:
        rate = getattr(synth, "rate", None)
        if rate is None:
            return _DEFAULT_CHARS_PER_SEC
        rate = float(rate)
    except (TypeError, ValueError):
        return _DEFAULT_CHARS_PER_SEC
    return max(1.0, 5.0 + (rate / 100.0) * 45.0)


def _cancel_all_pending() -> None:
    with _pending_lock:
        timers = list(_pending_timers)
        _pending_timers.clear()
    for t in timers:
        try:
            t.Stop()
        except Exception:
            pass


def _track_timer(timer: Any) -> None:
    with _pending_lock:
        _pending_timers.append(timer)


def _untrack_timer(timer: Any) -> None:
    with _pending_lock:
        try:
            _pending_timers.remove(timer)
        except ValueError:
            pass


def _schedule_silent_completion(speechSequence: Any, synth: Any) -> None:
    try:
        import wx
        import synthDriverHandler
        try:
            from speech.commands import IndexCommand
        except ImportError:
            IndexCommand = None  # type: ignore[assignment]
    except Exception:
        log.exception("rsc: silent completion imports failed")
        return

    cps = _compute_chars_per_second(synth)
    chars_so_far = 0
    scheduled: List = []  # (delay_seconds, callable)

    for item in speechSequence:
        if isinstance(item, str):
            chars_so_far += len(item)
        elif IndexCommand is not None and isinstance(item, IndexCommand):
            delay_s = chars_so_far / cps
            idx = item.index

            def fire_index(_idx=idx, _syn=synth, _self=None):
                _untrack_timer(_self["t"]) if _self else None
                try:
                    synthDriverHandler.synthIndexReached.notify(synth=_syn, index=_idx)
                except Exception:
                    pass

            scheduled.append((delay_s, fire_index))

    total_delay_s = chars_so_far / cps

    def fire_done():
        try:
            synthDriverHandler.synthDoneSpeaking.notify(synth=synth)
        except Exception:
            pass

    scheduled.append((total_delay_s, fire_done))

    for delay_s, fn in scheduled:
        ms = max(1, int(delay_s * 1000))
        timer_holder: dict = {}

        def wrapper(_fn=fn, _holder=timer_holder):
            _untrack_timer(_holder.get("t"))
            _fn()

        timer = wx.CallLater(ms, wrapper)
        timer_holder["t"] = timer
        _track_timer(timer)


def _wrap(synth: Any) -> None:
    global _original_speak, _original_cancel, _wrapped_synth
    if synth is None or synth is _wrapped_synth:
        return
    _unwrap_current()
    try:
        original_speak = synth.speak
    except AttributeError:
        log.warning("rsc: synth has no speak attr: %r", synth)
        return
    original_cancel = getattr(synth, "cancel", None)

    def speak(speechSequence, *args, **kwargs):
        if state_module.state.should_drop_speech:
            _schedule_silent_completion(speechSequence, synth)
            return None
        return original_speak(speechSequence, *args, **kwargs)

    def cancel(*args, **kwargs):
        try:
            _cancel_all_pending()
        except Exception:
            log.exception("rsc: _cancel_all_pending failed; cancel continuing")
        if original_cancel is not None:
            return original_cancel(*args, **kwargs)

    try:
        synth.speak = speak
        if original_cancel is not None:
            synth.cancel = cancel
    except (AttributeError, TypeError):
        log.warning("rsc: cannot bind overrides on %r", synth)
        return

    _original_speak = original_speak
    _original_cancel = original_cancel
    _wrapped_synth = synth
    log.info("rsc: wrapped synth speak/cancel on %s", type(synth).__name__)


def _unwrap_current() -> None:
    global _original_speak, _original_cancel, _wrapped_synth
    if _wrapped_synth is None:
        return
    if _original_speak is not None:
        try:
            _wrapped_synth.speak = _original_speak
        except Exception:
            pass
    if _original_cancel is not None:
        try:
            _wrapped_synth.cancel = _original_cancel
        except Exception:
            pass
    _wrapped_synth = None
    _original_speak = None
    _original_cancel = None
    _cancel_all_pending()


def install() -> None:
    global _synth_changed_handler
    import synthDriverHandler

    cur = synthDriverHandler.getSynth()
    if cur is not None:
        _wrap(cur)

    def on_synth_changed(*args, **kwargs):
        try:
            cur = synthDriverHandler.getSynth()
            _wrap(cur)
        except Exception:
            log.exception("rsc: re-wrap after synth change failed")

    _synth_changed_handler = on_synth_changed
    try:
        synthDriverHandler.synthChanged.register(on_synth_changed)
    except AttributeError:
        log.warning("rsc: synthChanged extension point unavailable")


def uninstall() -> None:
    global _synth_changed_handler
    try:
        import synthDriverHandler
        if _synth_changed_handler is not None:
            try:
                synthDriverHandler.synthChanged.unregister(_synth_changed_handler)
            except Exception:
                pass
    finally:
        _synth_changed_handler = None
        _unwrap_current()
