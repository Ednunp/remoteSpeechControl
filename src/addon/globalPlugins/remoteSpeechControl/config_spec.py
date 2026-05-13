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
    "autoUpdateCheck": "boolean(default=False)",
    "lastUpdateCheckMs": "integer(default=0)",
    "snoozedUpdateVersion": "string(default='')",
    "verboseLogging": "boolean(default=True)",
}


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


def get_verbose() -> bool:
    return bool(config.conf[SECTION]["verboseLogging"])


def get_auto_update_check() -> bool:
    return bool(config.conf[SECTION]["autoUpdateCheck"])


def get_last_update_check_ms() -> int:
    try:
        return int(config.conf[SECTION]["lastUpdateCheckMs"])
    except (TypeError, ValueError):
        return 0


def get_snoozed_update_version() -> str:
    return str(config.conf[SECTION]["snoozedUpdateVersion"] or "")
