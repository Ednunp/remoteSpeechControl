# Remote Speech Control — handover and publishing notes

This document has two parts. Part 1 is the technical handover for the add-on itself — what each file does, why, and the load-bearing implementation details. Part 2 is a reusable recipe for publishing an NVDA add-on (or any GitHub-hosted project) end to end, from cold-start GitHub account to a release in the NVDA Add-on Store. Part 2 is written generally enough to follow for a different project later; specifics for this project are called out as examples.

## Quick reference

- **GitHub repo:** <https://github.com/Ednunp/remoteSpeechControl>
- **Releases:** <https://github.com/Ednunp/remoteSpeechControl/releases>
- **Latest store submission:** <https://github.com/nvaccess/addon-datastore/issues/9174> (v0.7.5)
- **Local working tree (out of Dropbox):** `D:\proj\remoteSpeechControl\`
- **GPL-2.0 licence**, NVDA 2024.1 — 2026.1, internal addonId `remoteSpeechControl`, log prefix `rsc:`, config section `[remoteSpeechControl]` in `nvda.ini`.

---

# Part 1 — The add-on

## What it does

Two related features for NVDA Remote sessions.

1. **Mutes local speech on the controlled machine** while a remote keyboard is driving NVDA. The instant a real key is pressed on the controlled machine itself, speech resumes; it mutes again on the next remote keystroke ("ping-pong"). This solves the doubled-audio problem when something else is also forwarding the controlled machine's sound back to the controller — without that, the user hears two voices a fraction of a second out of sync.
2. **Keeps NVDA's synth settings ring on the local NVDA** while in remote control mode, so the rate/pitch/volume keys adjust the synth the user actually hears rather than the remote one they don't.

Plus a self-updater that polls GitHub Releases once a day if the user opts in.

## Project layout

```
D:\proj\remoteSpeechControl\
  src/addon/
    manifest.ini                              # add-on metadata NVDA reads
    doc/en/readme.html                        # in-store help, opened by the Help button
    globalPlugins/remoteSpeechControl/        # the python package NVDA loads
      __init__.py                             # GlobalPlugin entry point
      logger.py                               # tiny wrapper around NVDA's logHandler
      config_spec.py                          # config schema + accessors
      state.py                                # mute state machine
      protocol.py                             # HMAC handshake + replay protection
      audiomute.py                            # OS-level WASAPI session mute (ISimpleAudioVolume.SetMute)
      inputmonitor.py                         # WH_KEYBOARD_LL hook for ping-pong attribution
      remoteintegration.py                    # patches into NVDA's _remoteClient
      selfupdater.py                          # daily GitHub release poll
      settings.py                             # Settings panel
  build.py                                    # produces dist/remoteSpeechControl-X.Y.Z.nvda-addon
  README.md                                   # public README rendered on GitHub
  LICENSE                                     # GPL-2.0 full text
  .gitignore                                  # excludes dist/, __pycache__, etc.
  HANDOVER.md                                 # this file
```

A `.nvda-addon` file is just `src/addon/` zipped with `manifest.ini` at the archive root. `build.py` does that and writes the result into `dist/`. Don't commit `dist/`; release artifacts are attached to GitHub releases instead.

## How each module works

### `__init__.py` — the entry point

Defines `GlobalPlugin`, which NVDA instantiates once at startup. Its `__init__` brings every other module online:

```
config_spec.install()         # register our section in NVDA's config schema
audiomute.install()           # arm OS-level WASAPI mute via state listener
inputmonitor.install()        # install WH_KEYBOARD_LL hook for ping-pong
remoteintegration.install()   # patch _remoteClient
selfupdater.start()           # schedule daily update check (if enabled)
gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(RemoteSpeechControlPanel)
```

`terminate()` does the reverse in safe order. The toggle-mute hotkey (`NVDA+control+shift+m`, rebindable) lives here as a script method; it delegates to `remoteintegration.toggle_mute_action()`, which is controller-only and gated on `RemoteClient.sendingKeys` (i.e. the controller has F11'd into the controlled machine). When active, it sends an authenticated `mute_request` or `unmute_request` directly — no confirmation dialog on the controller side, because pressing the hotkey is itself the confirmation and the user's keystrokes are forwarded to the remote while they are F11'd in, which would make a local dialog impossible to navigate anyway. Mute direction is chosen from `_peer_muted_state`, the controller-side mirror of the controlled peer's mute state from inbound `MSG_STATE` acknowledgements. The controlled side has no hotkey by design — local user unmute is handled by ping-pong on any keystroke via the LL hook. To mute themselves persistently the controlled user asks the controller to toggle.

On `__init__`, the GlobalPlugin also calls `remoteintegration.register_persistent_local_script(self.script_toggleMute)`. That adds the script to NVDA Remote's `localScripts`, so when the controller is F11'd in, NVDA Remote runs the script locally instead of forwarding the keystroke as input to the controlled side.

### `logger.py` — log routing

Small wrapper that forwards `info/debug/warning/error/exception` to NVDA's `logHandler.log`. The reason it exists at all: an earlier version of this add-on used `logging.getLogger(name)` directly and nothing reached NVDA's main log because of how NVDA configures its handlers. `logHandler.log` is the canonical entry point and is guaranteed to land in NVDA's `nvda.log`. All messages begin with the prefix `rsc:` so they're easy to grep.

The user-facing "Verbose logging" checkbox is wired through `configure_verbosity(bool)`: when off, `info`-level emission is suppressed; `warning`, `error` and `exception` still fire so genuine problems are never hidden. Default on, because the log is the main diagnostic surface.

### `config_spec.py` — settings persistence

Registers a `[remoteSpeechControl]` section in NVDA's config schema (`config.conf.spec`). All settings are stored here so they persist across NVDA restarts. Accessor functions (`get_password()`, `get_auto_request()` etc.) wrap `config.conf[SECTION][key]` so the rest of the code never touches NVDA's config directly. The schema:

```python
"password": "string(default='')",
"autoRequestOnConnect": "boolean(default=False)",
"allowAutoMute": "boolean(default=False)",
"keepSynthSettingsRingLocal": "boolean(default=False)",
"autoUpdateCheck": "boolean(default=False)",
"lastUpdateCheckMs": "integer(default=0)",
"snoozedUpdateVersion": "string(default='')",
"verboseLogging": "boolean(default=True)",
```

All "Auto-X" toggles default to False per the workspace rule of explicit opt-in.

### `state.py` — the mute state machine

Two booleans:

- `muted_by_remote` — set True when an authenticated `mute_request` has been honoured. Cleared on disconnect or on an authenticated `unmute_request`. Setting it False atomically also clears `remote_driving`.
- `remote_driving` — set True on every remote-driven keystroke, cleared on every physical local keystroke. Strict ping-pong, no timeout. Driven by the WH_KEYBOARD_LL hook in `inputmonitor.py` reading `LLKHF_INJECTED`.

The OS-level audio session mute is engaged when `should_drop_speech` is True, which is `muted_by_remote AND remote_driving`. State has a listener registry; `audiomute.py` subscribes and toggles `SetMute` on each transition. So the controller arms muting via `mute_request`, and from then on ping-pong rules: the controlled machine is silent while the controller is driving, audible the moment the local user touches the keyboard, silent again on the next remote keystroke. The mute is fully released on `unmute_request` or on session disconnect.

### `protocol.py` — the wire authentication

Custom message types ride NVDA Remote's existing TCP transport (see `remoteintegration.py`) but use a string prefix `remoteSpeechControl_` instead of one of NVDA Remote's built-in `RemoteMessageType` enum values. The five types we use:

```
remoteSpeechControl_capability       # "I have the add-on installed, version N"
remoteSpeechControl_mute_request     # signed: please mute on your side
remoteSpeechControl_unmute_request   # signed: please unmute on your side
remoteSpeechControl_state            # acknowledgement: muted / unmuted / denied
remoteSpeechControl_consent_pending  # I'm waiting for my local user to consent
```

Authentication: PBKDF2-HMAC-SHA256 (100 000 iterations) derives a key from the shared password. Each request is signed with HMAC-SHA256 over `(action | nonce | timestamp)`. Verification uses `hmac.compare_digest` (constant time), a +/- 30 second timestamp window, and a 90-second sliding-window nonce tracker. Three failed attempts on the same transport trip a `FailureLimiter` that drops further requests silently for 60 seconds.

Password never crosses the wire. Wrong password is indistinguishable on the wire from no password configured. Bad MACs are dropped without any acknowledgement so an attacker can't probe.

### `audiomute.py` — the speech mute mechanism

Toggles the Windows audio session mute flag on NVDA's process via `ISimpleAudioVolume.SetMute`. The COM bindings (`IMMDeviceEnumerator`, `IMMDevice`, `IAudioSessionManager2`, `ISimpleAudioVolume`) are hand-rolled with `comtypes` (which NVDA bundles); no `pycaw` or other vendored dependency. `IAudioSessionManager2.GetSimpleAudioVolume(NULL, 0)` returns the default per-process session directly, so we never enumerate sessions and match on PID.

Registered as a listener on `state.MuteState`. Each transition of `should_drop_speech` fires exactly one `SetMute(True/False)` call, marshalled to the main thread via `wx.CallAfter` so the COM object always lives on a known apartment.

Why at the Windows-audio-session layer rather than at `speech.speak` or `nvwave.WavePlayer.feed`:

- Wrapping `speech.speak` would block NVDA Remote's upstream speech-forwarding intercept, which is exactly wrong — the controller needs to hear the controlled side's speech.
- Wrapping `WavePlayer.feed` and substituting zero-fill bytes was the previous approach (versions 0.5.x – 0.6.12). It worked on some setups but on NVDA 2026.1 / Python 3.13 / WASAPI it perturbed the WavePlayer drain reporting, which races the synth-index callback chain in `speech.sayAll`. Queued `nextLine` callbacks ran after `_TextReader.stop()` had cleared `textInfo`, producing `AttributeError: 'NoneType' object has no attribute 'collapse'` in `sayAll.collapseLineImpl`. The new OS-level mute touches no audio data, the synth runs end-to-end at real timing, every WavePlayer drain event fires on the same schedule as without the addon, and sayAll/Shift-to-pause/Ctrl-to-stop behave identically to addon-not-installed. Only the speakers stop receiving the output.

`uninstall()` always calls `SetMute(False)` unconditionally, because leaving an audio session muted with no UI to undo it is a nightmare for the user to discover.

### `inputmonitor.py` — physical vs injected keystrokes

NVDA's `_remoteClient` replays remote keystrokes via raw `SendInput()` with no extra-info sentinel, so by the time NVDA's input layer sees them they look identical to physical keys at the content level. Windows *itself*, however, sets `LLKHF_INJECTED` (and `LLKHF_LOWER_IL_INJECTED` for cross-integrity-level injection) in the `KBDLLHOOKSTRUCT.flags` field for any synthesised input.

This module installs a `WH_KEYBOARD_LL` hook via ctypes and reads those flags on every key-down event. Attribution is one-line:

- injected (either flag set) → `mark_remote_input()` → `remote_driving = True`
- physical (neither flag set) → `mark_local_input()` → `remote_driving = False`

Result: speech is silenced from the moment a remote keystroke arrives, and restored the moment any physical local keystroke happens. The audiomute listener catches each transition and toggles `SetMute` accordingly.

The hook callback is deliberately tiny: read 4 bytes of flags, one bitwise-and, one `wx.CallAfter` to enqueue the state change on the main thread, then through `CallNextHookEx` and return. Windows enforces `LowLevelHooksTimeout` (default 300 ms); going over it makes Windows silently unhook us, so anything beyond a memory read and an enqueue must NOT happen here. The hook must also never raise — a raised exception in a low-level hook proc drops keystrokes system-wide.

The earlier "300 ms injection window" gate that filtered injected events by whether `LocalMachine.sendKey` had recently run is gone. It was a workaround for a different problem (NVDA's own internal `SendInput` calls firing the hook and being misattributed) that turned out to be self-correcting in practice — those internal injections are bracketed by NVDA's own speech-pause-resume around the same gestures, so a brief mute-flicker during them is invisible. Removing the window simplifies the attribution to one flag check.

### `remoteintegration.py` — glue into `_remoteClient`

This is the biggest file. It does the following monkey-patches into NVDA's bundled `_remoteClient` package:

1. **`TCPTransport.parse`** — intercepts inbound messages whose `type` field begins with `remoteSpeechControl_` and dispatches to our handlers *before* the original parser tries `RemoteMessageType(obj["type"])` (which would reject and silently drop unknown types). Everything else falls through to the original.
2. **`LocalMachine.sendKey`** — wraps the controlled-side replay function. Short-circuits the call entirely when `_input_frozen_transports` is non-empty (the consent-freeze mechanism described below), otherwise hands straight through to the original. The wrap does NOT mark the keystroke for ping-pong attribution — `inputmonitor.py`'s `WH_KEYBOARD_LL` hook reads `LLKHF_INJECTED` on the resulting kernel event and does that job, more accurately (kernel-event truth, not a Python-level scheduling proxy) and as a single attribution path for any injection source.
3. **`LocalMachine.brailleInput`** — same short-circuit treatment for braille input.
4. **`RemoteClient.onConnectedAsLeader` / `onConnectedAsFollower` / `onDisconnectedAsLeader` / `onDisconnectedAsFollower`** — hooks the role lifecycle. The earlier approach of patching `Transport.__init__` didn't reliably fire for the `RelayTransport` subclass in current NVDA builds; role callbacks always fire and run *after* the transport is set up, which is exactly the moment we need.
5. **`RemoteSession.handleClientConnected`** — re-announces our capability whenever a peer joins the channel. Without this, a peer that joins later than us never receives our capability message (NVDA Remote's relay does not replay history). This was another real bug late in development.

For sending: we bypass the typed `Transport.send` (whose first arg is restricted to the closed `RemoteMessageType` enum) and write through the existing JSON serialiser directly onto `transport.queue`. The serialiser accepts any `str`-typed value because `RemoteMessageType` is `StrEnum`, so the wire format is identical to NVDA Remote's own messages — just with our prefix.

**Functions to monkey-patch must use `@functools.wraps(original)`.** NVDA's `extensionPoints.util.callWithSupportedKwargs` filters kwargs based on the callable's `inspect.signature`. Without `functools.wraps`, our wrapper's `*args, **kwargs` signature accepts everything (including kwargs the original doesn't accept), and the original then raises `TypeError`. With `functools.wraps`, inspect.signature follows `__wrapped__` back to the original, and the filter behaves correctly.

**Local scripts management.** NVDA Remote exposes `RemoteClient.localScripts` as a set of script callables that, when matched during `processKeyInput` on the controller, are run locally and NOT forwarded to the remote as keystrokes. We use this for two things, both reconciled by `apply_local_scripts()`:

* **Persistent local scripts.** Bound script methods registered via `register_persistent_local_script(...)` — at present, just the GlobalPlugin's `script_toggleMute` — are always in `localScripts`. That's what makes `NVDA+control+shift+m` run on the controller's side instead of being forwarded as a keystroke when the user is F11'd into the controlled machine.
* **Synth-settings-ring scripts.** Four (six including large-step variants) bound methods on `globalCommands.commands` are added or removed based on the "Synth settings ring adjusts this machine, not the remote" checkbox.

`apply_local_scripts()` runs at install (deferred via `wx.CallAfter` because the `RemoteClient` singleton isn't yet up when we initialise), on every role connect (the `RemoteClient` may have been recreated), on settings save, and on persistent-script registration. `uninstall()` clears both classes of scripts back out of the set before unpatching.

**The consent freeze.** When an authenticated `mute_request` arrives on the controlled side AND `allowAutoMute` is unticked, the transport id is added to `_input_frozen_transports`. The wrapped `sendKey` and `brailleInput` short-circuit while that set is non-empty, so the controller cannot drive the consent dialog themselves. A `consent_pending` message is sent back to the controller; their NVDA announces "Waiting for remote user to allow mute. Remote input is paused." once. The freeze clears the moment the local user resolves the dialog (yes or no) or the transport disconnects.

The "Allow speech to be automatically muted" checkbox on the controlled side bypasses both the prompt and the freeze. That's the escape hatch for legitimate unattended remote control of one's own machines.

### `selfupdater.py` — the daily update poll

Polls `api.github.com/repos/Ednunp/remoteSpeechControl/releases/latest` once a day (when opted in), compares the released tag to the installed addon version using a proper tuple-of-ints comparator (`0.4.10` correctly > `0.4.2`), and offers to download and install a newer `.nvda-addon` from the release's assets.

Defensive shape:

- HTTP runs on a daemon worker thread; UI is touched only via `wx.CallAfter` back to the main thread.
- All filesystem and network exceptions caught; nothing crashes NVDA.
- A "no" answer is persisted per-version (`snoozedUpdateVersion` in config). Manual "Check now" bypasses the snooze.
- Manifest name on the downloaded bundle is verified before install — an unexpected URL serving a different add-on cannot replace ours.
- Failed download or install leaves the previous installation untouched.

Module-level singleton API: `start()`, `stop()`, `check_now(interactive=True)`, `refresh_schedule()`. `start` and `stop` are called from `GlobalPlugin.__init__` / `terminate`. `check_now` is called from the Settings-panel button. `refresh_schedule` is called from the Settings-panel `onSave` so toggling the daily-check setting takes effect immediately.

### `settings.py` — the panel

Standard NVDA `SettingsPanel`. Order of controls, with mnemonics:

1. Password (Alt+P) — masked text field, must match peer.
2. Auto-request mute when I connect as controller (Alt+A) — checkbox.
3. Allow speech to be automatically muted by controlling machine (Alt+M) — checkbox.
4. Synth settings ring adjusts this machine, not the remote (Alt+S) — checkbox.
5. Check for updates daily (Alt+U) — checkbox.
6. Check for updates now (Alt+C) — button.
7. Verbose logging (Alt+V) — checkbox.
8. Static text reminding about the toggle-mute hotkey.

All mnemonics are unique per panel (P, A, M, S, U, C, V).

## Build / install

```powershell
py -3.11 build.py
```

Produces `dist/remoteSpeechControl-<version>.nvda-addon`. The build script reads the version from `manifest.ini` and writes one artefact, deleting any prior `dist/remoteSpeechControl-*.nvda-addon` first so only the current build is present.

Install on both machines via NVDA → Tools → Add-on Store → Install from external file.

---

# Part 2 — Publishing an NVDA add-on to GitHub and the NVDA Add-on Store

This part is the reusable recipe. Wherever something is specific to this project, the project-specific value is shown in parentheses for reference; substitute your own when you reuse the recipe for a different project.

## What you need before you start

- A working `.nvda-addon` and its source under a *non-Dropbox* directory (Dropbox corrupts `.git` if you let it sync the repo).
- About 30 minutes for the first project; future releases of the same project are ~5 minutes each.

## Step 1 — GitHub account and two-factor authentication

If you already have an account, skip the create-account part; just check that 2FA is on.

1. Go to <https://github.com> and create an account if needed. Pick a username you're OK being publicly identified by — your add-on will show this in PR comments and on the store listing.
2. Enable 2FA at <https://github.com/settings/security>. Pick *Authenticator app* (Google Authenticator, Authy etc.) rather than SMS — it works better with screen readers and is more secure. Save the recovery codes somewhere outside Dropbox (a password manager, a text file on a USB stick). They're the only way back in if you lose access to your authenticator.
3. Also at <https://github.com/settings/emails>, tick "Keep my email address private". Git will then use a `<id>+<login>@users.noreply.github.com` address in commit metadata instead of your real one.

## Step 2 — Personal Access Token

The GitHub CLI needs a token to act as you from a script. Create one for that purpose:

1. Go to <https://github.com/settings/tokens/new>.
2. **Note**: any descriptive label, e.g. `gh CLI for projectName`.
3. **Expiration**: 90 days, or "No expiration" if you don't want to renew. Tokens with long lifetimes need to be guarded carefully because they're effectively your password for scripted access.
4. **Scopes** — tick exactly these three:
   - `repo` (the whole block — ticking the top-level checkbox auto-ticks every sub-item)
   - `workflow`
   - `read:org` (a sub-item under the `admin:org` block — tick *just* this one)
5. Generate the token. The next page shows the token **once and only once**. Copy it.

GitHub may warn that some scopes overlap with others ("the scopes you've selected are included in other scopes"). That's informational, not an error — the minimum set the CLI needs is the three above.

## Step 3 — Install the GitHub CLI

```powershell
winget install --id GitHub.cli --accept-package-agreements --accept-source-agreements
```

After this finishes, open a *fresh* terminal (not the one you used to install, because the new PATH only takes effect in newly-launched processes). Verify with:

```powershell
gh --version
```

If installed by winget into the default location, the binary is at `C:\Program Files\GitHub CLI\gh.exe`.

## Step 4 — Authenticate the CLI with the token

The interactive `gh auth login` flow needs you to copy a short device code from the terminal into a browser, which is fiddly with a screen reader. Use the token from Step 2 instead.

1. Save the token to a plain text file outside Dropbox, e.g. `D:\proj\.gh-token.txt`. The file should contain the token on one line, nothing else, no quotes. (Notepad sometimes adds a `.txt` extension when you save a file whose name starts with a dot. If you see `gh-token.txt.txt` in Explorer, that's why. Easiest workaround: save without a leading dot, e.g. `D:\proj\gh-token.txt`.)
2. Authenticate:
   ```powershell
   gh auth login --hostname github.com --git-protocol https --with-token < D:\proj\.gh-token.txt
   ```
3. Verify:
   ```powershell
   gh auth status
   ```
   Expected output includes `Logged in to github.com account YOUR-USERNAME` and `Token scopes: 'read:org', 'repo', 'workflow'`.
4. **Delete the token file**. `gh` has now saved the token to the Windows credential manager keyring; the text file is no longer needed and is a liability if anyone else gets at the disk.
   ```powershell
   del D:\proj\.gh-token.txt
   ```
5. Wire `gh` to be git's credential helper for HTTPS, so subsequent `git push` calls don't try to pop up the Windows credential prompt:
   ```powershell
   gh auth setup-git
   ```

## Step 5 — Decide where the working tree lives

Pick a non-Dropbox folder, e.g. `D:\proj\<projectName>\`. If your source is currently in Dropbox, copy it out (don't move — keep the Dropbox version as a backup until the GitHub flow is working).

```powershell
mkdir D:\proj
robocopy D:\Dropbox\proj\<projectName> D:\proj\<projectName> /E
```

From now on the GitHub workflow lives at `D:\proj\<projectName>\` and Dropbox is not involved.

## Step 6 — Create the public GitHub repo

We use a slightly indirect dance so the LICENSE file is the canonical full-text GPL-2.0, not a hand-written approximation: we let `gh` create the remote repo with the licence pre-populated, clone it, and copy our source files on top.

```powershell
cd D:\proj
ren <projectName> _staging

gh repo create <projectName> --public --license gpl-2.0 --description "<one-line description>" --clone
```

`--license gpl-2.0` makes GitHub populate `LICENSE` with the canonical text. `--clone` clones the new repo down into `D:\proj\<projectName>\` immediately. So now `D:\proj\` has both the cloned repo (with `LICENSE` and a stub README) and the source-only staging copy.

Other licence choices the flag accepts: `mit`, `apache-2.0`, `gpl-3.0`. For NVDA add-ons, GPL-2.0 is by far the most common because NVDA itself is GPL-2.0; reviewers will expect it.

## Step 7 — Copy source into the clone and set git identity

```powershell
cd D:\proj\<projectName>
git config user.name "<your-display-name>"
git config user.email "<id>+<login>@users.noreply.github.com"
robocopy ..\_staging . /E
```

To get your noreply email address without looking it up by hand:

```powershell
gh api user --jq "\"\(.id)+\(.login)@users.noreply.github.com\""
```

## Step 8 — Write `.gitignore` and a public-facing README

`.gitignore` content for an NVDA add-on:

```
__pycache__/
*.pyc
*.pyo
dist/
*.tmp
*.tmp.*
.gh-token.txt
*.log
.DS_Store
Thumbs.db
*.swp
```

`README.md` is what GitHub shows on the repo home page. It's read by potential users, NV Access reviewers, and NVDA store users who click through. Include:

- One-line description.
- What problem it solves (paragraph form).
- Install instructions (link to the latest release).
- Configuration overview.
- Security model (if relevant).
- Build-from-source instructions.
- Issues / contributing pointer.
- Licence line.

## Step 9 — Commit, push

```powershell
git add -A
git status              # sanity check
git commit -m "Initial source"
git push
```

If `git push` hangs and never returns, it's almost certainly waiting on the Windows credential manager UI which doesn't appear in non-interactive shells. `gh auth setup-git` from Step 4 prevents this. If you skipped that step earlier, run it now and retry `git push`.

## Step 10 — Build the release artefact and publish a release

```powershell
py -3.11 build.py
```

(Adapt to whatever build command your project uses.)

Then publish a GitHub release with the artefact attached:

```powershell
gh release create v<version> dist/<projectName>-<version>.nvda-addon `
  --title "v<version> — <short summary>" `
  --notes "<release notes in markdown>"
```

The tag (`v<version>`) is what NVDA's Add-on Store will reference for downloads. The release notes will appear in NVDA's update prompt and on the GitHub Releases page. Keep them concise — bullet points of what's new and a one-line install reminder.

Verify the download URL works:

```powershell
gh api repos/<owner>/<projectName>/releases/tags/v<version> --jq ".assets[].browser_download_url"
```

That URL is the one you'll submit to the NVDA store in Step 11.

## Step 11 — Clean up and put it on the NVDA Add-on Store

Clean up the staging copy:

```powershell
cd D:\proj
rmdir /S /Q _staging
```

Submit to the store. The NVDA Add-on Store catalogue lives at `github.com/nvaccess/addon-datastore`. Submission is via a GitHub *issue form* (not a direct PR). The form fields get parsed automatically to generate the metadata JSON and open a PR.

You can submit via the web form at <https://github.com/nvaccess/addon-datastore/issues/new/choose>, or via the CLI:

```powershell
gh issue create `
  --repo nvaccess/addon-datastore `
  --title "[Submit add-on]: <Display Name> v<version>" `
  --label autoSubmissionFromIssue `
  --body "### Download URL`n`nhttps://github.com/<owner>/<projectName>/releases/download/v<version>/<projectName>-<version>.nvda-addon`n`n### Source URL`n`nhttps://github.com/<owner>/<projectName>`n`n### Publisher`n`n<your-name>`n`n### Channel`n`nstable`n`n### License Name`n`nGPL v2`n`n### License URL`n`nhttps://www.gnu.org/licenses/gpl-2.0.html"
```

(The body format has to follow the issue form's `### Field Label` heading convention exactly; the autoSubmission bot is sensitive to it.)

What happens after submission:

1. NV Access's bot downloads the `.nvda-addon`, computes its SHA256, reads the manifest, generates a metadata JSON, and opens a PR adding it to `addons/<addonId>/<version>.json`.
2. Automated validation runs: schema check, virus scan via VirusTotal, code scan via CodeQL.
3. First time submitting under this `addonId`: a human reviewer at NV Access must approve you as an authorised submitter for that add-on ID. This is the only manual step they perform; turnaround is typically a few days.
4. Once approved and merged, the add-on appears in everyone's NVDA Add-on Store within about a day.

If a reviewer asks for changes, they'll comment on the issue or PR. Read the comment, fix the underlying source (manifest typo, missing licence header, etc.), publish a new GitHub release at a bumped version number, and **re-submit** with a fresh issue — you don't edit the existing one.

If you submit version N and then realise you want to ship N+1 before review starts, close the N issue with a comment ("superseded by N+1, please review that instead") and open a fresh issue for N+1. Reviewers prefer this to amending in flight.

You're automatically subscribed to any issue you create on GitHub, so notifications about reviewer comments will reach you by email.

## Future releases — the 5-minute path

For any subsequent release of the same project, the loop is:

1. Bump the version in `src/addon/manifest.ini`.
2. Make sure source changes are committed locally:
   ```powershell
   cd D:\proj\<projectName>
   git add -A
   git commit -m "<what changed>"
   git push
   ```
3. Build:
   ```powershell
   py -3.11 build.py
   ```
4. Release:
   ```powershell
   gh release create v<new-version> dist/<projectName>-<new-version>.nvda-addon `
     --title "v<new-version> — <short summary>" `
     --notes "<what's new>"
   ```
5. Submit to the store (if you're listed there) with the new download URL, same form as Step 11.

That's it. The store PR auto-generates as before.

## Troubleshooting

- `gh push` hangs forever → you skipped `gh auth setup-git`. Run it, retry.
- "Permission denied" or "remote does not exist" on push → your gh auth scopes don't include `repo`. Make a new token with the right scopes (see Step 2) and `gh auth login --with-token` again.
- Submitted to addon-datastore, the bot's PR fails with "submitter not approved" → first-time gate; wait for a human at NV Access to add you to the approved submitters list. Nothing to do until then except wait.
- Submitted to addon-datastore, the bot says manifest mismatch → almost always a typo in the manifest version vs the release tag, or the asset name in the release doesn't end in `.nvda-addon`. Re-upload the asset with the correct name; bot picks it up on the next validation pass.
- Wide-area "we missed read-org" type token errors → make a new token, this time tick the missing scope. The auth flow takes seconds to re-do.
