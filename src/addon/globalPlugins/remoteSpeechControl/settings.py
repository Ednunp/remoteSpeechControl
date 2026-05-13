"""Settings panel for Remote Speech Control, registered into NVDA's Settings dialog."""
from __future__ import annotations

import wx

import config
import gui
from gui.settingsDialogs import SettingsPanel

from . import config_spec
from . import logger

log = logger.get()


class RemoteSpeechControlPanel(SettingsPanel):

    title = "Remote Speech Control"

    def makeSettings(self, settingsSizer: wx.BoxSizer) -> None:
        helper = gui.guiHelper.BoxSizerHelper(self, sizer=settingsSizer)

        self._password_ctrl = helper.addLabeledControl(
            "&Password (must match the other machine):",
            wx.TextCtrl,
            style=wx.TE_PASSWORD,
        )
        self._password_ctrl.SetValue(config_spec.get_password())

        self._auto_ctrl = helper.addItem(
            wx.CheckBox(self, label="&Auto-request mute when I connect as controller")
        )
        self._auto_ctrl.SetValue(config_spec.get_auto_request())

        self._allow_ctrl = helper.addItem(
            wx.CheckBox(self, label="Allow speech to be automatically &muted by controlling machine")
        )
        self._allow_ctrl.SetValue(config_spec.get_allow_auto_mute())

        self._keep_ring_ctrl = helper.addItem(
            wx.CheckBox(self, label="&Synth settings ring adjusts this machine, not the remote")
        )
        self._keep_ring_ctrl.SetValue(config_spec.get_keep_synth_settings_ring_local())

        self._verbose_ctrl = helper.addItem(
            wx.CheckBox(self, label="&Verbose logging")
        )
        self._verbose_ctrl.SetValue(config_spec.get_verbose())

        helper.addItem(
            wx.StaticText(
                self,
                label=(
                    "The force-unmute hotkey defaults to NVDA+shift+u. "
                    "Rebind it from NVDA's Input gestures dialog under "
                    "category Remote Speech Control."
                ),
            )
        )

    def onSave(self) -> None:
        section = config.conf[config_spec.SECTION]
        section["password"] = self._password_ctrl.GetValue()
        section["autoRequestOnConnect"] = self._auto_ctrl.GetValue()
        section["allowAutoMute"] = self._allow_ctrl.GetValue()
        section["keepSynthSettingsRingLocal"] = self._keep_ring_ctrl.GetValue()
        section["verboseLogging"] = self._verbose_ctrl.GetValue()
        logger.configure_verbosity(section["verboseLogging"])
        log.info(
            "rsc: settings saved (password set: %s, auto-request: %s, allow-auto-mute: %s, "
            "keep-ring-local: %s, verbose: %s)",
            bool(section["password"]),
            section["autoRequestOnConnect"],
            section["allowAutoMute"],
            section["keepSynthSettingsRingLocal"],
            section["verboseLogging"],
        )
        try:
            from . import remoteintegration
            remoteintegration.apply_keep_synth_ring_local()
        except Exception:
            log.exception("rsc: failed applying keep-synth-ring-local after save")
