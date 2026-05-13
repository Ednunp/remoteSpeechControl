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
from . import synthwedge
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
        synthwedge.install()
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
        synthwedge.uninstall()
        super().terminate()

    def script_forceUnmute(self, gesture):
        # Drops the muted-by-remote state entirely. The next mute_request from
        # the controller is required to re-arm; ordinary local keypresses do
        # the gentler ping-pong unmute via inputmonitor.
        s = state_module.state
        was = s.muted_by_remote
        s.set_muted_by_remote(False)
        if was:
            try:
                remoteintegration.request_unmute_for_active_session()
            except Exception:
                log.exception("rsc: notifying peer of force-unmute failed")
            wx.CallAfter(_speak_message, "Speech force unmuted")
        else:
            wx.CallAfter(_speak_message, "Speech is not currently muted")

    script_forceUnmute.__doc__ = "Forces Remote Speech Control off; controller must re-arm to mute again."

    __gestures = {
        "kb:NVDA+shift+u": "forceUnmute",
    }


def _speak_message(text: str) -> None:
    try:
        import ui
        ui.message(text)
    except Exception:
        log.warning("rsc: ui.message failed", exc_info=True)
