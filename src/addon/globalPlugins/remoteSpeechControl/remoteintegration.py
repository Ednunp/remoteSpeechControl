"""Glue between Remote Speech Control and NVDA's bundled ``_remoteClient`` package.

Monkey patches:

1. ``TCPTransport.parse`` — intercept inbound messages whose ``type``
   begins with our ``remoteSpeechControl_`` prefix and dispatch them to our
   handlers before the original parser tries ``RemoteMessageType(...)``
   (which would reject and drop the message).

2. ``RemoteClient.onConnectedAsLeader`` / ``onConnectedAsFollower`` /
   ``onDisconnectedAsLeader`` / ``onDisconnectedAsFollower`` — hook our
   connect/disconnect setup directly into NVDA Remote's role lifecycle.
   We deliberately use these rather than the base ``Transport.__init__``
   because the latter doesn't reliably fire for the ``RelayTransport``
   subclass in current NVDA builds, while the role callbacks are part of
   ``_remoteClient``'s public flow and always run.

3. ``FollowerSession.handleClientDisconnected`` /
   ``LeaderSession.handleClientDisconnected`` — fires when the *peer*
   leaves the channel even though the local transport stays open
   (e.g. the controller closes their NVDA Remote session window but
   our follower transport is still alive). The role-level disconnect
   callbacks in (2) only fire on actual transport close, not on
   peer-leave, so without this hook ``muted_by_remote`` would stay
   True after the controller has visibly gone away and the controlled
   side's local user would be unable to clear the mute by typing.

4. ``LocalMachine.sendKey`` / ``LocalMachine.brailleInput`` — gate
   inbound input behind ``_input_frozen_transports`` so the controller
   cannot answer the controlled side's consent prompt themselves.
   ``sendKey`` *also* calls ``state.mark_remote_input()``: this is the
   authoritative "remote is driving" signal for the ping-pong mute.
   The LL hook in inputmonitor.py used to attribute injected events
   to the remote side via ``LLKHF_INJECTED``, but Windows and various
   text-input layers turn out to echo every physical keystroke with
   the injected bit set ~30–200 ms later — those phantom echoes
   toggled the WASAPI mute on and off per local keystroke and
   chopped edit-field speech to silence. Reading the wrap is much
   more accurate: it only fires when NVDA Remote is actually
   replaying a keystroke from the leader on our side, so phantoms
   from other Windows plumbing can't pretend to be NVDA Remote.

For sending we bypass the typed ``Transport.send`` (its first arg is
typed against the closed ``RemoteMessageType`` enum) and write directly
through the existing JSON serializer onto ``transport.queue``.
"""
from __future__ import annotations

import functools
import importlib
from typing import Any, Callable, Dict, List, Optional, Tuple

import wx

from . import config_spec
from . import logger
from . import protocol
from . import state as state_module

log = logger.get()


# Kept module-global because ``_running_client()`` reads it on every
# inbound message and lifecycle event. The other ``_remoteClient`` sub-
# modules we touch (transport, localMachine, client, session) are only
# referenced inside ``install()``, so they live as locals there.
_remote_pkg: Optional[Any] = None

_patches: List[Tuple[Any, str, Any]] = []
_nonce_trackers: Dict[int, protocol.NonceTracker] = {}
_failure_limiters: Dict[int, protocol.FailureLimiter] = {}
_transport_roles: Dict[int, str] = {}
_capability_processed: set = set()
_session_consented_transports: set = set()
_pending_consent_transports: set = set()
# Controller-side mirror of the controlled peer's audible-mute state,
# tracked from inbound MSG_STATE so the toggle-mute hotkey on the
# controller knows whether to open the "Mute?" or the "Unmute?"
# confirmation. Keyed by transport id.
_peer_muted_state: Dict[int, bool] = {}
# While non-empty, all inbound replay paths on the controlled side
# (sendKey, brailleInput) are dropped — the controller is locked out
# of driving the machine until the local user resolves the mute prompt.
_input_frozen_transports: set = set()

# Transport ids on the leader side where we've sent a battery_request
# and are waiting for the corresponding battery_response. Used to drive
# the timeout fallback.
_battery_request_pending: set = set()
# Transport ids on the leader side where the user selected the
# "remote then local" announcement order. When the battery_response
# arrives, the local battery is spoken after the remote one. The same
# tids drive the timeout fallback: if the remote never responds, the
# local battery is still spoken so the user isn't left silent.
_pending_post_remote_local_battery: set = set()

# How long to wait for the remote battery_response before falling back
# to the local-only announcement. Two seconds is well above typical
# LAN / Internet round-trips and short enough that the user doesn't
# perceive a stall.
BATTERY_REPLY_TIMEOUT_MS = 2000


def install() -> None:
    global _remote_pkg
    try:
        _remote_pkg = importlib.import_module("_remoteClient")
        transport_mod = importlib.import_module("_remoteClient.transport")
        local_machine_mod = importlib.import_module("_remoteClient.localMachine")
        client_mod = importlib.import_module("_remoteClient.client")
        session_mod = importlib.import_module("_remoteClient.session")
    except ImportError:
        log.warning("rsc: _remoteClient not present in this NVDA build; pairing disabled")
        return

    _patch(transport_mod.TCPTransport, "parse", _make_patched_parse)
    _patch(local_machine_mod.LocalMachine, "sendKey", _make_patched_send_key)
    if hasattr(local_machine_mod.LocalMachine, "brailleInput"):
        _patch(local_machine_mod.LocalMachine, "brailleInput", _make_patched_braille_input)

    if hasattr(session_mod.RemoteSession, "handleClientConnected"):
        _patch(session_mod.RemoteSession, "handleClientConnected", _make_patched_handle_client_connected)
    else:
        log.warning("rsc: RemoteSession has no handleClientConnected; late-joiners won't be paired")

    # Peer-left disconnect. Each session subclass defines its own
    # ``handleClientDisconnected``; the base class's method (which we
    # could otherwise patch once) is overridden in both subclasses, so
    # patching only the base would never see real peer-leaves. Patch
    # both subclasses directly.
    for cls_name in ("FollowerSession", "LeaderSession"):
        cls = getattr(session_mod, cls_name, None)
        if cls is None:
            log.warning("rsc: %s class missing from _remoteClient.session; peer-leave hook skipped", cls_name)
            continue
        if hasattr(cls, "handleClientDisconnected"):
            _patch(cls, "handleClientDisconnected", _make_patched_handle_client_disconnected)
        else:
            log.warning("rsc: %s has no handleClientDisconnected; peer-leave hook skipped", cls_name)

    rc_class = client_mod.RemoteClient
    for attr, is_leader, is_connect in (
        ("onConnectedAsLeader", True, True),
        ("onConnectedAsFollower", False, True),
        ("onDisconnectedAsLeader", True, False),
        ("onDisconnectedAsFollower", False, False),
    ):
        if not hasattr(rc_class, attr):
            log.warning("rsc: RemoteClient has no %s; skipping", attr)
            continue
        factory = (
            (lambda a, l=is_leader: lambda orig: _make_role_connect(orig, l))
            if is_connect
            else (lambda a, l=is_leader: lambda orig: _make_role_disconnect(orig, l))
        )(attr)
        _patch(rc_class, attr, factory)

    # Wrap processKeyInput so we can pre-empt the local speech queue
    # when a synth-settings-ring chord is about to schedule its
    # announcement. See ``_make_patched_process_key_input`` for the
    # full rationale.
    if hasattr(rc_class, "processKeyInput"):
        _patch(rc_class, "processKeyInput", _make_patched_process_key_input)
    else:
        log.warning("rsc: RemoteClient has no processKeyInput; synth-ring speech preempt skipped")

    log.info("rsc: _remoteClient integration installed")
    # The RemoteClient instance may not yet exist at our install() time
    # (NVDA initialises us first, then _remoteClient.initialize() runs).
    # Defer until the wx loop is idle so the singleton is ready.
    try:
        wx.CallAfter(apply_local_scripts)
    except Exception:
        log.exception("rsc: deferred apply_local_scripts schedule failed")
    # If a session is already live (i.e. the user reloaded plugins during
    # an active NVDA Remote session), several BoundMethodWeakref entries
    # in NVDA's extension-point system point to our *previous* incarnation
    # of the wrappers: the transport's inboundHandlers (KEY, BRAILLE_INPUT,
    # CLIENT_JOINED, CLIENT_LEFT) and inputCore.decide_handleRawKey (the
    # leader-side processKeyInput gate). Our just-completed uninstall
    # dropped the only strong refs to those wrappers, so those weakrefs
    # are now dead — Action.notify / Decider.decide silently skip them.
    # That's what makes the controlled machine stop accepting remote
    # keystrokes after a reload, and (with the processKeyInput patch now
    # in place) would also break the leader-side forwarding. Rebinding
    # adds the *current* (post-patch) bound methods alongside the dead
    # ones; dispatch starts working again on the next event.
    try:
        wx.CallAfter(_rebind_dispatch_handlers)
    except Exception:
        log.exception("rsc: deferred _rebind_dispatch_handlers schedule failed")


def uninstall() -> None:
    # Pull our scripts out of NVDA Remote's localScripts BEFORE we unpatch,
    # so we don't leave bound methods from a GlobalPlugin that's about to
    # be torn down hanging in there. If addon is later reloaded we re-add
    # them on the next register/apply cycle.
    rc = _running_client()
    if rc is not None:
        local_scripts = getattr(rc, "localScripts", None)
        if local_scripts is not None:
            for s in _persistent_local_scripts:
                local_scripts.discard(s)
            for s in _get_synth_ring_scripts():
                local_scripts.discard(s)
    _persistent_local_scripts.clear()

    while _patches:
        owner, attr, original = _patches.pop()
        try:
            setattr(owner, attr, original)
        except Exception:
            log.exception("rsc: failed restoring %r.%s", owner, attr)

    # Mid-session reload detection. If a RemoteClient session is still
    # connected at our teardown time, this is almost certainly a plugin
    # reload (NVDA hasn't disconnected the session yet, so it's not a
    # full shutdown) rather than an addon disable. In that case we want
    # to preserve the audio mute across the reload — install() and
    # _rebind_dispatch_handlers() will repopulate our shadow state, and
    # whatever mute the controller had armed stays armed. Otherwise
    # (addon disabled, NVDA shutting down, no session ever connected)
    # we release the mute as a safety measure so audio isn't left
    # muted with nothing to clear it.
    has_live_session = False
    if rc is not None:
        for t in (
            getattr(rc, "leaderTransport", None),
            getattr(rc, "followerTransport", None),
        ):
            if t is not None and getattr(t, "connected", False):
                has_live_session = True
                break

    _nonce_trackers.clear()
    _failure_limiters.clear()
    _transport_roles.clear()
    _capability_processed.clear()
    _session_consented_transports.clear()
    _pending_consent_transports.clear()
    _input_frozen_transports.clear()
    _peer_muted_state.clear()
    _battery_request_pending.clear()
    _pending_post_remote_local_battery.clear()
    if not has_live_session:
        state_module.state.set_muted_by_remote(False)


def _patch(owner: Any, attr: str, factory: Callable[[Any], Any]) -> None:
    original = getattr(owner, attr)
    replacement = factory(original)
    setattr(owner, attr, replacement)
    _patches.append((owner, attr, original))


# ---------------------------------------------------------------------------
# Inbound interception
# ---------------------------------------------------------------------------

_PREFIX_BYTES = protocol.CUSTOM_TYPE_PREFIX.encode("utf-8")


def _make_patched_parse(original: Callable[..., Any]) -> Callable[..., Any]:
    # Fast pre-filter: if the raw bytes don't even contain our prefix
    # substring, it can't be one of our custom messages. Skip the
    # deserialize and hand straight to the original parse — this keeps
    # the per-message overhead near zero for the dominant traffic
    # (SPEAK forwards during say-all). Only when our prefix appears
    # somewhere in the line do we deserialize to confirm and dispatch.
    @functools.wraps(original)
    def parse(self, line: bytes) -> None:
        if _PREFIX_BYTES not in line:
            return original(self, line)
        try:
            obj = self.serializer.deserialize(line)
        except Exception:
            return original(self, line)
        if isinstance(obj, dict):
            t = obj.get("type")
            if isinstance(t, str) and t.startswith(protocol.CUSTOM_TYPE_PREFIX):
                payload = {k: v for k, v in obj.items() if k != "type"}
                wx.CallAfter(_dispatch_custom, self, t, payload)
                return
        return original(self, line)
    return parse


# ---------------------------------------------------------------------------
# Inbound input gating (consent-freeze)
# ---------------------------------------------------------------------------

def _make_patched_send_key(original: Callable[..., Any]) -> Callable[..., Any]:
    # Two jobs:
    #   1. Gate inbound key injection while a consent prompt is pending,
    #      so the controller cannot drive the confirmation dialog
    #      themselves.
    #   2. Mark ``state.remote_driving = True`` — the authoritative
    #      "remote is driving" signal for the ping-pong mute. The LL
    #      hook in inputmonitor.py used to do this via ``LLKHF_INJECTED``
    #      but Windows / NVDA / text-input layers echo every physical
    #      keystroke with the injected bit set ~30–200 ms later,
    #      causing the WASAPI mute to flap on every local keystroke and
    #      chop edit-field speech to silence. Reading the wrap is much
    #      more accurate — it only fires when NVDA Remote is actually
    #      replaying a real keystroke from the leader on this side.
    @functools.wraps(original)
    def send_key(self, *args, **kwargs):
        if _input_frozen_transports:
            return None
        try:
            state_module.state.mark_remote_input()
        except Exception:
            log.exception("rsc: mark_remote_input from sendKey wrap failed")
        try:
            return original(self, *args, **kwargs)
        except Exception:
            log.exception("rsc: sendKey original raised")
            raise
    return send_key


def _make_patched_braille_input(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def braille_input(self, *args, **kwargs):
        if _input_frozen_transports:
            return None
        return original(self, *args, **kwargs)
    return braille_input


def _make_patched_handle_client_connected(original: Callable[..., Any]) -> Callable[..., Any]:
    # The transport-connect callback only fires once and runs before the
    # peer has actually joined the channel. If our peer joins later, our
    # first capability send was lost (relay does not replay history).
    # handleClientConnected fires whenever a peer joins the channel, so
    # re-announcing here covers the late-joiner case for both sides.
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        try:
            transport = getattr(self, "transport", None)
            if transport is not None and getattr(transport, "connected", False):
                log.info("rsc: peer joined session; re-announcing capability")
                _send_custom(transport, protocol.MSG_CAPABILITY, version=protocol.PROTOCOL_VERSION)
        except Exception:
            log.exception("rsc: re-announce capability on peer join failed")
        return result
    return wrapper


def _make_patched_handle_client_disconnected(original: Callable[..., Any]) -> Callable[..., Any]:
    # Fires when the *peer* leaves the session even though our local
    # transport stays alive (e.g. the controller closes their NVDA
    # Remote session window without dropping the relay connection).
    # The role-level RemoteClient.onDisconnectedAs* hooks only fire on
    # actual transport close, so without this hook ``muted_by_remote``
    # stays True after the controller is visibly gone, and the
    # controlled side's local user can't clear the mute by typing.
    # Clear our session state defensively here — if a new peer joins
    # next, the normal handleClientConnected path will re-arm anything
    # that's needed.
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        try:
            log.info("rsc: peer left session; clearing local mute state")
            state_module.state.set_muted_by_remote(False)
            # Also clear our capability-processed memory for this
            # transport so the next peer-join exchange is clean.
            transport = getattr(self, "transport", None)
            if transport is not None:
                tid = id(transport)
                _capability_processed.discard(tid)
                _peer_muted_state.pop(tid, None)
        except Exception:
            log.exception("rsc: clearing mute on peer-leave failed")
        return result
    return wrapper


def _send_custom(transport: Any, msg_type: str, **kwargs: Any) -> None:
    if not getattr(transport, "connected", False):
        log.warning("rsc: not sending %s, transport not connected", msg_type)
        return
    try:
        obj = transport.serializer.serialize(type=msg_type, **kwargs)
        transport.queue.put(obj)
        log.info("rsc: sent %s", msg_type)
    except Exception:
        log.exception("rsc: send %s failed", msg_type)


# ---------------------------------------------------------------------------
# Role lifecycle patches
# ---------------------------------------------------------------------------

def _make_role_connect(original: Callable[..., Any], is_leader: bool) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        role = "leader" if is_leader else "follower"
        try:
            transport = self.leaderTransport if is_leader else self.followerTransport
            if transport is None:
                log.warning("rsc: %s connect fired but %sTransport is None", role, role)
            else:
                _on_role_connected(transport, is_leader)
        except Exception:
            log.exception("rsc: post-connect setup failed (%s)", role)
        return result
    return wrapper


def _make_role_disconnect(original: Callable[..., Any], is_leader: bool) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self, *args, **kwargs):
        role = "leader" if is_leader else "follower"
        try:
            transport = self.leaderTransport if is_leader else self.followerTransport
            if transport is not None:
                _on_role_disconnected(transport, is_leader)
        except Exception:
            log.exception("rsc: pre-disconnect teardown failed (%s)", role)
        return original(self, *args, **kwargs)
    return wrapper


def _on_role_connected(transport: Any, is_leader: bool) -> None:
    tid = id(transport)
    role = "leader" if is_leader else "follower"
    _nonce_trackers[tid] = protocol.NonceTracker()
    _failure_limiters[tid] = protocol.FailureLimiter()
    _transport_roles[tid] = role
    log.info("rsc: %s transport connected (id=%x)", role, tid)
    log.info("rsc: announcing capability on %s transport", role)
    _send_custom(transport, protocol.MSG_CAPABILITY, version=protocol.PROTOCOL_VERSION)
    # Re-apply in case the RemoteClient was recreated since startup.
    try:
        apply_local_scripts()
    except Exception:
        log.exception("rsc: failed applying local-scripts on connect")


def _on_role_disconnected(transport: Any, is_leader: bool) -> None:
    tid = id(transport)
    role = _transport_roles.pop(tid, "leader" if is_leader else "follower")
    _nonce_trackers.pop(tid, None)
    _failure_limiters.pop(tid, None)
    _capability_processed.discard(tid)
    _session_consented_transports.discard(tid)
    _pending_consent_transports.discard(tid)
    _input_frozen_transports.discard(tid)
    _peer_muted_state.pop(tid, None)
    state_module.state.set_muted_by_remote(False)
    # Belt-and-braces: in addition to the state-listener path triggered
    # by set_muted_by_remote(False), directly force-apply SetMute(False)
    # on a freshly acquired audio-session volume reference. Guards
    # against the scenario where the listener / wx.CallAfter chain didn't
    # complete (e.g. because the cached volume reference went stale
    # mid-session) and the session was left muted past disconnect, which
    # would leave the controlled machine silent until NVDA restart.
    try:
        from . import audiomute
        wx.CallAfter(audiomute.force_unmute_now)
    except Exception:
        log.exception("rsc: deferred force_unmute_now schedule failed")
    log.info("rsc: %s transport disconnected (id=%x)", role, tid)


def _running_client() -> Optional[Any]:
    pkg = _remote_pkg
    if pkg is None:
        return None
    return getattr(pkg, "_remoteClient", None)


def _is_leader_for(transport: Any) -> Optional[bool]:
    tid = id(transport)
    role = _transport_roles.get(tid)
    if role == "leader":
        return True
    if role == "follower":
        return False
    rc = _running_client()
    if rc is None:
        return None
    if rc.leaderTransport is transport:
        return True
    if rc.followerTransport is transport:
        return False
    return None


# ---------------------------------------------------------------------------
# processKeyInput wrap — synth-ring speech preemption
# ---------------------------------------------------------------------------
#
# When NVDA Remote leader is F11'd into a follower, every ``SPEAK``
# forwarded from the follower is queued on the leader's local speech
# subsystem via ``LocalMachine.speak`` -> ``speech._manager.speak`` at
# ``Spri.NORMAL`` priority. ``ui.message`` (which the synth-settings-
# ring scripts use to announce the new setting / value) also speaks at
# ``Spri.NORMAL`` and does not preempt currently-playing speech. So if
# a forwarded utterance is being rendered when the user presses
# NVDA+(shift+)control+arrow, the synth-ring announcement queues behind
# it, roughly doubling the perceived response time and making it
# impossible to skim through ring options.
#
# Fix: when we detect a synth-ring local-script chord (``gesture.script``
# is one of the six ``script_*SynthSetting`` names AND it's in
# ``self.localScripts``), schedule ``speech.cancelSpeech`` via
# ``wx.CallAfter`` BEFORE the original ``processKeyInput`` schedules
# its own ``wx.CallAfter(script, gesture)``. ``wx.CallAfter`` is FIFO on
# the main thread, so the cancel lands first (clearing any forwarded
# speech in flight) and the script then queues its announcement onto an
# empty pipeline.
#
# Restricted to the synth-ring scripts by name so we never accidentally
# cancel forwarded speech the user actually wants to hear (e.g. a
# response announcement after the toggle-mute hotkey, which is also a
# local script).
#
# Cheap vk pre-filter to avoid the gesture construction on every
# keystroke. Covers the keys all six synth-ring scripts are bound to
# under NVDA's default desktop and laptop layouts: arrows for the
# regular increase/decrease/next/prev variants; pageUp/pageDown for the
# large-step variants.
_SYNTH_RING_CANDIDATE_VK = frozenset({
    0x21, 0x22,  # PageUp, PageDown
    0x25, 0x26, 0x27, 0x28,  # Left, Up, Right, Down
})

# Synth-settings-ring script names. The wrap pre-empts local speech
# (via ``speech.cancelSpeech``) when ``gesture.script.__name__`` matches
# one of these AND the script is in ``self.localScripts``. The same
# tuple is consumed below by ``_get_synth_ring_scripts`` /
# ``apply_local_scripts`` to populate ``rc.localScripts`` itself.
_SYNTH_RING_SCRIPT_NAMES = (
    "script_nextSynthSetting",
    "script_previousSynthSetting",
    "script_increaseSynthSetting",
    "script_decreaseSynthSetting",
    "script_increaseLargeSynthSetting",
    "script_decreaseLargeSynthSetting",
)
_SYNTH_RING_PREEMPT_NAMES = frozenset(_SYNTH_RING_SCRIPT_NAMES)

# vk code for ``B`` — NVDA's default report-battery-status chord is
# ``NVDA+shift+b``. Used as a cheap pre-filter so the gesture lookup
# only runs on candidate keys rather than every keystroke.
_BATTERY_CANDIDATE_VK = 0x42


def _make_patched_process_key_input(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def wrapper(self, vkCode=None, scanCode=None, extended=None, pressed=None):
        # Battery announcement orchestration. Only meaningful when the
        # user has the feature on, the chord is a candidate, and they're
        # F11'd in. If those all hold AND the resolved gesture is the
        # battery script AND it's in localScripts (added by
        # apply_local_scripts when the feature is enabled), we bypass
        # the original processKeyInput entirely — the orchestration
        # decides what to speak based on the chosen announcement order.
        if (
            pressed
            and vkCode == _BATTERY_CANDIDATE_VK
            and getattr(self, "sendingKeys", False)
            and config_spec.get_announce_local_battery_on_remote()
        ):
            try:
                from keyboardHandler import KeyboardInputGesture
                gp = KeyboardInputGesture(
                    self.keyModifiers, vkCode, scanCode, extended,
                )
                if not gp.isModifier:
                    script_p = gp.script
                    if (
                        _is_battery_script(script_p)
                        and script_p in self.localScripts
                    ):
                        _orchestrate_battery_announcement(self)
                        return False  # block default forwarding
            except Exception:
                log.exception("rsc: battery orchestration failed")

        # Bail out fast for the overwhelming majority of keystrokes
        # that can't possibly be a synth-ring chord: not pressed, not
        # in remote-control mode, or not one of the candidate vk codes.
        if (
            pressed
            and vkCode in _SYNTH_RING_CANDIDATE_VK
            and getattr(self, "sendingKeys", False)
        ):
            try:
                from keyboardHandler import KeyboardInputGesture
                gp = KeyboardInputGesture(
                    self.keyModifiers, vkCode, scanCode, extended,
                )
                if not gp.isModifier:
                    script_p = gp.script
                    if (
                        script_p is not None
                        and getattr(script_p, "__name__", None) in _SYNTH_RING_PREEMPT_NAMES
                        and script_p in self.localScripts
                    ):
                        import speech as _speech_mod
                        wx.CallAfter(_speech_mod.cancelSpeech)
            except Exception:
                log.exception("rsc: synth-ring speech preempt failed")
        return original(
            self,
            vkCode=vkCode,
            scanCode=scanCode,
            extended=extended,
            pressed=pressed,
        )
    return wrapper


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _dispatch_custom(transport: Any, msg_type: str, payload: Dict[str, Any]) -> None:
    log.info("rsc: inbound %s", msg_type)
    try:
        if msg_type == protocol.MSG_CAPABILITY:
            _on_capability(transport, **payload)
        elif msg_type == protocol.MSG_MUTE_REQUEST:
            _on_mute_request(transport, payload)
        elif msg_type == protocol.MSG_UNMUTE_REQUEST:
            _on_unmute_request(transport, payload)
        elif msg_type == protocol.MSG_STATE:
            _on_state(transport, **payload)
        elif msg_type == protocol.MSG_CONSENT_PENDING:
            _on_consent_pending(transport, **payload)
        elif msg_type == protocol.MSG_BATTERY_REQUEST:
            _on_battery_request(transport, **payload)
        elif msg_type == protocol.MSG_BATTERY_RESPONSE:
            _on_battery_response(transport, **payload)
        else:
            log.debug("rsc: unknown custom type %s", msg_type)
    except Exception:
        log.exception("rsc: dispatching %s failed", msg_type)


def _on_consent_pending(transport: Any, **_: Any) -> None:
    if _is_leader_for(transport) is not True:
        return
    wx.CallAfter(_announce, "Waiting for remote user to allow mute. Remote input is paused.")


def _on_capability(transport: Any, version: Any = None, **_: Any) -> None:
    try:
        peer_version = int(version) if version is not None else 0
    except (TypeError, ValueError):
        peer_version = 0
    if peer_version != protocol.PROTOCOL_VERSION:
        log.warning("rsc: peer protocol version mismatch (got %r)", version)
        return
    tid = id(transport)
    if tid in _capability_processed:
        log.debug("rsc: duplicate capability for transport %x; ignoring", tid)
        return
    _capability_processed.add(tid)
    role = _is_leader_for(transport)
    log.info("rsc: peer capability received; my role=%s", role)
    if role is True:
        password = config_spec.get_password()
        if not password:
            log.info("rsc: peer supports muting but no password configured locally")
            return
        if config_spec.get_auto_request():
            log.info("rsc: auto-request enabled; sending mute_request")
            _send_mute_request(transport)
        else:
            wx.CallAfter(_prompt_to_mute, transport)


def _prompt_to_mute(transport: Any) -> None:
    if not getattr(transport, "connected", False):
        return
    try:
        import gui
        parent = gui.mainFrame
    except Exception:
        parent = None
    dlg = wx.MessageDialog(
        parent,
        "Mute speech on the remote machine while you control it from here?",
        "Remote Speech Control",
        style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
    )
    try:
        choice = dlg.ShowModal()
    finally:
        dlg.Destroy()
    if choice == wx.ID_YES:
        _send_mute_request(transport)


def _send_mute_request(transport: Any) -> None:
    password = config_spec.get_password()
    if not password:
        return
    req = protocol.make_request(password, protocol.ACTION_MUTE)
    _send_custom(transport, protocol.MSG_MUTE_REQUEST, **req)


def _send_unmute_request(transport: Any) -> None:
    password = config_spec.get_password()
    if not password:
        return
    req = protocol.make_request(password, protocol.ACTION_UNMUTE)
    _send_custom(transport, protocol.MSG_UNMUTE_REQUEST, **req)


def _on_mute_request(transport: Any, request: Dict[str, Any]) -> None:
    if _is_leader_for(transport) is True:
        log.warning("rsc: mute_request reached leader; ignoring")
        return
    tid = id(transport)
    limiter = _failure_limiters.get(tid)
    nonces = _nonce_trackers.get(tid)
    if limiter is None or nonces is None:
        log.warning("rsc: mute_request on untracked transport; ignoring")
        return
    if limiter.is_locked():
        log.warning("rsc: auth locked out; dropping mute_request")
        return
    password = config_spec.get_password()
    if not password:
        return
    if not protocol.verify_request(password, request, nonces):
        limiter.record_failure()
        log.warning("rsc: mute_request authentication failed")
        return
    limiter.reset()
    # Authentication passed. Apply controlled-side consent: if the user has
    # ticked "Allow speech to be automatically muted by controlling machine"
    # we mute straight away, otherwise we prompt the local user and only
    # mute if they explicitly accept. A "yes" is remembered for the rest of
    # this session so a controller with auto-request doesn't re-prompt on
    # every connect.
    if config_spec.get_allow_auto_mute() or tid in _session_consented_transports:
        _apply_mute(transport, "pre-authorised by local user")
        return
    if tid in _pending_consent_transports:
        log.info("rsc: consent prompt already pending for this transport")
        return
    _pending_consent_transports.add(tid)
    # Freeze all inbound input from the controller until the local user
    # has answered. Otherwise the controller could simply send Tab+Space
    # over NVDA Remote and click "yes" on the confirmation themselves.
    _input_frozen_transports.add(tid)
    log.info("rsc: consent prompt pending; freezing inbound input from controller")
    _send_custom(transport, protocol.MSG_CONSENT_PENDING)
    wx.CallAfter(_prompt_to_allow_mute, transport)


def _on_unmute_request(transport: Any, request: Dict[str, Any]) -> None:
    if _is_leader_for(transport) is True:
        return
    tid = id(transport)
    limiter = _failure_limiters.get(tid)
    nonces = _nonce_trackers.get(tid)
    if limiter is None or nonces is None:
        return
    if limiter.is_locked():
        return
    password = config_spec.get_password()
    if not password:
        return
    if not protocol.verify_request(password, request, nonces):
        limiter.record_failure()
        return
    limiter.reset()
    state_module.state.set_muted_by_remote(False)
    log.info("rsc: unmuted by remote (authenticated)")
    _send_custom(transport, protocol.MSG_STATE, muted=False)


def _on_state(transport: Any, muted: Any = False, denied: Any = False, **_: Any) -> None:
    if _is_leader_for(transport) is not True:
        return
    tid = id(transport)
    if bool(denied):
        msg = "Remote machine declined the mute request"
    elif bool(muted):
        msg = "Remote machine speech muted"
        _peer_muted_state[tid] = True
    else:
        msg = "Remote machine speech unmuted"
        _peer_muted_state[tid] = False
    wx.CallAfter(_announce, msg)


def _apply_mute(transport: Any, reason: str) -> None:
    tid = id(transport)
    _pending_consent_transports.discard(tid)
    state_module.state.set_muted_by_remote(True)
    log.info("rsc: muted by remote (%s)", reason)
    _send_custom(transport, protocol.MSG_STATE, muted=True)


def _prompt_to_allow_mute(transport: Any) -> None:
    tid = id(transport)
    if not getattr(transport, "connected", False):
        _pending_consent_transports.discard(tid)
        _input_frozen_transports.discard(tid)
        return
    try:
        import gui
        parent = gui.mainFrame
    except Exception:
        parent = None
    dlg = wx.MessageDialog(
        parent,
        (
            "The controller has requested to mute speech on this machine. "
            "Remote input is paused until you answer. "
            "Allow muting for this session?"
        ),
        "Remote Speech Control",
        style=wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
    )
    try:
        choice = dlg.ShowModal()
    finally:
        dlg.Destroy()
    _pending_consent_transports.discard(tid)
    _input_frozen_transports.discard(tid)
    log.info("rsc: consent prompt resolved; remote input flowing again")
    if not getattr(transport, "connected", False):
        return
    if choice == wx.ID_YES:
        _session_consented_transports.add(tid)
        _apply_mute(transport, "consented by local user")
    else:
        log.info("rsc: local user declined mute request")
        _send_custom(transport, protocol.MSG_STATE, muted=False, denied=True)


def _announce(text: str) -> None:
    try:
        import ui
        ui.message(text)
    except Exception:
        log.warning("rsc: ui.message failed", exc_info=True)


# ---------------------------------------------------------------------------
# Battery announcement
# ---------------------------------------------------------------------------
#
# NVDA's NVDA+shift+B chord is bound to ``globalCommands.script_say_battery_status``
# which delegates to ``winAPI._powerTracking.reportCurrentBatteryStatus``.
# That function builds a localized announcement string from
# ``GetSystemPowerStatus()`` and speaks it via ``ui.message``.
#
# To mirror NVDA's wording exactly (correct locale, correct pluralisation,
# correct "Plugged in"/"Unplugged"/"X hours and Y minutes remaining"
# phrasing) we don't reimplement the formatting — we invoke NVDA's own
# function with ``ui.message`` temporarily monkey-patched to a capture
# callable instead of running it. The capture is scoped to a single
# synchronous call and restored immediately afterwards, so no other speech
# is intercepted.

def _capture_battery_text() -> str:
    """Call NVDA's own report-battery-status logic and capture exactly
    what it would have spoken, without speaking it.

    Returns the captured string, or empty string on any failure (which
    causes the caller to skip the announcement rather than speak something
    misleading).
    """
    try:
        import ui as ui_module
        from winAPI._powerTracking import reportCurrentBatteryStatus
    except Exception:
        log.exception("rsc: battery query — import failed")
        return ""

    captured: List[str] = []

    def _capture(text: str, *args: Any, **kwargs: Any) -> None:
        captured.append(text)

    original_message = ui_module.message
    ui_module.message = _capture
    try:
        reportCurrentBatteryStatus()
    except Exception:
        log.exception("rsc: battery query — reportCurrentBatteryStatus raised")
    finally:
        ui_module.message = original_message

    return captured[0] if captured else ""


def _get_battery_script() -> Optional[Any]:
    """Return NVDA's battery-status script as a bound method on
    ``globalCommands.commands``, or None if it can't be located."""
    try:
        import globalCommands
    except Exception:
        log.exception("rsc: globalCommands import failed")
        return None
    commands = getattr(globalCommands, "commands", None)
    if commands is None:
        return None
    return getattr(commands, "script_say_battery_status", None)


def _is_battery_script(script: Any) -> bool:
    if script is None:
        return False
    return getattr(script, "__name__", "") == "script_say_battery_status"


def _speak_local_battery() -> None:
    text = _capture_battery_text()
    if text:
        _announce("Local " + text)


def _speak_remote_battery(text: str) -> None:
    if text:
        _announce("Remote " + text)


def _send_battery_request(transport: Any) -> None:
    _send_custom(transport, protocol.MSG_BATTERY_REQUEST)


def _send_battery_response(transport: Any, text: str) -> None:
    _send_custom(transport, protocol.MSG_BATTERY_RESPONSE, text=text)


def _on_battery_request(transport: Any, **_: Any) -> None:
    """Controlled side: respond with NVDA's exact battery announcement text.

    We deliberately don't gate this on auth — battery status is non-sensitive
    and anyone with channel access already has stronger access. We also
    don't gate on the controlled-side setting: if the controller asked,
    answer. The controller decides what to do with it.
    """
    if _is_leader_for(transport) is True:
        # The leader sent a request to itself somehow — ignore.
        return
    text = _capture_battery_text()
    _send_battery_response(transport, text)


def _on_battery_response(transport: Any, text: str = "", **_: Any) -> None:
    """Controller side: announce the remote's text, optionally followed by
    the local battery if the user chose ``remote_then_local`` ordering."""
    if _is_leader_for(transport) is not True:
        return
    tid = id(transport)
    _battery_request_pending.discard(tid)
    _speak_remote_battery(text)
    if tid in _pending_post_remote_local_battery:
        _pending_post_remote_local_battery.discard(tid)
        wx.CallAfter(_speak_local_battery)


def _on_battery_timeout(tid: int) -> None:
    """Wired via ``wx.CallLater`` from ``_orchestrate_battery_announcement``.

    Fires unconditionally after ``BATTERY_REPLY_TIMEOUT_MS``; the
    ``_battery_request_pending`` membership check is how we tell whether
    the response already arrived (in which case this is a no-op) or the
    remote went silent. In the silent case, fall back: if the user was
    expecting a follow-up local announcement (``remote_then_local`` mode),
    speak it now so they aren't left without any answer at all.
    """
    if tid not in _battery_request_pending:
        return  # response arrived in time
    _battery_request_pending.discard(tid)
    log.warning("rsc: remote battery request timed out (tid=%x)", tid)
    if tid in _pending_post_remote_local_battery:
        _pending_post_remote_local_battery.discard(tid)
        wx.CallAfter(_speak_local_battery)


def _orchestrate_battery_announcement(rc: Any) -> None:
    """Decide what to do when the user presses the battery chord while
    F11'd in. Invoked from the processKeyInput wrap."""
    leader = getattr(rc, "leaderTransport", None)
    if leader is None or not getattr(leader, "connected", False):
        # No live remote transport — just speak the local battery as a
        # graceful fallback.
        wx.CallAfter(_speak_local_battery)
        return

    mode = config_spec.get_battery_announcement_mode()
    tid = id(leader)

    if mode == "local_only":
        wx.CallAfter(_speak_local_battery)
        return

    if mode == "local_then_remote":
        wx.CallAfter(_speak_local_battery)
        _battery_request_pending.add(tid)
        _send_battery_request(leader)
        try:
            wx.CallLater(BATTERY_REPLY_TIMEOUT_MS, _on_battery_timeout, tid)
        except Exception:
            log.exception("rsc: scheduling battery timeout failed")
        return

    if mode == "remote_then_local":
        _pending_post_remote_local_battery.add(tid)
        _battery_request_pending.add(tid)
        _send_battery_request(leader)
        try:
            wx.CallLater(BATTERY_REPLY_TIMEOUT_MS, _on_battery_timeout, tid)
        except Exception:
            log.exception("rsc: scheduling battery timeout failed")
        return

    # Unknown / invalid mode value — default to local-only as the safe
    # behaviour (always produces some audible output).
    log.warning("rsc: unknown battery announcement mode %r; defaulting to local-only", mode)
    wx.CallAfter(_speak_local_battery)


# _SYNTH_RING_SCRIPT_NAMES is defined near the top of the file
# (next to the processKeyInput wrap) because that wrap consumes it
# for synth-ring speech preemption. ``_get_synth_ring_scripts`` /
# ``apply_local_scripts`` below reference the same tuple.

# Scripts that should always run locally on the controller, never be
# forwarded to the controlled side. Registered by callers (typically the
# GlobalPlugin) via ``register_persistent_local_script``; rolled into
# the RemoteClient's ``localScripts`` set every time ``apply_local_scripts``
# runs.
_persistent_local_scripts: List[Any] = []

def register_persistent_local_script(script: Any) -> None:
    """Add a bound script method to the always-local set.

    Use this for scripts whose forwarded-to-remote variant is wrong — e.g.
    the toggle-mute hotkey: pressing it on the controller should always
    open the controller's yes/no dialog and send an authenticated request,
    never get forwarded to the controlled side and run with controlled-side
    semantics.

    Idempotent. Triggers an ``apply_local_scripts`` so the registration
    takes effect immediately if the RemoteClient is already up.
    """
    if script in _persistent_local_scripts:
        return
    _persistent_local_scripts.append(script)
    try:
        wx.CallAfter(apply_local_scripts)
    except Exception:
        log.exception("rsc: deferred apply_local_scripts after registration failed")


def _get_synth_ring_scripts() -> List[Any]:
    """Return the bound methods on NVDA's GlobalCommands singleton that
    implement the synth-settings-ring gestures, or an empty list if any
    of the lookups fail."""
    try:
        import globalCommands
    except Exception:
        log.exception("rsc: globalCommands import failed")
        return []
    commands = getattr(globalCommands, "commands", None)
    if commands is None:
        log.warning("rsc: globalCommands.commands singleton not found")
        return []
    scripts = []
    for name in _SYNTH_RING_SCRIPT_NAMES:
        s = getattr(commands, name, None)
        if s is not None:
            scripts.append(s)
    if not scripts:
        log.warning("rsc: no synth-settings-ring scripts located")
    return scripts


def apply_local_scripts() -> None:
    """Reconcile the running RemoteClient's ``localScripts`` set with our
    requirements.

    Two sources of "local" scripts:

    1. Persistent scripts registered via ``register_persistent_local_script``
       — always added. Used by the toggle-mute hotkey so that even when the
       controller is F11'd into remote-control mode, pressing the hotkey
       runs the controller-side script (opens the yes/no dialog) instead
       of being forwarded as a keystroke to the controlled side.

    2. The synth-settings-ring scripts — added only if the user has the
       "Synth settings ring adjusts this machine, not the remote" setting
       ticked.

    3. NVDA's report-battery-status script — added only if the user has
       the "Announce local machine battery status when querying battery
       from a remote session" setting ticked. The actual orchestration
       (deciding whether to also fetch and announce the remote's battery)
       is in the processKeyInput wrap; this just makes sure the chord
       doesn't get forwarded to the remote when the user has the feature
       enabled.

    NVDA Remote's ``processKeyInput`` already runs ``localScripts`` entries
    on the local machine and skips forwarding them, so reconciling this
    set is all we need to do.
    """
    rc = _running_client()
    if rc is None:
        log.debug("rsc: apply_local_scripts — no running client yet")
        return
    local_scripts = getattr(rc, "localScripts", None)
    if local_scripts is None:
        log.warning("rsc: RemoteClient has no localScripts attribute")
        return
    # Persistent scripts — always local.
    for s in _persistent_local_scripts:
        local_scripts.add(s)
    if _persistent_local_scripts:
        log.info("rsc: %d persistent local script(s) registered", len(_persistent_local_scripts))
    # Conditional: synth settings ring.
    synth_ring = _get_synth_ring_scripts()
    if synth_ring:
        if config_spec.get_keep_synth_settings_ring_local():
            for s in synth_ring:
                local_scripts.add(s)
            log.info("rsc: %d synth-settings-ring scripts kept local", len(synth_ring))
        else:
            for s in synth_ring:
                local_scripts.discard(s)
            log.info("rsc: synth-settings-ring scripts allowed to forward to remote")
    # Conditional: NVDA's report-battery-status script.
    battery_script = _get_battery_script()
    if battery_script is not None:
        if config_spec.get_announce_local_battery_on_remote():
            local_scripts.add(battery_script)
            log.info("rsc: battery-status script kept local for custom announcement")
        else:
            local_scripts.discard(battery_script)
            log.info("rsc: battery-status script allowed to forward to remote")


def _rebind_dispatch_handlers() -> None:
    """Re-register our current wrappers on every weakref-backed
    extension-point binding that captured a bound method against a
    class attribute we class-patch.

    Background: NVDA stores extension-point handlers via
    ``BoundMethodWeakref``, which holds weak references to both the
    instance and the underlying function. When we class-patch
    ``LocalMachine.sendKey`` (et al.), the previously registered bound
    method captures *our wrap function* as its ``__func__``. As soon as
    our ``uninstall()`` puts the original back on the class, nothing
    strong-references our old wrap any more — Python collects it, the
    weakref dies, and on the next dispatch ``Action.notify`` /
    ``Decider.decide`` silently skips the dead entry. Symptom: the
    controlled machine stops accepting remote keystrokes (KEY inbound
    weakref dead) and/or the leader stops forwarding keys (decide_
    handleRawKey weakref dead) until the user reconnects.

    Two classes of binding need attention:

    1. ``RemoteSession.__init__`` and ``FollowerSession.__init__``
       capture bound methods via ``transport.registerInbound(...)``:
         * ``KEY``           ← ``localMachine.sendKey``         (follower)
         * ``BRAILLE_INPUT`` ← ``localMachine.brailleInput``    (follower)
         * ``CLIENT_JOINED`` ← ``session.handleClientConnected``(both sessions)
         * ``CLIENT_LEFT``   ← ``session.handleClientDisconnected``(both)

    2. ``RemoteClient.__init__`` captures a bound method via
       ``inputCore.decide_handleRawKey.register(self.processKeyInput)``.
       This is on the *leader* side and gates outbound key forwarding.

    Class-attribute patches that are *not* captured this way — e.g.
    ``TCPTransport.parse`` (resolved via ``self.parse`` at call time on
    every line) and the ``RemoteClient.onConnectedAs*`` callbacks — are
    unaffected because they're attribute-looked-up on each call, not
    weak-bound at construction.

    Safe to call any time: if no session / no client exists yet, the
    early ``None`` checks make it a no-op. The freshly registered bound
    method becomes a new strong-anchored entry alongside the dead one;
    ``Action.notify`` / ``Decider.decide`` skip dead weakrefs and fire
    the live one.
    """
    rc = _running_client()
    if rc is None:
        return
    try:
        protocol_mod = importlib.import_module("_remoteClient.protocol")
    except ImportError:
        log.warning("rsc: cannot import _remoteClient.protocol for handler rebind")
        return
    RemoteMessageType = getattr(protocol_mod, "RemoteMessageType", None)
    if RemoteMessageType is None:
        log.warning("rsc: RemoteMessageType missing; skipping handler rebind")
        return

    follower_bindings = (
        ("KEY", "localMachine", "sendKey"),
        ("BRAILLE_INPUT", "localMachine", "brailleInput"),
        ("CLIENT_JOINED", None, "handleClientConnected"),
        ("CLIENT_LEFT", None, "handleClientDisconnected"),
    )
    leader_bindings = (
        ("CLIENT_JOINED", None, "handleClientConnected"),
        ("CLIENT_LEFT", None, "handleClientDisconnected"),
    )

    for session_attr, bindings in (
        ("followerSession", follower_bindings),
        ("leaderSession", leader_bindings),
    ):
        session = getattr(rc, session_attr, None)
        if session is None:
            continue
        transport = getattr(session, "transport", None)
        if transport is None:
            continue
        inbound = getattr(transport, "inboundHandlers", None)
        if inbound is None:
            continue
        rebound_count = 0
        for msg_name, sub_attr, method_name in bindings:
            msg_type = getattr(RemoteMessageType, msg_name, None)
            if msg_type is None:
                continue
            action = inbound.get(msg_type)
            if action is None:
                # The session registered this handler at __init__, so
                # the entry must exist if the session is alive. If it
                # doesn't, skip silently rather than create one — we'd
                # be guessing at the right Action type.
                continue
            target = session if sub_attr is None else getattr(session, sub_attr, None)
            if target is None:
                continue
            handler = getattr(target, method_name, None)
            if handler is None:
                continue
            try:
                action.register(handler)
                rebound_count += 1
            except Exception:
                log.exception(
                    "rsc: rebind %s on %s failed", msg_name, session_attr,
                )
        if rebound_count:
            log.info(
                "rsc: rebound %d inbound handler(s) on %s after reload",
                rebound_count, session_attr,
            )

    # Leader-side outbound-key gate. Patching RemoteClient.processKeyInput
    # invalidates the weakref captured at RemoteClient.__init__:
    #   inputCore.decide_handleRawKey.register(self.processKeyInput)
    # Re-register the current bound method so leader-side key forwarding
    # keeps working after a mid-session reload.
    try:
        import inputCore  # NVDA's input core, always present in NVDA process
    except Exception:
        log.exception("rsc: inputCore import failed; processKeyInput rebind skipped")
        inputCore = None  # type: ignore[assignment]
    if inputCore is not None:
        decider = getattr(inputCore, "decide_handleRawKey", None)
        handler = getattr(rc, "processKeyInput", None)
        if decider is not None and handler is not None:
            try:
                decider.register(handler)
                log.info("rsc: rebound RemoteClient.processKeyInput on inputCore.decide_handleRawKey after reload")
            except Exception:
                log.exception("rsc: rebind processKeyInput on decide_handleRawKey failed")

    # Repopulate our protocol shadow state for the live transports.
    # uninstall() cleared _transport_roles, _nonce_trackers,
    # _failure_limiters and _capability_processed; without restoring
    # them, _on_mute_request / _on_unmute_request would hit the
    # "untracked transport; ignoring" early-return on the next inbound
    # request and the controlled side's user would see the controller's
    # mute hotkey silently do nothing. Fresh NonceTracker and
    # FailureLimiter are fine — the TCP connection is the same but our
    # auth state can legitimately restart from zero across a reload.
    # Marking the transport as already capability-processed prevents a
    # redundant capability exchange (the live session has already
    # agreed on protocol version).
    #
    # _peer_muted_state is deliberately NOT restored — we don't know
    # the peer's current mute state after our reload wiped it. The
    # first MSG_STATE we receive (in response to the next mute_request
    # / unmute_request, or unsolicited if the peer sends one) will
    # repopulate it correctly. _session_consented_transports is also
    # not restored — re-prompting for consent on a fresh mute_request
    # is the security-positive default.
    for transport, role in (
        (getattr(rc, "leaderTransport", None), "leader"),
        (getattr(rc, "followerTransport", None), "follower"),
    ):
        if transport is None or not getattr(transport, "connected", False):
            continue
        tid = id(transport)
        if tid in _transport_roles:
            continue
        _transport_roles[tid] = role
        _nonce_trackers[tid] = protocol.NonceTracker()
        _failure_limiters[tid] = protocol.FailureLimiter()
        _capability_processed.add(tid)
        log.info(
            "rsc: restored protocol shadow state for %s transport after reload (id=%x)",
            role, tid,
        )


def toggle_mute_action() -> str:
    """Handle the toggle-mute hotkey on the controlling side.

    The hotkey is meaningful only when:

    * we have an active leader transport (we are controlling someone), and
    * we are currently in remote-control mode (NVDA Remote's
      ``RemoteClient.sendingKeys`` flag is True, i.e. the user has F11'd
      into the controlled machine).

    Under those conditions, sends an authenticated mute_request or
    unmute_request directly — no confirmation dialog on the controller
    side, because (a) pressing the hotkey is already the confirmation,
    and (b) while the user is F11'd in, their keystrokes go to the
    remote machine, so they couldn't navigate a local dialog anyway.

    Mute direction is chosen from ``_peer_muted_state``, our mirror of
    the controlled side's mute state from inbound ``MSG_STATE``
    acknowledgements. The controlled side's consent flow still applies
    to mute requests; if the user there hasn't ticked "Allow auto-mute",
    they'll see the standard yes/no prompt and remote input will freeze
    until they answer. Unmute requests are applied immediately by the
    controlled side with no prompt.

    Returns an empty string in the success case — the round-trip is
    fast enough that the intermediate "sending..." announcement was
    only ever stepping on the end-state announcement that arrives via
    ``MSG_STATE``. The user hears just the final "Remote machine speech
    muted" / "Remote machine speech unmuted" / "Remote machine declined
    the mute request". Returns a short status string only when the
    hotkey doesn't apply (no session / not in remote-control mode).

    By design the controlled side has no hotkey: the controlled user
    unmutes themselves implicitly by pressing any key (ping-pong via the
    LL hook). To mute, they ask the controller to toggle.
    """
    rc = _running_client()
    if rc is None:
        return "No remote session"
    leader = getattr(rc, "leaderTransport", None)
    if leader is None or not getattr(leader, "connected", False):
        return "Not currently controlling a remote machine"
    if not bool(getattr(rc, "sendingKeys", False)):
        # We're connected as leader but not currently driving — F11'd
        # back to local control. Per the user's design: hotkey is a
        # no-op in this state.
        return "Not currently controlling a remote machine"

    tid = id(leader)
    currently_muted = _peer_muted_state.get(tid, False)
    if currently_muted:
        log.info("rsc: toggle-mute hotkey; sending unmute_request")
        _send_unmute_request(leader)
    else:
        log.info("rsc: toggle-mute hotkey; sending mute_request")
        _send_mute_request(leader)
    return ""
