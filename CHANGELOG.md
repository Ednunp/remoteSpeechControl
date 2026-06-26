# Changelog

## 1.1.1

One new feature, one bug fix.

**New: announce your own machine's battery status when querying battery from a remote.** If you're on a laptop controlling a desktop and you press NVDA+Shift+B (NVDA's "report battery status" hotkey), the desktop normally answers "No system battery" — which isn't what you want to hear. This add-on now has an option to make your own machine answer instead, or both machines one after the other.

Turn it on in the Remote Speech Control settings panel. Two new options:

- "Announce local machine battery status when querying battery from a remote session" (off by default).
- "Battery announcement order" — three choices: local then remote, remote then local, local only.

The wording is exactly what NVDA would normally say, just prefixed with "Local" or "Remote" so you can tell them apart. If the remote doesn't reply within two seconds (e.g. it's running an older version of the add-on or the network is being slow), the local answer is given anyway so you're never left without an announcement.

**Fix: multiple "mute speech on the remote?" confirmation dialogs can no longer stack on top of each other.** If you reconnected to a remote a few times in quick succession before answering the first dialog, you used to get one dialog per reconnect piling up. Now only one is ever shown at a time; subsequent reconnects are quietly ignored until you've answered the open one. (Thanks Jonathan for reporting this on GitHub.) Fixed.

## 1.0.0

One fix, building on the reload work from 0.8.0.

**Reloading the add-on no longer un-mutes the controlled machine, and the mute hotkey still works afterwards.** In 0.8.0, reloading plugins while a remote session was active released the audio mute as part of the teardown, and the controller's mute hotkey then had no effect (the controlled side silently rejected the request). Now the mute is preserved across the reload and the hotkey continues to work normally. (Thanks Andre for reporting this on GitHub.) Fixed.

## 0.8.0

Two fixes.

**The synth settings ring is quicker to skip through when remote-controlling.** When you're typing into a remote machine and you press the synth settings ring keys to change voice, rate, pitch or volume, the announcement of the new setting now plays straight away. Before, it could be held up briefly while your local NVDA finished saying something the remote machine had passed across, which made the ring feel about twice as slow as usual.

**Reloading the add-on no longer breaks an in-progress remote-control session.** If you reloaded NVDA plugins on a machine that was being remote-controlled (or installed an update to this add-on mid-session), the person controlling it would stop being able to type into it until both ends disconnected and reconnected. (Thanks Andre for reporting this on GitHub.) Fixed.
