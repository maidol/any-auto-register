"""Camoufox 默认插件开关。

Camoufox 默认会装 uBlock Origin，它跑在单独的 WebExtensions 进程里，
每次启动浏览器都要重新加载过滤规则。设 CAMOUFOX_UBLOCK=false 可以不装它。
"""
import os

from camoufox.addons import DefaultAddons

_OFF_VALUES = {"0", "false", "no", "off"}


def ublock_enabled() -> bool:
    value = os.environ.get("CAMOUFOX_UBLOCK", "")
    return value.strip().lower() not in _OFF_VALUES


def apply_ublock_switch(launch_opts: dict) -> dict:
    """CAMOUFOX_UBLOCK 关闭时往 launch_opts 里加 exclude_addons，原地修改并返回同一个 dict。"""
    if not ublock_enabled():
        launch_opts["exclude_addons"] = [DefaultAddons.UBO]
    return launch_opts
