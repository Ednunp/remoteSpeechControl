"""Remote Speech Control global plugin — entry point.

Wires together the audio session mute (OS-level WASAPI SetMute), the
WH_KEYBOARD_LL ping-pong input monitor, the _remoteClient integration,
and the settings panel. NVDA instantiates the GlobalPlugin once at
startup and calls terminate() at shutdown.
"""
from __future__ import annotations

import globalPluginHandler
import gui
import wx

from . import logger
from . import config_spec
from . import audiomute
from . import inputmonitor
from . import remoteintegration
from . import settings as settings_module

log = logger.get()


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
        # Tell remoteintegration to keep the toggle-mute script local on
        # the controller. When the user is F11'd into remote-control
        # mode, NVDA Remote would otherwise forward the keystroke; we
        # want it handled locally so the request is sent from here.
        remoteintegration.register_persistent_local_script(self.script_toggleMute)
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
        remoteintegration.uninstall()
        inputmonitor.uninstall()
        audiomute.uninstall()
        super().terminate()

    def script_toggleMute(self, gesture):
        # Controlling-side only. The script is registered into NVDA
        # Remote's ``localScripts`` so it always runs on whichever
        # machine the user pressed the key on — never gets forwarded as
        # a keystroke even when the controller is F11'd into the remote
        # machine. The action itself is gated on "we are leader AND
        # currently in remote-control mode (sendingKeys=True)"; otherwise
        # it speaks a short status message and does nothing.
        try:
            msg = remoteintegration.toggle_mute_action()
        except Exception:
            log.exception("rsc: toggle_mute_action failed")
            msg = "Toggle mute failed"
        if msg:
            wx.CallAfter(_speak_message, msg)

    script_toggleMute.__doc__ = "Toggle mute on the controlled machine while you are controlling it. Sends a mute or unmute request directly (no confirmation dialog). Only effective while you are F11'd into the controlled machine."

    __gestures = {
        "kb:NVDA+control+shift+m": "toggleMute",
    }


def _speak_message(text: str) -> None:
    try:
        import ui
        ui.message(text)
    except Exception:
        log.warning("rsc: ui.message failed", exc_info=True)
