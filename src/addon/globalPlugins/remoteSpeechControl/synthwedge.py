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

Silent completion
-----------------
NVDA's say-all (used for "read all", rich text and HTML reading) queues
the next chunk only after the synth signals it has reached the IndexCommand
at the end of the current chunk. If we drop a speak() and never fire that
signal, say-all stalls. So when we drop, we fire synthIndexReached for
each IndexCommand in the dropped sequence followed by synthDoneSpeaking,
deferred to the next wx event-loop tick so the dispatch is asynchronous
(same as a real synth firing them from its audio callback thread).

We deliberately do NOT try to estimate real-time speech duration and pace
the index events accordingly. Earlier versions did, with a chars-per-second
heuristic; the heuristic was inevitably wrong for some synth + rate
combinations and produced audible pauses between forwarded lines on the
controller side. The controller's own local synth already provides natural
playback pacing for forwarded speech, so we just fire the events promptly
and let NVDA's say-all proceed at whatever pace the wx event loop allows.

We re-wrap on synthChanged because changing synth or voice replaces the
live SynthDriver instance.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import logger
from . import state as state_module

log = logger.get()


_original_speak: Optional[Callable[..., Any]] = None
_wrapped_synth: Optional[Any] = None
_synth_changed_handler: Optional[Callable[..., None]] = None


def _emit_silent_completion(speechSequence: Any) -> None:
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

    synth = synthDriverHandler.getSynth()
    if synth is None:
        return

    indices = []
    if IndexCommand is not None:
        for item in speechSequence:
            if isinstance(item, IndexCommand):
                indices.append(item.index)

    def fire():
        for index in indices:
            try:
                synthDriverHandler.synthIndexReached.notify(synth=synth, index=index)
            except Exception:
                pass
        try:
            synthDriverHandler.synthDoneSpeaking.notify(synth=synth)
        except Exception:
            pass

    try:
        wx.CallAfter(fire)
    except Exception:
        log.exception("rsc: scheduling silent completion failed")


def _wrap(synth: Any) -> None:
    global _original_speak, _wrapped_synth
    if synth is None or synth is _wrapped_synth:
        return
    _unwrap_current()
    try:
        original_speak = synth.speak
    except AttributeError:
        log.warning("rsc: synth has no speak attr: %r", synth)
        return

    def speak(speechSequence, *args, **kwargs):
        if state_module.state.should_drop_speech:
            _emit_silent_completion(speechSequence)
            return None
        return original_speak(speechSequence, *args, **kwargs)

    try:
        synth.speak = speak
    except (AttributeError, TypeError):
        log.warning("rsc: cannot bind speak override on %r", synth)
        return

    _original_speak = original_speak
    _wrapped_synth = synth
    log.info("rsc: wrapped synth speak on %s", type(synth).__name__)


def _unwrap_current() -> None:
    global _original_speak, _wrapped_synth
    if _wrapped_synth is None:
        return
    if _original_speak is not None:
        try:
            _wrapped_synth.speak = _original_speak
        except Exception:
            pass
    _wrapped_synth = None
    _original_speak = None


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
