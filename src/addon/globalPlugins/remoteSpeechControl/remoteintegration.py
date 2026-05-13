"""Glue between Remote Speech Control and NVDA's bundled ``_remoteClient`` package.

We make three monkey patches:

1. ``TCPTransport.parse`` — intercept inbound messages whose ``type``
   begins with our ``remoteSpeechControl_`` prefix and dispatch them to our handlers
   before the original parser tries ``RemoteMessageType(...)`` (which
   would reject and drop the message).

2. ``RemoteClient.onConnectedAsLeader`` / ``onConnectedAsFollower`` /
   ``onDisconnectedAsLeader`` / ``onDisconnectedAsFollower`` — hook our
   connect/disconnect setup directly into NVDA Remote's role lifecycle.
   We deliberately use these rather than the base ``Transport.__init__``
   because the latter doesn't reliably fire for the ``RelayTransport``
   subclass in current NVDA builds, while the role callbacks are part of
   ``_remoteClient``'s public flow and always run.

3. ``LocalMachine.sendKey`` — open the keystroke-injection window in
   inputmonitor so the low-level keyboard hook attributes the next
   injected key correctly.

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
from . import inputmonitor
from . import logger
from . import protocol
from . import state as state_module

log = logger.get()


_remote_pkg: Optional[Any] = None
_transport_module: Optional[Any] = None
_local_machine_module: Optional[Any] = None
_client_module: Optional[Any] = None
_session_module: Optional[Any] = None

_patches: List[Tuple[Any, str, Any]] = []
_per_transport_disconnect_handlers: Dict[int, Callable[[], None]] = {}
_nonce_trackers: Dict[int, protocol.NonceTracker] = {}
_failure_limiters: Dict[int, protocol.FailureLimiter] = {}
_transport_roles: Dict[int, str] = {}
_capability_processed: set = set()
_session_consented_transports: set = set()
_pending_consent_transports: set = set()
# While non-empty, all inbound replay paths on the controlled side
# (sendKey, brailleInput) are dropped — the controller is locked out
# of driving the machine until the local user resolves the mute prompt.
_input_frozen_transports: set = set()


def install() -> None:
    global _remote_pkg, _transport_module, _local_machine_module, _client_module, _session_module
    try:
        _remote_pkg = importlib.import_module("_remoteClient")
        _transport_module = importlib.import_module("_remoteClient.transport")
        _local_machine_module = importlib.import_module("_remoteClient.localMachine")
        _client_module = importlib.import_module("_remoteClient.client")
        _session_module = importlib.import_module("_remoteClient.session")
    except ImportError:
        log.warning("rsc: _remoteClient not present in this NVDA build; pairing disabled")
        return

    _patch(_transport_module.TCPTransport, "parse", _make_patched_parse)
    _patch(_local_machine_module.LocalMachine, "sendKey", _make_patched_send_key)
    if hasattr(_local_machine_module.LocalMachine, "brailleInput"):
        _patch(_local_machine_module.LocalMachine, "brailleInput", _make_patched_braille_input)

    if hasattr(_session_module.RemoteSession, "handleClientConnected"):
        _patch(_session_module.RemoteSession, "handleClientConnected", _make_patched_handle_client_connected)
    else:
        log.warning("rsc: RemoteSession has no handleClientConnected; late-joiners won't be paired")

    rc_class = _client_module.RemoteClient
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

    log.info("rsc: _remoteClient integration installed")
    # The RemoteClient instance may not yet exist at our install() time
    # (NVDA initialises us first, then _remoteClient.initialize() runs).
    # Defer until the wx loop is idle so the singleton is ready.
    try:
        wx.CallAfter(apply_keep_synth_ring_local)
    except Exception:
        log.exception("rsc: deferred apply_keep_synth_ring_local schedule failed")


def uninstall() -> None:
    while _patches:
        owner, attr, original = _patches.pop()
        try:
            setattr(owner, attr, original)
        except Exception:
            log.exception("rsc: failed restoring %r.%s", owner, attr)
    _per_transport_disconnect_handlers.clear()
    _nonce_trackers.clear()
    _failure_limiters.clear()
    _transport_roles.clear()
    _capability_processed.clear()
    _session_consented_transports.clear()
    _pending_consent_transports.clear()
    _input_frozen_transports.clear()
    state_module.state.set_muted_by_remote(False)


def _patch(owner: Any, attr: str, factory: Callable[[Any], Any]) -> None:
    original = getattr(owner, attr)
    replacement = factory(original)
    setattr(owner, attr, replacement)
    _patches.append((owner, attr, original))


# ---------------------------------------------------------------------------
# Inbound interception
# ---------------------------------------------------------------------------

def _make_patched_parse(original: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(original)
    def parse(self, line: bytes) -> None:
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
# Outbound + injection-window patches
# ---------------------------------------------------------------------------

def _make_patched_send_key(original: Callable[..., Any]) -> Callable[..., Any]:
    # The original sendKey MUST run for every call we receive — a single
    # missed key-up event leaves a modifier (Shift, Ctrl, Alt) stuck on
    # the controlled machine. Wrap our pre-action in its own try/except
    # so an unexpected failure in our injection-window bookkeeping can
    # never drop the keystroke.
    @functools.wraps(original)
    def send_key(self, *args, **kwargs):
        if _input_frozen_transports:
            # A consent prompt is pending on this machine; drop all
            # inbound input from the controller until the local user
            # has answered, so the controller cannot drive the
            # confirmation dialog themselves.
            return None
        try:
            inputmonitor.open_injection_window()
        except Exception:
            log.exception("rsc: open_injection_window failed; sendKey continuing")
        return original(self, *args, **kwargs)
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
        apply_keep_synth_ring_local()
    except Exception:
        log.exception("rsc: failed applying keep-synth-ring-local on connect")


def _on_role_disconnected(transport: Any, is_leader: bool) -> None:
    tid = id(transport)
    role = _transport_roles.pop(tid, "leader" if is_leader else "follower")
    _nonce_trackers.pop(tid, None)
    _failure_limiters.pop(tid, None)
    _capability_processed.discard(tid)
    _session_consented_transports.discard(tid)
    _pending_consent_transports.discard(tid)
    _input_frozen_transports.discard(tid)
    state_module.state.set_muted_by_remote(False)
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
    if bool(denied):
        msg = "Remote machine declined the mute request"
    elif bool(muted):
        msg = "Remote machine speech muted"
    else:
        msg = "Remote machine speech unmuted"
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


_SYNTH_RING_SCRIPT_NAMES = (
    "script_nextSynthSetting",
    "script_previousSynthSetting",
    "script_increaseSynthSetting",
    "script_decreaseSynthSetting",
    "script_increaseLargeSynthSetting",
    "script_decreaseLargeSynthSetting",
)


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


def apply_keep_synth_ring_local() -> None:
    """Add or remove the synth-settings-ring scripts from the running
    RemoteClient's ``localScripts`` set per the user's setting.

    NVDA Remote's ``processKeyInput`` already runs ``localScripts`` entries
    on the local machine and skips forwarding them to the remote — so all
    we have to do is keep our four (six including large-step variants)
    scripts in/out of that set."""
    rc = _running_client()
    if rc is None:
        log.debug("rsc: apply_keep_synth_ring_local — no running client yet")
        return
    local_scripts = getattr(rc, "localScripts", None)
    if local_scripts is None:
        log.warning("rsc: RemoteClient has no localScripts attribute")
        return
    scripts = _get_synth_ring_scripts()
    if not scripts:
        return
    if config_spec.get_keep_synth_settings_ring_local():
        for s in scripts:
            local_scripts.add(s)
        log.info("rsc: %d synth-settings-ring scripts kept local", len(scripts))
    else:
        for s in scripts:
            local_scripts.discard(s)
        log.info("rsc: synth-settings-ring scripts allowed to forward to remote")


def request_unmute_for_active_session() -> None:
    rc = _running_client()
    if rc is None:
        return
    for transport in (rc.leaderTransport, rc.followerTransport):
        if transport is None or not getattr(transport, "connected", False):
            continue
        if _is_leader_for(transport) is True:
            _send_unmute_request(transport)
