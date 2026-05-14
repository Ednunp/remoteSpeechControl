"""Audio-level mute via nvwave.WavePlayer.feed.

When ``state.should_drop_speech`` is True at the moment of a feed call,
the wrapper substitutes the audio buffer with same-length zero bytes
(PCM silence) before passing it to NVDA's audio backend. The synth
still runs to completion at its natural pace: it generates audio
samples, fills the wave buffer, and NVDA's playback-position callbacks
(which drive ``synthIndexReached`` and ``synthDoneSpeaking`` for
say-all) fire at correct real-audio timing because the buffer drains at
the real sample rate regardless of whether the bytes are zeros or
real speech.

NVDA Remote's ``speech.speak`` interception is upstream of audio
output, so forwarded speech to the controller is unaffected. The mute
check happens per-feed, so the ping-pong behaviour (mute when remote
drives / unmute on a local keystroke) is naturally dynamic: the next
chunk after a state flip is either silenced or audible accordingly,
with at most one feed-chunk's worth of latency (typically tens of
milliseconds).

Why this is better than wrapping ``synth.speak``
------------------------------------------------
NVDA's synth events fire at real-audio-playback timing. Wrapping
``synth.speak`` at the synth-driver layer means the wedge has to fake
those timings, which we cannot do correctly across every synth and
rate — the result was either audible pauses (slow estimate) or
loss of stop-precision in say-all (fast estimate). Substituting
silence at the audio-output layer keeps the synth doing real work and
firing real events on real timings; only the audio device receives
silence instead of speech. Say-all pacing, stop-on-Shift, and every
other synth-event-driven behaviour Just Works.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import logger
from . import state as state_module

log = logger.get()


_original_feed: Optional[Callable[..., Any]] = None


def _patched_feed(self, data, *args, **kwargs):
    if state_module.state.should_drop_speech and data:
        try:
            length = len(data)
            if length > 0:
                data = bytes(length)  # zeros: PCM silence for any 16-bit format
        except (TypeError, ValueError):
            # data isn't a buffer we can size; leave it alone and let
            # the original feed handle whatever it is.
            pass
    return _original_feed(self, data, *args, **kwargs)


def install() -> None:
    global _original_feed
    if _original_feed is not None:
        return
    try:
        import nvwave
    except Exception:
        log.exception("rsc: nvwave unavailable; audio mute disabled")
        return
    cls = getattr(nvwave, "WavePlayer", None)
    if cls is None or not hasattr(cls, "feed"):
        log.warning("rsc: nvwave.WavePlayer.feed not found; audio mute disabled")
        return
    _original_feed = cls.feed
    try:
        cls.feed = _patched_feed
    except (AttributeError, TypeError):
        log.exception("rsc: cannot bind feed override on WavePlayer")
        _original_feed = None
        return
    log.info("rsc: nvwave.WavePlayer.feed wrapped; audio mute armed")


def uninstall() -> None:
    global _original_feed
    if _original_feed is None:
        return
    try:
        import nvwave
        cls = getattr(nvwave, "WavePlayer", None)
        if cls is not None:
            cls.feed = _original_feed
    except Exception:
        log.exception("rsc: failed restoring nvwave.WavePlayer.feed")
    _original_feed = None
    log.info("rsc: nvwave.WavePlayer.feed unwrapped")
