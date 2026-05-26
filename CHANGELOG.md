# Changelog

## 0.8.0

Two fixes.

**The synth settings ring is quicker to skip through when remote-controlling.** When you're typing into a remote machine and you press the synth settings ring keys to change voice, rate, pitch or volume, the announcement of the new setting now plays straight away. Before, it could be held up briefly while your local NVDA finished saying something the remote machine had passed across, which made the ring feel about twice as slow as usual.

**Reloading the add-on no longer breaks an in-progress remote-control session.** If you reloaded NVDA plugins on a machine that was being remote-controlled (or installed an update to this add-on mid-session), the person controlling it would stop being able to type into it until both ends disconnected and reconnected. (Thanks Andre for reporting this on GitHub.) Fixed.
