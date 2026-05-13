# Remote Speech Control — handover and publishing notes

This document has two parts. Part 1 is the technical handover for the add-on itself — what each file does, why, and the load-bearing implementation details. Part 2 is a reusable recipe for publishing an NVDA add-on (or any GitHub-hosted project) end to end, from cold-start GitHub account to a release in the NVDA Add-on Store. Part 2 is written generally enough to follow for a different project later; specifics for this project are called out as examples.

## Quick reference

- **GitHub repo:** <https://github.com/Ednunp/remoteSpeechControl>
- **Releases:** <https://github.com/Ednunp/remoteSpeechControl/releases>
- **Latest store submission:** <https://github.com/nvaccess/addon-datastore/issues/9147> (v0.5.0)
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
      synthwedge.py                           # synth.speak / cancel patches
      inputmonitor.py                         # WH_KEYBOARD_LL hook
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
synthwedge.install()          # patch synth.speak / cancel
inputmonitor.install()        # install the low-level keyboard hook
remoteintegration.install()   # patch _remoteClient
selfupdater.start()           # schedule daily update check (if enabled)
gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(RemoteSpeechControlPanel)
```

`terminate()` does the reverse in safe order. The force-unmute hotkey (`NVDA+shift+u`, rebindable) lives here as a script method; it calls `state.set_muted_by_remote(False)` and, if we're the controller, sends an `unmute_request` to the peer.

### `logger.py` — log routing

Five-line wrapper that forwards `info/debug/warning/error/exception` to NVDA's `logHandler.log`. The reason it exists at all: an earlier version of this add-on used `logging.getLogger(name)` directly and nothing reached NVDA's main log because of how NVDA configures its handlers. `logHandler.log` is the canonical entry point and is guaranteed to land in NVDA's `nvda.log`. All messages begin with the prefix `rsc:` so they're easy to grep.

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

- `muted_by_remote` — set True when an authenticated `mute_request` has been honoured. Cleared on disconnect, on force-unmute, or on an authenticated `unmute_request`.
- `remote_driving` — set True on every remote-driven keystroke, cleared on every physical local keystroke. Strict ping-pong, no timeout.

The wedge drops speech only when **both** flags are True. So muting is gated on the user's authorisation AND on the controller actually driving right now.

### `protocol.py` — the wire authentication

Custom message types ride NVDA Remote's existing TCP transport (see `remoteintegration.py`) but use a string prefix `remoteSpeechControl_` instead of one of NVDA Remote's built-in `RemoteMessageType` enum values. The four types we use:

```
remoteSpeechControl_capability       # "I have the add-on installed, version N"
remoteSpeechControl_mute_request     # signed: please mute on your side
remoteSpeechControl_unmute_request   # signed: please unmute on your side
remoteSpeechControl_state            # acknowledgement: muted / unmuted / denied
remoteSpeechControl_consent_pending  # I'm waiting for my local user to consent
```

Authentication: PBKDF2-HMAC-SHA256 (100 000 iterations) derives a key from the shared password. Each request is signed with HMAC-SHA256 over `(action | nonce | timestamp)`. Verification uses `hmac.compare_digest` (constant time), a +/- 30 second timestamp window, and a 90-second sliding-window nonce tracker. Three failed attempts on the same transport trip a `FailureLimiter` that drops further requests silently for 60 seconds.

Password never crosses the wire. Wrong password is indistinguishable on the wire from no password configured. Bad MACs are dropped without any acknowledgement so an attacker can't probe.

### `synthwedge.py` — the speech mute mechanism

Wraps the current `SynthDriver.speak` method and re-wraps on `synthDriverHandler.synthChanged` (because changing voice or driver replaces the SynthDriver instance). When `state.should_drop_speech` is True, the wrapper drops the call.

Why at the synth-driver level rather than at `speech.speak`: NVDA Remote intercepts `speech.speak` upstream, captures the speech sequence, and forwards it to the controller before the call continues into NVDA's normal pipeline. By the time control reaches the active SynthDriver's `speak()` method, NVDA Remote has already shipped the speech to the controller. Dropping at the synth-driver level silences local audio without affecting what the controller hears. Wrapping `speech.speak` instead would also block the upstream forward, which is exactly wrong.

The non-obvious bit: when we drop a `speak` call, we still emit `synthIndexReached` for each `IndexCommand` in the dropped sequence, followed by `synthDoneSpeaking`. NVDA's "say all" (used for reading rich text and HTML continuously) waits for those signals between chunks; without them the say-all stalls in the middle of a chapter. Punctual single-utterance reads (Notepad's line-at-a-time cursor reading) don't notice because they don't queue. This was the root cause of a real bug discovered late in development.

`cancel()` is also wrapped, defensively wrapped in try/except so an exception from our cancel bookkeeping can never prevent the underlying synth cancel from running.

### `inputmonitor.py` — physical vs injected keystrokes

NVDA's `_remoteClient` replays remote keystrokes via raw `SendInput()` with no extra-info sentinel, so by the time NVDA's hook sees them they look identical to physical keys at the content level. Windows *itself*, however, sets `LLKHF_INJECTED` in the `KBDLLHOOKSTRUCT.flags` field for any synthesised input.

This module installs a `WH_KEYBOARD_LL` hook via ctypes and reads that flag on every key-down event. Combined with a 300 ms "injection window" opened by `remoteintegration.py` whenever `LocalMachine.sendKey` runs, it attributes keystrokes correctly:

- physical (not injected) → `mark_local_input()` → clear `remote_driving`
- injected within window → `mark_remote_input()` → set `remote_driving`
- injected outside window → ignored (some other tool's injection, leave state alone)

The third case matters because NVDA itself, AutoHotkey, the on-screen keyboard etc. all also produce injected events; without the window gate we'd flicker into "remote driving" whenever NVDA used SendInput internally.

The hook callback is deliberately tiny and passes everything through via `CallNextHookEx` — it observes, it does not block. Windows enforces a hook callback time limit (`LowLevelHooksTimeout`, default 300 ms); going over it makes Windows silently unhook us, so the callback must stay cheap.

### `remoteintegration.py` — glue into `_remoteClient`

This is the biggest file. It does the following monkey-patches into NVDA's bundled `_remoteClient` package:

1. **`TCPTransport.parse`** — intercepts inbound messages whose `type` field begins with `remoteSpeechControl_` and dispatches to our handlers *before* the original parser tries `RemoteMessageType(obj["type"])` (which would reject and silently drop unknown types). Everything else falls through to the original.
2. **`LocalMachine.sendKey`** — wraps the controlled-side replay function. Opens the injection-detection window in `inputmonitor` before the original runs. Also short-circuits the call entirely when `_input_frozen_transports` is non-empty (the consent-freeze mechanism described below). The whole pre-action is in try/except so a failure on our side can never drop a key-up event (which would leave a modifier stuck on the remote machine — we had a real bug doing this before adding the try/except).
3. **`LocalMachine.brailleInput`** — same short-circuit treatment for braille input.
4. **`RemoteClient.onConnectedAsLeader` / `onConnectedAsFollower` / `onDisconnectedAsLeader` / `onDisconnectedAsFollower`** — hooks the role lifecycle. The earlier approach of patching `Transport.__init__` didn't reliably fire for the `RelayTransport` subclass in current NVDA builds; role callbacks always fire and run *after* the transport is set up, which is exactly the moment we need.
5. **`RemoteSession.handleClientConnected`** — re-announces our capability whenever a peer joins the channel. Without this, a peer that joins later than us never receives our capability message (NVDA Remote's relay does not replay history). This was another real bug late in development.

For sending: we bypass the typed `Transport.send` (whose first arg is restricted to the closed `RemoteMessageType` enum) and write through the existing JSON serialiser directly onto `transport.queue`. The serialiser accepts any `str`-typed value because `RemoteMessageType` is `StrEnum`, so the wire format is identical to NVDA Remote's own messages — just with our prefix.

**Functions to monkey-patch must use `@functools.wraps(original)`.** NVDA's `extensionPoints.util.callWithSupportedKwargs` filters kwargs based on the callable's `inspect.signature`. Without `functools.wraps`, our wrapper's `*args, **kwargs` signature accepts everything (including kwargs the original doesn't accept), and the original then raises `TypeError`. With `functools.wraps`, inspect.signature follows `__wrapped__` back to the original, and the filter behaves correctly.

**The synth-settings-ring "stay local" feature** is dead simple: NVDA Remote already exposes `RemoteClient.localScripts` as a set of script callables that, when matched during `processKeyInput`, are run locally and NOT forwarded to the remote. We just add the four (six including large-step variants) synth-settings-ring scripts from `globalCommands.commands` to that set when the user has the checkbox ticked, and remove them when it's unticked. No new key hooking required.

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
8. Static text reminding about the force-unmute hotkey.

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
