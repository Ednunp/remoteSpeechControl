# Remote Speech Control — handover

NVDA add-on that mutes local NVDA speech on a machine being controlled by NVDA Remote, while a remote keyboard is driving NVDA. The instant a real keypress is detected on the controlled machine, speech resumes (ping-pong). Both ends must have this add-on installed and the same shared password configured.

## Layout

- `src/addon/` is the unzipped add-on. The `.nvda-addon` file is just this folder zipped, with `manifest.ini` at the archive root.
- `src/addon/globalPlugins/remoteSpeechControl/` is the Python package NVDA loads.
- `build.py` produces `dist/remoteSpeechControl-<version>.nvda-addon`. (PowerShell scripts are blocked by exec policy on this workspace, so the builder is Python.)

## How it works at a glance

1. **`synthwedge.py`** wraps the active synth driver's `speak()` method. NVDA Remote captures speech upstream at `speech.speak`, *before* control reaches the synth driver — so dropping at the synth-driver level silences local audio without affecting what the controller hears. Re-wraps on `synthDriverHandler.synthChanged`.
2. **`inputmonitor.py`** installs a `WH_KEYBOARD_LL` low-level keyboard hook via ctypes. NVDA Remote injects keystrokes via raw `SendInput()` with no extra-info sentinel, but Windows sets `LLKHF_INJECTED` in the hook struct's flags field for any synthesised input. Combined with a short "injection window" opened by `remoteintegration.py` whenever `LocalMachine.sendKey` runs, we attribute keystrokes correctly: physical keys clear remote-driving; injected keys inside the window set it; injected keys outside the window (some other tool) are ignored.
3. **`state.py`** holds the two-flag state machine: `muted_by_remote` (auth-armed) and `remote_driving` (ping-pong). Speech is dropped iff both are true.
4. **`protocol.py`** does HMAC-SHA256 of (action, nonce, timestamp) under a PBKDF2-derived key. Verifies with `compare_digest`, +/- 30 s timestamp window, sliding-window nonce tracker, per-session failure-rate limiter. All custom message types share a `remoteSpeechControl_` wire-prefix.
5. **`remoteintegration.py`** does three class-level monkey-patches inside NVDA's bundled `_remoteClient` package: `Transport.parse` (intercept `remoteSpeechControl_*` messages before the closed `RemoteMessageType` enum rejects them), `Transport.__init__` (register on connect/disconnect actions for every new transport), `LocalMachine.sendKey` (open the injection window). Sending bypasses the typed `Transport.send` and writes directly to `transport.queue` after running through the existing serializer.
6. **`settings.py`** registers an "Remote Speech Control" panel inside NVDA's Settings dialog with three controls: password (Alt+P), auto-request mute on connect (Alt+A), verbose logging (Alt+V).
7. **`__init__.py`** is the GlobalPlugin. Wires everything together and registers the `NVDA+shift+u` force-unmute gesture (rebindable in NVDA's Input gestures dialog).

## Security notes

- Shared password never leaves either machine; only HMACs cross the wire.
- PBKDF2-HMAC-SHA256, 100 000 iterations, fixed salt — the salt's job is just to make brute-force require recomputation, not to protect against rainbow tables (no per-user uniqueness possible without a handshake).
- `+/- 30 s` timestamp window plus per-session nonce tracker (90 s sliding window) blocks replay.
- Bad MACs are silently rejected (no oracle), and a per-session failure-rate limiter locks out further auth for 60 s after 3 failures.
- Empty password = mute disabled entirely. Both sides must have the same non-empty password to pair.

## Consent freeze (implemented in 0.3.0)

When an authenticated `mute_request` arrives on the controlled side AND `allowAutoMute` is unticked, the transport id is added to `_input_frozen_transports`. The wrapped `LocalMachine.sendKey` and `LocalMachine.brailleInput` short-circuit when that set is non-empty, so the controller cannot drive the consent dialog themselves. A `remoteSpeechControl_consent_pending` message is sent back to the controller, whose NVDA announces "Waiting for remote user to allow mute. Remote input is paused." once. The freeze clears the moment the local user resolves the dialog (yes or no) or the transport disconnects.

Ticking "Allow speech to be automatically muted" on the controlled side bypasses both the prompt and the freeze — authenticated mute requests apply immediately. This is the escape hatch for legitimate unattended remote control of one's own machines.

## Known unknowns / TODO before first real use

The integration is pinned to NVDA's bundled `_remoteClient` package as it stands in NVDA core (`source/_remoteClient/`). The relevant attribute paths verified against the public NVAccess source:

- `_remoteClient.transport.Transport.parse` (line ~347), `Transport.__init__`, `Transport.transportConnected` / `transportDisconnected` (extensionPoints.Action), `Transport.serializer.serialize(type=..., **kw)`, `Transport.queue.put(bytes)`.
- `_remoteClient.localMachine.LocalMachine.sendKey(vk_code, extended, pressed)`.
- `_remoteClient._remoteClient` module-level singleton (instance of `_remoteClient.client.RemoteClient`); `client.leaderTransport` / `client.followerTransport` for role detection.
- `_remoteClient.protocol.RemoteMessageType` is `StrEnum`, so wire `type` field is a plain string.

After the first install on each machine, look in NVDA's log (Tools → View log) for these `rsc:` lines:

- `_remoteClient integration installed` — module path and class patches resolved.
- `WH_KEYBOARD_LL hook installed` — keyboard injection-detection working.
- `wrapped synth speak on <SynthClass>` — speech mute wedge in place.
- `transport connected (id=...); announcing capability` — fires once per session, on each side.

If `_remoteClient not present in this NVDA build` appears, the NVDA version doesn't bundle remote yet and we'd need to fall back to detecting the legacy `globalPlugins.remoteClient` add-on path; not implemented because all current public NVDA versions ship it in core.

## Build and install

```powershell
py -3.11 build.py
```

Produces `dist/remoteSpeechControl-<version>.nvda-addon`. Install on both machines: NVDA → menu → Tools → Manage add-ons → Install... and pick the file. Restart NVDA.

## Settings

NVDA → menu → Preferences → Settings → Remote Speech Control. Set the password to the same string on both machines. Optionally tick "Auto-request mute when I connect as controller". Toggle verbose logging.

## Logging

Goes into NVDA's own log under the `remoteSpeechControl` logger name. Open with NVDA → menu → Tools → View log, then search for `rsc:`.

## Workflow

- On both machines: install add-on, set the same password, restart NVDA.
- Connect via NVDA Remote as usual (controlling machine = master, controlled machine = slave).
- If "Auto-request" is on, mute is requested automatically on connect; otherwise NVDA Remote will pop a "Mute speech on the remote machine?" yes/no on the controller.
- While muted: any physical keypress on the controlled machine disables speech-drop until the next remote keypress (ping-pong).
- Force-unmute on the controlled machine: `NVDA+shift+u` (rebindable). Releases `muted_by_remote` entirely; controller must re-arm.
- Disconnect always implicitly unmutes.
