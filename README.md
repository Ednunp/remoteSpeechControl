# Remote Speech Control

An NVDA add-on for managing speech during NVDA Remote sessions.

It does two related things:

1. **Mutes local speech on the controlled machine** while a remote keyboard is driving NVDA. The moment a real key is pressed on the controlled machine itself, speech resumes; it mutes again on the next remote keystroke. This stops the double-audio problem when you have a separate audio bridge (such as desktop-to-laptop streaming) carrying the controlled machine's speech to you alongside the speech NVDA Remote already forwards.
2. **Keeps NVDA's synth settings ring on the local machine** while you are in remote control mode, so you can adjust the rate, pitch, volume and other settings of the NVDA you're actually hearing — instead of those keystrokes going to the remote NVDA, where they have no audible effect.

## Installation

Install on **both** the controlling and controlled machines.

The add-on uses NVDA's bundled remote-access support (the `_remoteClient` package), so it requires a recent NVDA — minimum 2024.1, tested up to 2026.1.

Once the add-on appears in NVDA's Add-on Store, the simplest install path is NVDA menu → Tools → Add-on Store → search for "Remote Speech Control" → Install.

Until then, or if you'd rather install manually, download the `.nvda-addon` file from the latest [release](https://github.com/Ednunp/remoteSpeechControl/releases) and install it from NVDA menu → Tools → Add-on Store → Install from external file.

Restart NVDA after install.

## Configuration

NVDA menu → Preferences → Settings → Remote Speech Control.

- **Password** — shared secret between the two machines. Must match exactly on both ends, including case. The password is never transmitted; only an HMAC signature derived from it is. Without a password configured, the mute feature is inactive.
- **Auto-request mute when I connect as controller** — when ticked, automatically requests muting as soon as a session connects, instead of prompting you to confirm first. Default off.
- **Allow speech to be automatically muted by controlling machine** — when ticked, the controlled side accepts authenticated mute requests without prompting and without freezing input. When unticked (the default), you are prompted for consent on the controlled machine and all remote input from the controller is paused until you answer.
- **Synth settings ring adjusts this machine, not the remote** — when ticked, NVDA's synth settings ring keys are routed to your local NVDA while you are in remote control mode, instead of being forwarded to the remote. Default off.
- **Verbose logging** — writes detailed activity into NVDA's log under `rsc:` lines. Useful for diagnosing problems. Default on.

The force-unmute hotkey is `NVDA+shift+u` by default; rebindable from NVDA's Input gestures dialog under the Remote Speech Control category.

## How muting works in practice

1. Connect NVDA Remote as normal.
2. On the controller, you are asked "Mute speech on the remote machine while you control it from here?" (or the request is fired automatically if you have ticked Auto-request).
3. On the controlled machine, you are asked "The controller has requested to mute speech on this machine. Remote input is paused until you answer. Allow muting for this session?" (skipped if you have ticked Allow speech to be automatically muted).
4. Once both sides agree, the controlled machine's speech is silent while the controller is driving, and resumes the moment any key is pressed on the controlled machine itself.
5. Disconnecting the remote session always unmutes the controlled machine.

While the controlled side is waiting for your answer to the consent prompt, all keyboard and braille input from the controller is dropped on the controlled machine — only the local physical keyboard can answer the prompt. The controller's NVDA announces "Waiting for remote user to allow mute. Remote input is paused." once, so they know to wait.

## Security model

- A shared password is required for any mute to take effect. The password is derived through PBKDF2-HMAC-SHA256 (100 000 iterations) into a key, and each mute request is signed with an HMAC of that key plus a random nonce and a timestamp.
- The receiver checks the timestamp window (+/- 30 seconds), the nonce (rejected if seen within the last 90 seconds), and the HMAC (constant-time compared). Wrong passwords and tampered messages are silently dropped, so an attacker can't tell whether they got the password right.
- After three failed authentication attempts on a transport, further attempts are locked out for 60 seconds.
- A controller who has obtained your NVDA Remote channel key but does **not** have the password set cannot mute your speech.
- Even with the password, if you have not pre-authorised auto-muting, the controller cannot mute you without your local consent — remote input is frozen on your machine until you answer the prompt.

## Build from source

Requires Python 3.11 or newer.

```powershell
py -3.11 build.py
```

The build script produces a fresh `.nvda-addon` file in `dist/`.

## Contributing / reporting issues

Issues and pull requests welcome at <https://github.com/Ednunp/remoteSpeechControl/issues>.

## License

GNU General Public License version 2 — see [LICENSE](LICENSE).
