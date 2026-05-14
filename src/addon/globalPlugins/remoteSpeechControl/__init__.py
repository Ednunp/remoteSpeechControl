"""Remote Speech Control global plugin — entry point.

Wires together the synth wedge, input monitor, NVDA Remote integration, and
settings panel. NVDA instantiates the GlobalPlugin once at startup and calls
terminate() at shutdown.
"""
from __future__ import annotations

import globalPluginHandler
import gui
import wx

from . import logger
from . import config_spec
from . import state as state_module
from . import audiomute
from . import inputmonitor
from . import remoteintegration
from . import selfupdater
from . import settings as settings_module

log = logger.get()

ADDON_NAME = "remoteSpeechControl"


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

    scriptCategory = "Remote Speech Control"

    def __init__(self):
        super().__init__()
        log.info("rsc: starting up")
        config_spec.install()
        logger.configure_verbosity(config_spec.get_verbose())
        audiomute.install()
        inputmonitor.install()
        remoteintegration.install()
        selfupdater.start()
        gui.settingsDialogs.NVDASettingsDialog.categoryClasses.append(
            settings_module.RemoteSpeechControlPanel
        )
        log.info("rsc: started")

    def terminate(self):
        log.info("rsc: shutting down")
        try:
            gui.settingsDialogs.NVDASettingsDialog.categoryClasses.remove(
                settings_module.RemoteSpeechControlPanel
            )
        except ValueError:
            pass
        try:
            selfupdater.stop()
        except Exception:
            log.exception("rsc: selfupdater.stop failed")
        remoteintegration.uninstall()
        inputmonitor.uninstall()
        audiomute.uninstall()
        super().terminate()

    def script_toggleMute(self, gesture):
        # Toggles the controlled machine's mute, from either side.
        #
        # On the controlling side: sends an authenticated mute_request
        # or unmute_request depending on the last-known remote state.
        # The consent flow on the controlled side still applies unless
        # Allow auto-mute is ticked there.
        #
        # On the controlled side: toggles its own mute state. If we are
        # about to mute, the actual state change is deferred via a
        # CallbackCommand so the "Speech muted" announcement gets to
        # play before the audio wedge would silence it.
        #
        # Ordinary local keypresses on the controlled side still do the
        # ping-pong unmute via inputmonitor; this hotkey is the
        # deliberate toggle that survives until pressed again or until
        # the controller flips it back.
        try:
            msg, deferred = remoteintegration.toggle_mute_action()
        except Exception:
            log.exception("rsc: toggle_mute_action failed")
            wx.CallAfter(_speak_message, "Toggle mute failed")
            return
        if deferred is not None:
            try:
                import speech
                from speech.commands import CallbackCommand
                speech.speak([msg, CallbackCommand(deferred)])
                return
            except Exception:
                log.exception("rsc: failed scheduling deferred mute via CallbackCommand")
                # Fall back: announce, then apply mute after a delay.
                wx.CallAfter(_speak_message, msg)
                try:
                    wx.CallLater(1500, deferred)
                except Exception:
                    log.exception("rsc: fallback deferred mute also failed")
        else:
            wx.CallAfter(_speak_message, msg)

    script_toggleMute.__doc__ = "Toggles the mute on the controlled machine. Works from either the controller or the controlled side; on the controller the keystroke is sent as an authenticated mute or unmute request and the consent flow on the controlled side still applies."

    __gestures = {
        "kb:NVDA+shift+m": "toggleMute",
    }


def _speak_message(text: str) -> None:
    try:
        import ui
        ui.message(text)
    except Exception:
        log.warning("rsc: ui.message failed", exc_info=True)
