"""NVDA configuration schema for Remote Speech Control.

NVDA's config validates against a configspec. Registering ours up front gives
us defaults and type-checking; values live in nvda.ini under [remoteSpeechControl].
"""
from __future__ import annotations

import config

SECTION = "remoteSpeechControl"

SPEC = {
    "password": "string(default='')",
    "autoRequestOnConnect": "boolean(default=False)",
    "allowAutoMute": "boolean(default=False)",
    "keepSynthSettingsRingLocal": "boolean(default=False)",
    # Battery announcement settings. ``announceLocalBatteryOnRemote``
    # toggles the whole feature on/off. ``batteryAnnouncementMode`` picks
    # which of the three orderings runs when the chord is pressed and the
    # user is F11'd in. Default mode is "local_then_remote" so the user
    # hears their own battery as fast as possible (synchronous local
    # announcement) and the remote arrives as a follow-up.
    "announceLocalBatteryOnRemote": "boolean(default=False)",
    "batteryAnnouncementMode": "option('local_then_remote', 'remote_then_local', 'local_only', default='local_then_remote')",
    "verboseLogging": "boolean(default=True)",
}

# Internal ordering used by the settings UI ComboBox. Index → config value.
BATTERY_MODE_ORDER = (
    "local_then_remote",
    "remote_then_local",
    "local_only",
)
BATTERY_MODE_LABELS = (
    "Local battery, then remote",
    "Remote battery, then local",
    "Local battery only",
)


def install() -> None:
    config.conf.spec[SECTION] = SPEC


def get_password() -> str:
    return str(config.conf[SECTION]["password"] or "")


def get_auto_request() -> bool:
    return bool(config.conf[SECTION]["autoRequestOnConnect"])


def get_allow_auto_mute() -> bool:
    return bool(config.conf[SECTION]["allowAutoMute"])


def get_keep_synth_settings_ring_local() -> bool:
    return bool(config.conf[SECTION]["keepSynthSettingsRingLocal"])


def get_announce_local_battery_on_remote() -> bool:
    return bool(config.conf[SECTION]["announceLocalBatteryOnRemote"])


def get_battery_announcement_mode() -> str:
    mode = str(config.conf[SECTION]["batteryAnnouncementMode"] or "local_then_remote")
    if mode not in BATTERY_MODE_ORDER:
        mode = "local_then_remote"
    return mode


def get_verbose() -> bool:
    return bool(config.conf[SECTION]["verboseLogging"])
