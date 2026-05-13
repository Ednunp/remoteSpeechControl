"""GitHub Releases self-updater for Remote Speech Control.

Once a day (if enabled by the user), the running NVDA instance polls
``api.github.com/repos/<owner>/<repo>/releases/latest`` for this add-on,
compares the released tag to the installed version, and offers to
download and install a newer ``.nvda-addon`` from the release's assets.

Bulletproof goals:

- All network and filesystem I/O is wrapped in try/except so that a
  failure cannot crash NVDA.
- The HTTP call runs on a worker thread; the UI is only ever touched
  via wx.CallAfter back on the main thread.
- Version comparison is tuple-of-ints, not lexicographic, so 0.4.10 is
  correctly newer than 0.4.2.
- A "no" answer is persisted; the user is not re-prompted for the same
  version on subsequent automatic checks. Manual "Check now" overrides
  the snooze.
- A failed download or install leaves the previous installation
  untouched. The user is told what went wrong.
- Manifest name on the downloaded bundle is verified before install,
  so the updater cannot accidentally swap the add-on for something
  unrelated served from the same URL.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from . import config_spec
from . import logger

log = logger.get()


ADDON_NAME = "remoteSpeechControl"
ADDON_LABEL = "Remote Speech Control"
GITHUB_OWNER = "Ednunp"
GITHUB_REPO = "remoteSpeechControl"

CHECK_INTERVAL_S = 24 * 3600
RETRY_INTERVAL_S = 30 * 60
STARTUP_GRACE_S = 30
DOWNLOAD_BLOCK = 8192
API_TIMEOUT_S = 20.0
DOWNLOAD_TIMEOUT_S = 300.0

_USER_AGENT = f"{ADDON_NAME}-updater/1"


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def _version_tuple(v: str) -> Tuple[int, ...]:
    """Parse a version string into a tuple of ints suitable for ordering.

    Tolerates leading ``v`` or ``V`` (``v0.4.1`` -> ``0.4.1``) and any
    pre-release suffix joined by ``-``, ``+`` or whitespace (``0.4.1-dev``
    -> ``0.4.1``). Returns ``(0,)`` for empty input or any non-numeric
    component so unparseable strings compare as older than any real
    version.
    """
    if not v:
        return (0,)
    cleaned = re.split(r"[-+ ]", v.strip().lstrip("vV"), 1)[0]
    parts = cleaned.split(".") if cleaned else []
    out = []
    for p in parts:
        try:
            out.append(int(p))
        except ValueError:
            return (0,)
    return tuple(out) if out else (0,)


def _is_newer(remote: str, local: str) -> bool:
    return _version_tuple(remote) > _version_tuple(local)


# ---------------------------------------------------------------------------
# Updater core
# ---------------------------------------------------------------------------

class _Updater:
    def __init__(self) -> None:
        self._api = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
        self._timer: Optional[Any] = None
        self._inflight_lock = threading.Lock()
        self._inflight = False
        self._stopped = False

    # ---- lifecycle ----

    def start(self) -> None:
        self._stopped = False
        if not config_spec.get_auto_update_check():
            log.info("rsc: auto-update disabled in settings; timer not scheduled")
            return
        self._schedule_first()

    def stop(self) -> None:
        self._stopped = True
        self._cancel_timer()

    def refresh_schedule(self) -> None:
        """Re-evaluate the timer after the user changed the auto-update setting."""
        if self._stopped:
            return
        if config_spec.get_auto_update_check():
            self._schedule_first()
        else:
            self._cancel_timer()
            log.info("rsc: auto-update toggled off; timer cancelled")

    def check_now(self, interactive: bool = True) -> None:
        if self._stopped:
            return
        with self._inflight_lock:
            if self._inflight:
                if interactive:
                    self._announce("Update check already in progress.")
                return
            self._inflight = True
        threading.Thread(target=self._run_check, args=(interactive,), daemon=True).start()

    # ---- timer scheduling ----

    def _schedule_first(self) -> None:
        try:
            now_ms = int(time.time() * 1000)
            last_ms = config_spec.get_last_update_check_ms()
            interval_ms = CHECK_INTERVAL_S * 1000
            elapsed_ms = now_ms - last_ms if now_ms >= last_ms else 0
            remaining_ms = interval_ms - elapsed_ms
            if remaining_ms < STARTUP_GRACE_S * 1000:
                remaining_ms = STARTUP_GRACE_S * 1000
            self._schedule_in(remaining_ms)
        except Exception:
            log.exception("rsc: scheduling first update check failed")

    def _schedule_in(self, ms: int) -> None:
        self._cancel_timer()
        if self._stopped:
            return
        try:
            from core import callLater
        except Exception:
            log.warning("rsc: core.callLater unavailable; update timer not started")
            return
        try:
            self._timer = callLater(int(ms), self._on_timer)
            log.info("rsc: next automatic update check in ~%d s", int(ms) // 1000)
        except Exception:
            log.exception("rsc: failed to schedule update check timer")

    def _cancel_timer(self) -> None:
        if self._timer is None:
            return
        try:
            if self._timer.IsRunning():
                self._timer.Stop()
        except Exception:
            pass
        self._timer = None

    def _on_timer(self) -> None:
        self._timer = None
        if self._stopped:
            return
        if not config_spec.get_auto_update_check():
            return
        self.check_now(interactive=False)

    # ---- check pipeline ----

    def _run_check(self, interactive: bool) -> None:
        failed = False
        try:
            info = self._fetch_latest()
            current = self._installed_version()
            self._stamp_last_check()
            self._evaluate(info, current, interactive)
        except Exception:
            failed = True
            log.warning("rsc: update check failed", exc_info=True)
            if interactive:
                self._post_error(
                    "Unable to check for updates. Check your internet connection and try again later.",
                    "Update check failed",
                )
        finally:
            with self._inflight_lock:
                self._inflight = False
        if self._stopped:
            return
        try:
            import wx
            wx.CallAfter(self._schedule_in, (RETRY_INTERVAL_S if failed else CHECK_INTERVAL_S) * 1000)
        except Exception:
            log.exception("rsc: post-check rescheduling failed")

    def _stamp_last_check(self) -> None:
        try:
            import config
            config.conf[config_spec.SECTION]["lastUpdateCheckMs"] = int(time.time() * 1000)
        except Exception:
            log.exception("rsc: could not persist lastUpdateCheckMs")

    def _fetch_latest(self) -> Dict[str, Any]:
        req = urllib.request.Request(self._api, headers={"User-Agent": _USER_AGENT})
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S, context=ctx) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("unexpected payload type from releases API")
        asset_url = None
        asset_name = None
        for asset in (data.get("assets") or []):
            name = str(asset.get("name", ""))
            if name.lower().endswith(".nvda-addon"):
                asset_url = asset.get("browser_download_url")
                asset_name = name
                break
        if not asset_url or not asset_name:
            raise RuntimeError("latest release has no .nvda-addon asset attached")
        return {
            "version": str(data.get("tag_name", "")),
            "name": asset_name,
            "downloadUrl": asset_url,
            "notes": str(data.get("body") or "").strip(),
        }

    def _installed_version(self) -> str:
        try:
            import addonHandler
            for a in addonHandler.getAvailableAddons():
                if a.name == ADDON_NAME:
                    return str(a.version)
        except Exception:
            log.exception("rsc: addonHandler lookup failed")
        return ""

    def _evaluate(self, info: Dict[str, Any], current: str, interactive: bool) -> None:
        remote_v = info["version"]
        if not _is_newer(remote_v, current):
            if interactive:
                self._post_info(
                    f"You are running the latest version of {ADDON_LABEL} ({current}).",
                    "No updates available",
                )
            else:
                log.info("rsc: up to date (installed=%s latest=%s)", current, remote_v)
            return
        if not interactive:
            snoozed = config_spec.get_snoozed_update_version()
            if snoozed and not _is_newer(remote_v, snoozed):
                log.info("rsc: version %s previously declined; not re-prompting", remote_v)
                return
        self._post_prompt(info, current)

    # ---- prompts (always main thread) ----

    def _post_prompt(self, info: Dict[str, Any], current: str) -> None:
        try:
            import wx
            wx.CallAfter(self._do_prompt, info, current)
        except Exception:
            log.exception("rsc: posting update prompt failed")

    def _post_info(self, message: str, title: str) -> None:
        try:
            import wx
            wx.CallAfter(self._show_box, message, title, wx.OK | wx.ICON_INFORMATION)
        except Exception:
            log.exception("rsc: posting info dialog failed")

    def _post_error(self, message: str, title: str) -> None:
        try:
            import wx
            wx.CallAfter(self._show_box, message, title, wx.OK | wx.ICON_ERROR)
        except Exception:
            log.exception("rsc: posting error dialog failed")

    def _show_box(self, message: str, title: str, style: int) -> int:
        try:
            from gui import messageBox, mainFrame
            return messageBox(message, title, style, mainFrame)
        except Exception:
            log.exception("rsc: messageBox failed")
            return 0

    def _do_prompt(self, info: Dict[str, Any], current: str) -> None:
        try:
            import wx
            notes = info.get("notes") or ""
            message = (
                f"A new version of {ADDON_LABEL} is available.\n\n"
                f"Installed: {current or 'unknown'}\n"
                f"Available: {info['version']}\n\n"
                "Download and install it now?"
            )
            if notes:
                message = message + "\n\nRelease notes:\n" + notes
            choice = self._show_box(message, f"Update available — {ADDON_LABEL}", wx.YES_NO | wx.ICON_QUESTION)
            if choice == wx.YES:
                self._download_and_install(info)
            else:
                self._snooze_version(info["version"])
                log.info("rsc: user declined update to %s", info["version"])
        except Exception:
            log.exception("rsc: update prompt failed")

    def _snooze_version(self, version: str) -> None:
        try:
            import config
            config.conf[config_spec.SECTION]["snoozedUpdateVersion"] = version
        except Exception:
            log.exception("rsc: could not persist snoozedUpdateVersion")

    # ---- download + install ----

    def _download_and_install(self, info: Dict[str, Any]) -> None:
        try:
            import globalVars
            updates_dir = os.path.join(globalVars.appArgs.configPath, "addonUpdates")
        except Exception:
            log.exception("rsc: cannot resolve updates directory")
            return
        try:
            os.makedirs(updates_dir, exist_ok=True)
        except Exception:
            log.exception("rsc: cannot create updates directory %s", updates_dir)
            return
        dest = os.path.join(updates_dir, info["name"])
        if not self._download_with_progress(info["downloadUrl"], dest):
            return
        self._install_bundle(dest)

    def _download_with_progress(self, url: str, dest: str) -> bool:
        import wx
        try:
            from gui import mainFrame
        except Exception:
            log.exception("rsc: gui.mainFrame unavailable")
            return False
        try:
            mainFrame.prePopup()
        except Exception:
            pass
        progress = wx.ProgressDialog(
            "Downloading update",
            f"Downloading {os.path.basename(dest)}...",
            style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME | wx.PD_REMAINING_TIME | wx.PD_AUTO_HIDE,
            parent=mainFrame,
        )
        cancelled = [False]

        def cb(percent: int) -> bool:
            try:
                result = progress.Update(percent)
                # wxPython returns (keep_going, skipped) in recent versions
                if isinstance(result, tuple):
                    keep_going = bool(result[0])
                else:
                    keep_going = bool(result)
            except Exception:
                return False
            if not keep_going:
                cancelled[0] = True
                return True
            return False

        success = False
        try:
            try:
                self._http_download(url, dest, cb)
            except Exception:
                log.exception("rsc: download failed")
                self._show_box(
                    "Unable to download the update. Check your internet connection.",
                    "Download failed",
                    wx.OK | wx.ICON_ERROR,
                )
                return False
            success = not cancelled[0]
            return success
        finally:
            try:
                progress.Destroy()
            except Exception:
                pass
            try:
                mainFrame.postPopup()
            except Exception:
                pass
            if not success:
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                except Exception:
                    pass

    def _http_download(self, url: str, dest: str, progress_cb: Callable[[int], bool]) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        ctx = ssl.create_default_context()
        ExecAndPump = self._get_exec_and_pump()

        def do_download() -> None:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S, context=ctx) as resp:
                total = 0
                try:
                    total = int(resp.headers.get("content-length") or 0)
                except (TypeError, ValueError):
                    total = 0
                read = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(DOWNLOAD_BLOCK)
                        if not chunk:
                            break
                        f.write(chunk)
                        read += len(chunk)
                        if progress_cb is not None and total > 0:
                            pct = int(read / total * 100)
                            if pct > 100:
                                pct = 100
                            if progress_cb(pct):
                                return

        if ExecAndPump is not None:
            ExecAndPump(do_download)
        else:
            do_download()

    def _install_bundle(self, path: str) -> None:
        import wx
        try:
            from gui import mainFrame
        except Exception:
            mainFrame = None
        if mainFrame is not None:
            try:
                mainFrame.prePopup()
            except Exception:
                pass
        try:
            try:
                import addonHandler
                bundle = addonHandler.AddonBundle(path)
                manifest_name = bundle.manifest.get("name") or ""
                if manifest_name != ADDON_NAME:
                    raise RuntimeError(f"bundle manifest name mismatch: {manifest_name!r}")
                prev = None
                for a in addonHandler.getAvailableAddons():
                    if a.name == manifest_name:
                        prev = a
                        break
                ExecAndPump = self._get_exec_and_pump()
                if ExecAndPump is not None:
                    ExecAndPump(addonHandler.installAddonBundle, bundle)
                else:
                    addonHandler.installAddonBundle(bundle)
                if prev is not None:
                    try:
                        prev.requestRemove()
                    except Exception:
                        log.exception("rsc: could not request removal of previous addon")
                try:
                    os.remove(path)
                except Exception:
                    pass
                # Clear snooze marker — user has installed something now
                try:
                    import config
                    config.conf[config_spec.SECTION]["snoozedUpdateVersion"] = ""
                except Exception:
                    pass
                self._prompt_restart()
            except Exception:
                log.exception("rsc: install failed")
                self._show_box(
                    "Failed to install the update. Your existing installation is unchanged.",
                    "Install failed",
                    wx.OK | wx.ICON_ERROR,
                )
        finally:
            if mainFrame is not None:
                try:
                    mainFrame.postPopup()
                except Exception:
                    pass

    def _prompt_restart(self) -> None:
        try:
            from gui.addonGui import promptUserForRestart
            promptUserForRestart()
            return
        except Exception:
            pass
        import wx
        self._show_box(
            f"{ADDON_LABEL} update installed. Restart NVDA to apply.",
            "Update installed",
            wx.OK | wx.ICON_INFORMATION,
        )

    def _get_exec_and_pump(self) -> Optional[Callable[..., Any]]:
        try:
            from systemUtils import ExecAndPump  # type: ignore[no-redef]
            return ExecAndPump
        except Exception:
            pass
        try:
            from gui import ExecAndPump  # type: ignore[no-redef]
            return ExecAndPump
        except Exception:
            return None

    def _announce(self, text: str) -> None:
        try:
            import ui
            ui.message(text)
        except Exception:
            log.warning("rsc: ui.message failed", exc_info=True)


# ---------------------------------------------------------------------------
# Module-level singleton API
# ---------------------------------------------------------------------------

_singleton: Optional[_Updater] = None


def start() -> None:
    global _singleton
    if _singleton is None:
        _singleton = _Updater()
    _singleton.start()


def stop() -> None:
    if _singleton is not None:
        _singleton.stop()


def check_now(interactive: bool = True) -> None:
    if _singleton is not None:
        _singleton.check_now(interactive=interactive)


def refresh_schedule() -> None:
    if _singleton is not None:
        _singleton.refresh_schedule()
