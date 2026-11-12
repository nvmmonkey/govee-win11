"""Python-side text in the user's language (中文 / English).

The web UI has its own dictionary (``web/i18n.js``); this table only holds what Python
itself must word: the tray menu and tooltip, the window title, the shutdown block
reason, and the error / warning messages the Api returns for the UI to show as-is.

Language setting (``Settings.language``): ``"auto"`` | ``"zh"`` | ``"en"``. ``auto``
follows the Windows display language (``GetUserDefaultUILanguage``: primary language
Chinese -> ``zh``, anything else -> ``en``), with the C locale as fallback.
"""
from __future__ import annotations

import locale
import os
import sys

from .config import LANGUAGES as SETTINGS  # values of Settings.language
from .config import normalise_language as normalise_setting

LANGUAGES = ("zh", "en")             # resolved languages
_LANG_CHINESE = 0x04                 # PRIMARYLANGID of every Chinese LANGID

MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "app.title": "Govee 控制台",
        "tray.show": "显示主窗口",
        "tray.autostart": "开机自启",
        "tray.quit": "退出",
        "power.block_reason": "正在关闭灯带…",
        "cli.description": "Govee H6125 控制台",
        "cli.tray": "启动到托盘（窗口隐藏）并恢复上次的持续性模式",
        "cli.selftest": "自检：加载所有依赖和资源后退出（不开窗口、不连蓝牙）",
        # link status texts (link.py keeps the Chinese ones as its constants)
        "link.busy_elsewhere": "连不上灯带：可能被手机占用，请关闭手机蓝牙或 Govee App",
        "link.bt_unavailable": "电脑蓝牙不可用（请检查蓝牙是否开启/设备管理器）",
        "link.not_found": "没有找到 H6125 灯带，请确认灯带通电、手机 Govee App 已断开",
        "link.choose_other": "如果换了灯带，请在「设置 → 设备」里选择另一条。",
        # Api errors
        "err.generic": "操作失败：{detail}",
        "err.send_failed": "发送失败：与灯带的连接中断，请重试",
        "err.bad_color": "颜色格式不正确：{value}",
        "err.need_color": "至少需要一种颜色",
        "err.brightness_range": "亮度应在 0–100 之间",
        "err.color_count": "颜色数量不正确",
        "err.hue": "色相不正确",
        "err.palette_params": "配色参数不正确",
        "err.palette_name": "请输入调色板名称",
        "err.no_palette": "没有这个调色板：{name}",
        "err.keep_palette": "至少保留一个调色板",
        "err.unknown_effect": "未知效果：{effect}",
        "err.speed_range": "速度应在 0–100 之间",
        "err.width": "宽度不正确",
        "err.need_effect": "至少选择一个效果",
        "err.preset_name": "请输入预设名称",
        "err.no_preset": "没有这个预设：{name}",
        "err.no_scene": "没有这个情景：{id}",
        "err.screen_params": "屏幕同步参数不正确",
        "err.monitor": "显示器编号不正确",
        "err.saturation": "饱和度应在 0–3 之间",
        "err.transition_s": "过渡时间应在 0.3–5 秒之间",
        "err.gain": "RGB 增益不正确",
        "err.gain_range": "RGB 增益应在 0–3 之间",
        "err.screen_mode": "屏幕同步模式不正确",
        "err.tempo": "节奏应在 0–100 之间",
        "err.music_params": "音乐参数不正确",
        "err.sensitivity": "灵敏度应在 0.1–5 之间",
        "err.segment_index": "段号超出范围",
        "err.calibration_points": "校准数据不完整：需要 {n} 个点",
        "err.calibration_format": "校准数据格式不正确",
        "err.calibration_coord": "校准坐标应在 0–1 之间",
        "err.autostart_unavailable": "开机自启不可用",
        "err.power_sync_param": "开关机联动参数不正确",
        "err.transition_params": "过渡方式参数不正确",
        "err.transition_rate": "每段每秒刷新次数只能是 3、5、8 或 10",
        "err.transition_mix": "混合方式不正确",
        "err.color_cal_params": "颜色校准参数不正确",
        "err.color_cal_range": "颜色校准应在 0–100% 之间",
        "err.restore_failed": "没能恢复之前的模式，请重新选择",
        "err.language": "语言只能是自动、中文或 English",
        "err.bad_address": "设备地址格式不正确：{value}",
        "err.scan_unavailable": "无法搜索设备",
    },
    "en": {
        "app.title": "Govee Control",
        "tray.show": "Show window",
        "tray.autostart": "Start with Windows",
        "tray.quit": "Quit",
        "power.block_reason": "Turning the light strip off…",
        "cli.description": "Govee H6125 control",
        "cli.tray": "start in the tray (window hidden) and restore the last continuous mode",
        "cli.selftest": "self-test: load every dependency and resource, then exit "
                        "(no window, no Bluetooth)",
        "link.busy_elsewhere": "Can't reach the strip. Your phone may be connected to it: "
                               "turn off the phone's Bluetooth or close the Govee app.",
        "link.bt_unavailable": "This PC's Bluetooth is unavailable (check that it is on, "
                               "or look in Device Manager).",
        "link.not_found": "No H6125 strip found. Make sure the strip is powered on and "
                          "the phone's Govee app has disconnected.",
        "link.choose_other": "If you replaced the strip, choose another one in "
                             "Settings → Device.",
        "err.generic": "Something went wrong: {detail}",
        "err.send_failed": "Couldn't send: the connection to the strip dropped. Please try again.",
        "err.bad_color": "Invalid colour: {value}",
        "err.need_color": "At least one colour is needed",
        "err.brightness_range": "Brightness must be between 0 and 100",
        "err.color_count": "Invalid number of colours",
        "err.hue": "Invalid hue",
        "err.palette_params": "Invalid palette options",
        "err.palette_name": "Enter a palette name",
        "err.no_palette": "No such palette: {name}",
        "err.keep_palette": "Keep at least one palette",
        "err.unknown_effect": "Unknown effect: {effect}",
        "err.speed_range": "Speed must be between 0 and 100",
        "err.width": "Invalid width",
        "err.need_effect": "Choose at least one effect",
        "err.preset_name": "Enter a name for the DIY",
        "err.no_preset": "No such DIY: {name}",
        "err.no_scene": "No such scene: {id}",
        "err.screen_params": "Invalid screen sync options",
        "err.monitor": "Invalid display number",
        "err.saturation": "Saturation must be between 0 and 3",
        "err.transition_s": "Transition time must be between 0.3 and 5 s",
        "err.gain": "Invalid RGB gain",
        "err.gain_range": "RGB gain must be between 0 and 3",
        "err.screen_mode": "Invalid screen sync mode",
        "err.tempo": "Tempo must be between 0 and 100",
        "err.music_params": "Invalid music options",
        "err.sensitivity": "Sensitivity must be between 0.1 and 5",
        "err.segment_index": "Segment number out of range",
        "err.calibration_points": "Incomplete calibration: {n} points needed",
        "err.calibration_format": "Invalid calibration data",
        "err.calibration_coord": "Calibration coordinates must be between 0 and 1",
        "err.autostart_unavailable": "Start with Windows isn't available",
        "err.power_sync_param": "Invalid power sync option",
        "err.transition_params": "Invalid transition options",
        "err.transition_rate": "Updates per second must be 3, 5, 8 or 10",
        "err.transition_mix": "Invalid blending option",
        "err.color_cal_params": "Invalid colour calibration",
        "err.color_cal_range": "Colour calibration must be between 0 and 100%",
        "err.restore_failed": "Couldn't restore the previous mode. Please choose it again.",
        "err.language": "Language must be Auto, 中文 or English",
        "err.bad_address": "Invalid device address: {value}",
        "err.scan_unavailable": "Device search isn't available",
    },
}


def _ui_langid() -> int:
    """LANGID of the Windows display language (raises off Windows)."""
    import ctypes
    return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())


def _locale_name() -> str:
    try:
        name = locale.getlocale()[0]
    except Exception:
        name = None
    return name or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""


def _from_locale_name(name: str) -> str:
    n = (name or "").strip().lower()
    return "zh" if n.startswith(("zh", "chinese")) else "en"


def detect_system_language() -> str:
    """``zh`` when the Windows display language is Chinese, else ``en``; off Windows
    (or if the call fails) the locale decides."""
    if sys.platform == "win32":
        try:
            return "zh" if (_ui_langid() & 0x3FF) == _LANG_CHINESE else "en"
        except Exception:
            pass
    return _from_locale_name(_locale_name())


def system_language() -> str:
    """What ``auto`` resolves to (tests pin this)."""
    return detect_system_language()


def resolve(setting) -> str:
    """``"auto" | "zh" | "en"`` (anything else counts as auto) -> ``"zh" | "en"``."""
    s = normalise_setting(setting)
    return system_language() if s == "auto" else s


def tr(lang: str, key: str, **params) -> str:
    """Text for ``key`` in ``lang`` (falls back to Chinese, then to the key itself)."""
    table = MESSAGES.get(lang) or MESSAGES["zh"]
    text = table.get(key)
    if text is None:
        text = MESSAGES["zh"].get(key, key)
    if params:
        try:
            text = text.format(**params)
        except (KeyError, IndexError, ValueError):
            pass
    return text
