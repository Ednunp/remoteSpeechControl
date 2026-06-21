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

        self._battery_local_ctrl = helper.addItem(
            wx.CheckBox(
                self,
                label="Announce local machine &battery status when querying battery from a remote session",
            )
        )
        self._battery_local_ctrl.SetValue(config_spec.get_announce_local_battery_on_remote())
        self._battery_local_ctrl.Bind(wx.EVT_CHECKBOX, self._on_battery_local_toggle)

        self._battery_mode_ctrl = helper.addLabeledControl(
            "Battery announcement &order:",
            wx.Choice,
            choices=list(config_spec.BATTERY_MODE_LABELS),
        )
        current_mode = config_spec.get_battery_announcement_mode()
        try:
            current_idx = config_spec.BATTERY_MODE_ORDER.index(current_mode)
        except ValueError:
            current_idx = 0
        self._battery_mode_ctrl.SetSelection(current_idx)
        self._battery_mode_ctrl.Enable(self._battery_local_ctrl.GetValue())

        self._verbose_ctrl = helper.addItem(
            wx.CheckBox(self, label="&Verbose logging")
        )
        self._verbose_ctrl.SetValue(config_spec.get_verbose())

        helper.addItem(
            wx.StaticText(
                self,
                label=(
                    "The toggle-mute hotkey defaults to NVDA+control+shift+m. "
                    "Rebind it from NVDA's Input gestures dialog under "
                    "category Remote Speech Control."
                ),
            )
        )

    def _on_battery_local_toggle(self, event: wx.CommandEvent) -> None:
        # Enable / disable the order ComboBox in step with the checkbox.
        # Disabled state still navigable; NVDA announces "disabled" which
        # is the right cue that the choice has no effect right now.
        self._battery_mode_ctrl.Enable(self._battery_local_ctrl.GetValue())

    def onSave(self) -> None:
        section = config.conf[config_spec.SECTION]
        section["password"] = self._password_ctrl.GetValue()
        section["autoRequestOnConnect"] = self._auto_ctrl.GetValue()
        section["allowAutoMute"] = self._allow_ctrl.GetValue()
        section["keepSynthSettingsRingLocal"] = self._keep_ring_ctrl.GetValue()
        section["announceLocalBatteryOnRemote"] = self._battery_local_ctrl.GetValue()
        sel = self._battery_mode_ctrl.GetSelection()
        if 0 <= sel < len(config_spec.BATTERY_MODE_ORDER):
            section["batteryAnnouncementMode"] = config_spec.BATTERY_MODE_ORDER[sel]
        section["verboseLogging"] = self._verbose_ctrl.GetValue()
        logger.configure_verbosity(section["verboseLogging"])
        log.info(
            "rsc: settings saved (password set: %s, auto-request: %s, allow-auto-mute: %s, "
            "keep-ring-local: %s, battery-local: %s, battery-mode: %s, verbose: %s)",
            bool(section["password"]),
            section["autoRequestOnConnect"],
            section["allowAutoMute"],
            section["keepSynthSettingsRingLocal"],
            section["announceLocalBatteryOnRemote"],
            section["batteryAnnouncementMode"],
            section["verboseLogging"],
        )
        try:
            from . import remoteintegration
            remoteintegration.apply_local_scripts()
        except Exception:
            log.exception("rsc: failed applying local-scripts after save")
