# Changelog

## 1.0.0

One fix, building on the reload work from 0.8.0.

**Reloading the add-on no longer un-mutes the controlled machine, and the mute hotkey still works afterwards.** In 0.8.0, reloading plugins while a remote session was active released the audio mute as part of the teardown, and the controller's mute hotkey then had no effect (the controlled side silently rejected the request). Now the mute is preserved across the reload and the hotkey continues to work normally. (Thanks Andre for reporting this on GitHub.) Fixed.

## 0.8.0

Two fixes.

**The synth settings ring is quicker to skip through when remote-controlling.** When you're typing into a remote machine and you press the synth settings ring keys to change voice, rate, pitch or volume, the announcement of the new setting now plays straight away. Before, it could be held up briefly while your local NVDA finished saying something the remote machine had passed across, which made the ring feel about twice as slow as usual.

**Reloading the add-on no longer breaks an in-progress remote-control session.** If you reloaded NVDA plugins on a machine that was being remote-controlled (or installed an update to this add-on mid-session), the person controlling it would stop being able to type into it until both ends disconnected and reconnected. (Thanks Andre for reporting this on GitHub.) Fixed.
