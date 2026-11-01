"""告警通道层 —— 把一条告警投递到各个出口。

为什么要有这一层
================
哨兵原本只把告警打印到终端。终端是**有人盯着时**才有用的地方：
Agent 在后台跑一整晚，没人看着屏幕，告警等于没发出去。

所以这里把「告警」抽象成一组**通道**（channel）：

* :class:`ConsoleChannel` —— 终端输出；
* :class:`FileChannel` —— JSONL 落盘，便于事后复盘与机器消费；
* :class:`DesktopChannel` —— 操作系统级弹窗（Windows / macOS / Linux）；
* :class:`SoundChannel` —— 系统提示音；
* :class:`WebhookChannel` —— 钉钉 / 飞书 / 通用 HTTP。

每个通道实现同一个契约：

* ``send(alert) -> bool`` —— 投递一条告警，返回是否成功；
* **绝不向调用方抛异常** —— 通道失败由
  :class:`~src.core.notifier.Notifier` 统一隔离成一条 warning，
  绝不影响哨兵主流程（红线 R5：旁路失败）。

安全约定
========
告警正文来自**被监控 Agent 的日志**，属于不可信数据。凡是需要调用
外部进程的地方，告警正文一律通过**环境变量**传递，绝不拼进命令行
字符串 —— 命令行会被 shell 与参数解析器重新解释，等于给日志内容开了
一条注入通道。
"""

from __future__ import annotations

import abc
import base64
import hashlib
import hmac
import importlib
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urlparse

try:
    import requests
except ImportError:  # pragma: no cover —— requests 是声明依赖，正常都在
    requests = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DESKTOP_TITLE",
    "DEFAULT_FILE_CHANNEL_PATH",
    "LEVEL_RANK",
    "BaseChannel",
    "ConsoleChannel",
    "DesktopChannel",
    "FileChannel",
    "SoundChannel",
    "WebhookChannel",
    "alert_level",
    "format_alert",
    "level_rank",
]


# ---------------------------------------------------------------------------
# 等级
# ---------------------------------------------------------------------------

#: 等级排序表 —— ``min_level`` 过滤与显示都以此为准。
#:
#: 除了配置里约定的 ``info`` / ``warn`` / ``critical``，这里还收了一组
#: 常见别名，避免规则层将来引入新等级名时被静默丢弃。
LEVEL_RANK: Dict[str, int] = {
    "info": 0,
    "debug": 0,
    "notice": 0,
    "warn": 1,
    "warning": 1,
    "critical": 2,
    "error": 2,
    "fatal": 2,
}

#: 缺省等级（与规则层 ``_build_alert`` 的默认值保持一致）。
DEFAULT_LEVEL = "warn"


def level_rank(level: Any) -> int:
    """返回等级的排序值。

    :param level: 等级字符串；大小写与首尾空白不敏感。
    :return: 排序值，数值越大越严重。

    **无法识别的等级按最高级处理**（而不是丢弃）—— 沿用项目里
    「缺失绝不等同于安全」的原则：宁可多报一条，也不让一条新等级
    的告警被 ``min_level`` 悄悄吃掉。``None`` / 空串按缺省等级
    ``warn`` 处理，因为那正是规则层的默认取值。
    """
    key = str(level or "").strip().lower()
    if not key:
        return LEVEL_RANK[DEFAULT_LEVEL]
    return LEVEL_RANK.get(key, LEVEL_RANK["critical"])


def alert_level(alert: Any) -> str:
    """取出告警字典里的等级字符串（缺省 ``warn``）。"""
    if not isinstance(alert, dict):
        return DEFAULT_LEVEL
    return str(alert.get("level") or DEFAULT_LEVEL).strip().lower()


# ---------------------------------------------------------------------------
# 告警 → 人话
# ---------------------------------------------------------------------------

#: 弹窗 / 提示音场景下正文的最大长度。
#:
#: Windows 气泡提示本身只显示很短一截，无限截断没有意义；限制长度是
#: 为了不让一条超长日志把环境变量撑爆。
MAX_BODY_CHARS = 1800


def format_alert(alert: Any) -> str:
    """把告警字典渲染成人可读的多行文本。

    告警字典正常来自 :meth:`SentinelEngine._build_alert`，但告警层
    也可能被外部系统直接投递 **缺字段** 的 JSON，所以这里全程用
    ``get`` + 默认值，绝不做 KeyError 假设。

    :param alert: 告警字典；非字典时按空字典处理。
    :return: 多行文本，形如::

        [CRITICAL] DANGEROUS: 匹配到高危命令: rm -rf
        规则：dangerous_command
        工具：Bash
        命令：rm -rf /tmp/x
        人话解释：清理临时构建产物。原因：…
    """
    data: Dict[str, Any] = alert if isinstance(alert, dict) else {}

    level = str(data.get("level") or DEFAULT_LEVEL).upper()
    verdict = str(data.get("verdict") or "UNKNOWN").upper()
    reason = str(data.get("reason") or "").strip()

    head = f"[{level}] {verdict}"
    if reason:
        head = f"{head}: {reason}"
    lines: List[str] = [head]

    rule_id = str(data.get("rule_id") or "").strip()
    if rule_id:
        lines.append(f"规则：{rule_id}")

    lines.append(f"工具：{data.get('tool') or '-'!s}")
    lines.append(f"命令：{data.get('command') or '-'!s}")

    explanation = str(data.get("explanation") or "").strip()
    degraded = bool(data.get("explanation_degraded"))
    # L2 没给出解释时，用规则层的 reason 顶上 —— 至少让看告警的人
    # 知道「为什么被判危险」，而不是只看到一个空壳。
    body = explanation or reason
    if body:
        if degraded:
            body = f"（降级：本地模型不可用）{body}"
        lines.append(f"人话解释：{body}")
    elif degraded:
        lines.append("人话解释：（降级：本地模型不可用）")

    # --- 合并提示（v2.0.0）---
    # 同一条告警连续触发时只推一次，但用户有权知道
    # 「刚才其实发生了 N 次」—— 合并不是隐瞒。
    try:
        merged = int(data.get("merged_count") or 0)
    except (TypeError, ValueError):
        merged = 0
    if merged > 0:
        lines.append(f"（另有 {merged} 条同类告警已合并）")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 通道基类
# ---------------------------------------------------------------------------


class BaseChannel(abc.ABC):
    """告警通道基类。

    契约只有一条，但很硬：**``send()`` 绝不向调用方抛异常**。
    通道失败必须以 ``False`` 的形式返回，由上层统一隔离成日志告警。

    **通知型 vs 记录型**（v2.0.0）
    =============================

    通道分两类：

    * **记录型**（``console`` / ``file``）—— 是**流水账**，
      每条告警都要落账，绝不能丢；
    * **通知型**（``desktop`` / ``sound`` / ``webhook``）——
      是**敲门**，目的是把人叫醒。

    半夜被 5 条一样的 STUCK 各敲一次门，比漏报一次更让人讨厌。
    所以通知型通道会走 :class:`src.core.notifier.AlertCoalescer`
    的合并窗口（见 :attr:`is_notification`）。
    """

    #: 通道名（用于日志与测试断言）。
    name: str = "base"

    #: 这是不是**通知型**通道（会被合并窗口拦一下）。
    #:
    #: 记录型通道必须为 ``False`` —— 流水账不能因为「重复」而少记。
    is_notification: bool = False

    @abc.abstractmethod
    def send(self, alert: Dict[str, Any]) -> bool:
        """投递一条告警。

        :param alert: 告警字典。
        :return: 是否投递成功。**实现绝不能抛异常。**
        """

    def close(self) -> None:
        """释放通道占用的资源（默认无操作）。"""
        return


# ---------------------------------------------------------------------------
# 终端通道
# ---------------------------------------------------------------------------


class ConsoleChannel(BaseChannel):
    """终端通道 —— 把告警写到标准输出。

    这里用 ``logging`` 而不是 ``print``：库代码不直接写标准输出，
    这样宿主程序可以重定向、过滤或静默哨兵的输出。
    """

    name = "console"

    def __init__(
        self,
        *,
        stream: Any = None,
        level: int = logging.INFO,
        logger_name: str = "sentinel.alert.console",
    ) -> None:
        """
        :param stream: 输出流；``None`` 表示在**首次投递时**取
            ``sys.stdout``（延迟解析，便于测试替换标准输出）。
        :param level: 输出级别。
        :param logger_name: 使用的 logger 名。
        """
        self._stream = stream
        self._level = level
        self._handler: Optional[logging.Handler] = None
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel(level)

    def _ensure_handler(self) -> logging.Handler:
        """惰性挂上输出处理器（避免构造期就锁定 stdout）。"""
        if self._handler is not None:
            return self._handler
        stream = self._stream if self._stream is not None else sys.stdout
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.setLevel(self._level)
        self.logger.addHandler(handler)
        # 与 ``sentinel.alert`` 保持一致：不向上冒泡，避免重复输出。
        self.logger.propagate = False
        self._handler = handler
        return handler

    def send(self, alert: Dict[str, Any]) -> bool:
        """把告警写到终端。"""
        try:
            self._ensure_handler()
            message = format_alert(alert)
            self.logger.log(self._level, "%s", message)
            return True
        except Exception as exc:  # noqa: BLE001 —— 通道绝不外泄异常
            logger.warning("终端通道投递失败：%s: %s", type(exc).__name__, exc)
            return False

    def close(self) -> None:
        """摘掉输出处理器。"""
        if self._handler is not None:
            try:
                self.logger.removeHandler(self._handler)
                self._handler.close()
            except (OSError, ValueError) as exc:
                # 摘不掉输出处理器 —— 通知会继续往已关的流里写。
                logger.warning(
                    "摘除输出处理器失败：%s: %s", type(exc).__name__, exc
                )
            self._handler = None


# ---------------------------------------------------------------------------
# 文件通道
# ---------------------------------------------------------------------------

#: 文件通道的默认落盘位置（与 ``config/config.yaml`` 保持一致）。
DEFAULT_FILE_CHANNEL_PATH = os.path.join("logs", "alerts.jsonl")


class FileChannel(BaseChannel):
    """文件通道 —— 每条告警追加一行 JSON（JSONL）。

    选 JSONL 而不是纯文本：告警日志往往要被 ``jq`` / 脚本批量消费，
    一行一个对象是最省事的结构。
    """

    name = "file"

    def __init__(self, path: Optional[str] = None) -> None:
        """
        :param path: 目标文件路径；``None`` 时用
            :data:`DEFAULT_FILE_CHANNEL_PATH`。
        """
        self.path = os.path.abspath(path or DEFAULT_FILE_CHANNEL_PATH)
        self._handle: Optional[Any] = None

    def _ensure_handle(self) -> Any:
        """惰性打开文件句柄（首次投递时才创建目录与文件）。"""
        if self._handle is not None and not self._handle.closed:
            return self._handle
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 这个句柄是**长驻**的（写一次告警开一次文件太浪费），
        # 生命周期由 close() 管，所以不能用 with。
        self._handle = open(  # noqa: SIM115
            self.path, "a", encoding="utf-8"
        )
        return self._handle

    def send(self, alert: Dict[str, Any]) -> bool:
        """追加一行 JSON。"""
        try:
            handle = self._ensure_handle()
            payload = alert if isinstance(alert, dict) else {}
            line = json.dumps(payload, ensure_ascii=False, default=str)
            handle.write(line + "\n")
            handle.flush()
            return True
        except (OSError, TypeError, ValueError) as exc:
            logger.warning(
                "文件通道写入失败（%s）：%s: %s", self.path, type(exc).__name__, exc
            )
            return False

    def close(self) -> None:
        """关闭文件句柄（可重复调用）。"""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.close()
        except OSError as exc:
            logger.warning("关闭文件通道失败（%s）：%s", self.path, exc)


# ---------------------------------------------------------------------------
# 桌面通知通道
# ---------------------------------------------------------------------------

#: 桌面通知的默认标题。
DEFAULT_DESKTOP_TITLE = "哨兵告警"

#: 调用外部程序的默认超时（秒）。
DEFAULT_TIMEOUT = 10

#: Windows 气泡提示脚本。
#:
#: 脚本里**只有变量引用**，告警正文一律从环境变量读取 —— 绝不把
#: 不可信的日志内容拼进 PowerShell 代码里。
_WIN_BALLOON_SCRIPT = (
    "$ErrorActionPreference = 'Stop'\n"
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -AssemblyName System.Drawing\n"
    "$icon = New-Object System.Windows.Forms.NotifyIcon\n"
    "$icon.Icon = [System.Drawing.SystemIcons]::Warning\n"
    "$icon.Visible = $true\n"
    "$tip = [System.Windows.Forms.ToolTipIcon]::Warning\n"
    "$icon.ShowBalloonTip(10000, $env:SENTINEL_ALERT_TITLE, "
    "$env:SENTINEL_ALERT_BODY, $tip)\n"
    "Start-Sleep -Seconds 12\n"
    "$icon.Dispose()\n"
)

#: macOS 通知脚本（同样只引用环境变量）。
_MAC_NOTIFY_SCRIPT = (
    'display notification (system attribute "SENTINEL_ALERT_BODY") '
    'with title (system attribute "SENTINEL_ALERT_TITLE")'
)


def detect_platform() -> str:
    """返回当前平台标识。

    :return: ``windows`` / ``macos`` / ``linux`` / ``unknown``。

    抽成独立函数是为了让测试可以替换它 —— 平台相关的分支必须有
    离线可测的入口。
    """
    import platform as _platform

    system = _platform.system().strip().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return "unknown"


def load_plyer_notifier() -> Optional[Any]:
    """尝试加载可选依赖 ``plyer`` 的通知函数。

    ``plyer`` 不在依赖清单里（哨兵要尽量零依赖），所以这里动态导入，
    导入失败就当没有这个能力。

    :return: ``plyer.notification.notify`` 函数，或 ``None``。
    """
    try:
        module = importlib.import_module("plyer.notification")
    except (ImportError, OSError, AttributeError):
        return None
    return getattr(module, "notify", None)


def _windows_creation_flags() -> int:
    """返回 Windows 下隐藏子进程窗口的创建标志。

    只用 ``CREATE_NO_WINDOW``：它与 ``DETACHED_PROCESS`` 同时使用会
    互相覆盖（Windows 会忽略其中一个），反而可能弹出黑框或直接报错。
    """
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)


class DesktopChannel(BaseChannel):
    """桌面通知通道 —— 操作系统级弹窗。

    回退顺序：

    1. 可选依赖 ``plyer``（若已安装）；
    2. Windows —— 拉起 PowerShell，用
       ``System.Windows.Forms.NotifyIcon.ShowBalloonTip`` 显示气泡；
    3. macOS —— ``osascript display notification``；
    4. Linux —— ``notify-send``；
    5. 都没有 —— 记一条 warning，返回 ``False``。

    弹窗正文一律通过**环境变量**传给子进程，命令行里只出现变量名，
    因此被监控日志里的任何内容都不可能被当作脚本执行。
    """

    name = "desktop"

    #: 弹窗是**通知型**通道 —— 重复告警会被合并窗口拦一下。
    is_notification = True

    def __init__(
        self,
        *,
        title: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        :param title: 弹窗标题。
        :param timeout: 调用外部程序的超时（秒）。
        """
        self.title = str(title or DEFAULT_DESKTOP_TITLE)
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self._children: List[Any] = []

    # -- 对外接口 -------------------------------------------------------
    def send(self, alert: Dict[str, Any]) -> bool:
        """弹出桌面通知。"""
        try:
            message = format_alert(alert)
        except Exception as exc:  # noqa: BLE001 —— 通道绝不外泄异常
            logger.warning("桌面通知文案渲染失败：%s: %s", type(exc).__name__, exc)
            return False

        notifier = load_plyer_notifier()
        if notifier is not None:
            try:
                notifier(
                    title=self.title,
                    message=message,
                    timeout=self.timeout,
                )
                return True
            except Exception as exc:  # noqa: BLE001 —— 换回退路径即可
                logger.warning(
                    "plyer 桌面通知失败，改用系统回退方式：%s: %s",
                    type(exc).__name__,
                    exc,
                )

        platform_name = detect_platform()
        if platform_name == "windows":
            return self._send_windows(message)
        if platform_name == "macos":
            return self._send_macos(message)
        if platform_name == "linux":
            return self._send_linux(message)

        logger.warning("当前平台没有可用的桌面通知方式：%s", platform_name)
        return False

    def close(self) -> None:
        """回收已经结束的通知子进程。"""
        alive: List[Any] = []
        for child in self._children:
            try:
                if child.poll() is None:
                    alive.append(child)
            except (OSError, ValueError):
                continue
        self._children = alive

    # -- 平台实现 -------------------------------------------------------
    def _send_windows(self, message: str) -> bool:
        """Windows：PowerShell + ``NotifyIcon`` 气泡提示。"""
        env = dict(os.environ)
        env["SENTINEL_ALERT_TITLE"] = self.title
        env["SENTINEL_ALERT_BODY"] = message[:MAX_BODY_CHARS]
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-Command",
            _WIN_BALLOON_SCRIPT,
        ]
        try:
            child = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_windows_creation_flags(),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "启动 PowerShell 桌面通知失败：%s: %s", type(exc).__name__, exc
            )
            return False
        self._children.append(child)
        self._watch(child)
        return True

    def _watch(self, child: Any) -> None:
        """后台看护子进程：超时即杀掉，避免留下僵尸进程。"""
        timeout = max(self.timeout, 1.0)

        def _reap() -> None:
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except (OSError, ValueError) as exc:
                    # 杀不掉 —— 可能已经自己退了。要留痕，
                    # 否则「桌面上弹了个窗，关不掉」这类问题查不出来。
                    logger.warning(
                        "强杀桌面提示进程失败：%s: %s",
                        type(exc).__name__,
                        exc,
                    )
            except (OSError, ValueError) as exc:
                # 等子进程退出的过程出错 —— 看护到此为止。
                logger.warning(
                    "等待桌面提示进程退出失败：%s: %s",
                    type(exc).__name__,
                    exc,
                )

        thread = threading.Thread(
            target=_reap,
            name="sentinel-desktop",
            daemon=True,
        )
        thread.start()

    def _send_macos(self, message: str) -> bool:
        """macOS：``osascript display notification``。"""
        env = dict(os.environ)
        env["SENTINEL_ALERT_TITLE"] = self.title
        env["SENTINEL_ALERT_BODY"] = message[:MAX_BODY_CHARS]
        cmd = ["osascript", "-e", _MAC_NOTIFY_SCRIPT]
        try:
            result = subprocess.run(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("macOS 桌面通知失败：%s: %s", type(exc).__name__, exc)
            return False
        return int(result.returncode) == 0

    def _send_linux(self, message: str) -> bool:
        """Linux：``notify-send``。

        这里把正文作为 argv 的独立元素传入，且**不经过 shell**，
        因此正文里的 ``;`` / ``$()`` 只是普通字符，不会被解释。
        """
        executable = shutil.which("notify-send")
        if not executable:
            logger.warning("未找到 notify-send，无法弹出桌面通知。")
            return False
        cmd = [executable, self.title, message[:MAX_BODY_CHARS]]
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("Linux 桌面通知失败：%s: %s", type(exc).__name__, exc)
            return False
        return int(result.returncode) == 0


# ---------------------------------------------------------------------------
# 声音通道
# ---------------------------------------------------------------------------

#: 提示音频率（Hz）与时长（毫秒）—— 短而不刺耳。
DEFAULT_BEEP_FREQUENCY = 1000
DEFAULT_BEEP_DURATION_MS = 300

#: Linux 提示音候选文件（按播放器分开，避免格式不匹配）。
_LINUX_PAPLAY_CANDIDATES: Sequence[str] = (
    "/usr/share/sounds/freedesktop/stereo/message.oga",
    "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga",
    "/usr/share/sounds/freedesktop/stereo/complete.oga",
)
_LINUX_APLAY_CANDIDATES: Sequence[str] = (
    "/usr/share/sounds/alsa/Front_Center.wav",
    "/usr/share/sounds/alsa/Noise.wav",
    "/usr/share/sounds/alsa/Front_Right.wav",
)
#: macOS 自带提示音。
_MAC_SOUND_PATH = "/System/Library/Sounds/Basso.aiff"


def load_winsound() -> Optional[Any]:
    """尝试加载标准库 ``winsound``（Windows 专属）。

    :return: ``winsound`` 模块，或 ``None``（非 Windows / 不可用）。
    """
    try:
        import winsound  # type: ignore[import-not-found]
    except (ImportError, OSError):
        return None
    return winsound


class SoundChannel(BaseChannel):
    """声音通道 —— 系统提示音。

    回退顺序：

    1. Windows —— 标准库 ``winsound``（``Beep``），无需子进程；
    2. macOS —— ``afplay``，退化到 ``osascript -e beep``；
    3. Linux —— ``paplay`` / ``aplay``；
    4. 都没有 —— 记一条 warning，返回 ``False``。
    """

    name = "sound"

    #: 提示音是**通知型**通道 —— 重复告警会被合并窗口拦一下。
    is_notification = True

    def __init__(
        self,
        *,
        frequency: int = DEFAULT_BEEP_FREQUENCY,
        duration_ms: int = DEFAULT_BEEP_DURATION_MS,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """
        :param frequency: 蜂鸣频率（Hz）。
        :param duration_ms: 蜂鸣时长（毫秒）。
        :param timeout: 调用外部程序的超时（秒）。
        """
        self.frequency = int(frequency)
        self.duration_ms = int(duration_ms)
        self.timeout = float(timeout or DEFAULT_TIMEOUT)

    def send(self, alert: Dict[str, Any]) -> bool:
        """播放提示音。"""
        platform_name = detect_platform()
        if platform_name == "windows":
            return self._beep_windows()
        if platform_name == "macos":
            return self._play_macos()
        if platform_name == "linux":
            return self._play_linux()

        logger.warning("当前平台没有可用的提示音方式：%s", platform_name)
        return False

    # -- 平台实现 -------------------------------------------------------
    def _beep_windows(self) -> bool:
        """Windows：用标准库 ``winsound.Beep`` 蜂鸣。"""
        winsound = load_winsound()
        if winsound is None:
            logger.warning("winsound 不可用，无法播放提示音。")
            return False
        try:
            winsound.Beep(self.frequency, self.duration_ms)
            return True
        except Exception as exc:  # noqa: BLE001 —— 通道绝不外泄异常
            logger.warning("提示音播放失败：%s: %s", type(exc).__name__, exc)
            return False

    def _play_macos(self) -> bool:
        """macOS：``afplay`` 播放系统提示音。"""
        executable = shutil.which("afplay")
        if (
            executable
            and os.path.exists(_MAC_SOUND_PATH)
            and self._run([executable, _MAC_SOUND_PATH])
        ):
            return True
        executable = shutil.which("osascript")
        if not executable:
            logger.warning("未找到 afplay / osascript，无法播放提示音。")
            return False
        return self._run([executable, "-e", "beep"])

    def _play_linux(self) -> bool:
        """Linux：``paplay`` / ``aplay`` 播放系统提示音。"""
        for executable_name, candidates in (
            ("paplay", _LINUX_PAPLAY_CANDIDATES),
            ("aplay", _LINUX_APLAY_CANDIDATES),
        ):
            executable = shutil.which(executable_name)
            if not executable:
                continue
            for candidate in candidates:
                if not os.path.exists(candidate):
                    continue
                if self._run([executable, candidate]):
                    return True
        logger.warning("未找到可用的提示音播放器（paplay / aplay）。")
        return False

    def _run(self, cmd: Sequence[str]) -> bool:
        """执行一个播放命令；失败只记日志，不外泄异常。"""
        try:
            result = subprocess.run(
                list(cmd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            logger.warning("提示音播放失败：%s: %s", type(exc).__name__, exc)
            return False
        return int(result.returncode) == 0


# ---------------------------------------------------------------------------
# Webhook 通道
# ---------------------------------------------------------------------------

#: 支持的 Webhook 报文格式。
WEBHOOK_FORMATS: Sequence[str] = (
    "dingtalk",
    "feishu",
    "serverchan",
    "bark",
    "generic",
)

#: 允许的 URL 协议 —— 只允许 http/https，其余（file://、ftp:// …）
#: 一律拒绝，避免把告警内容写到本地文件或奇怪的地方去。
ALLOWED_URL_SCHEMES: Sequence[str] = ("http", "https")

#: 触发标记 —— 守护者红线拦截（目标进程已被挂起）。
TRIGGER_GUARDIAN_INTERCEPT = "guardian_intercept"

#: 触发标记 —— 连续意图漂移。
TRIGGER_INTENT_DRIFT = "intent_drift"

#: 触发标记 —— 上下文异常（死循环 / 敏感数据外泄）。
TRIGGER_CONTEXT_ANOMALY = "context_anomaly"

#: 触发标记 —— 本地推理引擎掉线（v3.3.0）。
TRIGGER_ENGINE_OFFLINE = "engine_offline"

#: 触发标记 —— 时间盒撞破，严重跑偏（v3.3.0）。
TRIGGER_TIMEBOX = "timebox_exceeded"

#: 触发标记 —— L3 回退预算熔断，放弃云端调用并回退 L1（v3.8.0）。
TRIGGER_CASCADE_BUDGET = "cascade_budget_exhausted"

#: 触发标记 —— 微观统计异常（v4.0.0）。
#:
#: ``STATS_LOOP`` / ``STATS_NOOP`` / ``STATS_STALL`` ——
#: 「同一个文件改了十遍」「编辑内容根本没变」「两次动作隔了一小时」。
#: 这三类**不是**红线（不动手），只是「它在原地打转」的硬证据。
TRIGGER_MICRO_STATS = "micro_stats"

#: 「远程升级告警」默认关注的触发类型。
#:
#: 手机推送**不能**和桌面弹窗一样什么都要推 —— 半夜一条 STUCK 就能把人吵醒，
#: 那等于没开。所以默认只推真正要紧的两类：
#: 守护者**真的动手拦了**，以及**连续**意图漂移。
DEFAULT_REMOTE_TRIGGERS: Sequence[str] = (
    TRIGGER_GUARDIAN_INTERCEPT,
    TRIGGER_INTENT_DRIFT,
)

#: 防骚扰窗口（秒）—— 同一条事件在这个窗口内不重复推到手机。
DEFAULT_DEDUP_SECONDS = 60.0

#: 默认最大重试次数（**不含**首次尝试）。
DEFAULT_MAX_RETRIES = 2

#: 默认重试退避基数（秒）—— 指数退避：1s、2s、4s…
DEFAULT_RETRY_BACKOFF = 1.0

#: 可以重试的 HTTP 状态码 —— 服务端的**临时**问题，重试有意义。
#:
#: 4xx 里只有 408（超时）/ 425（Too Early）/ 429（限流）值得重试；
#: 其余 4xx 是请求本身有问题（URL 写错、签名错），重试多少次都一样。
RETRYABLE_STATUS: frozenset = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Webhook 投递队列的容量 —— 满了就**丢弃新请求**并记一条警告。
#:
#: 宁可少推一条，也不能让手机告警的堆积把内存吃光。
DEFAULT_WEBHOOK_QUEUE_SIZE = 32


def format_remote_card(alert: Dict[str, Any]) -> str:
    """把告警渲染成适合推送到手机的 Markdown 卡片正文。

    必须包含五要素（缺一不可）：

    1. **Agent 名称** —— 是谁在闯祸；
    2. **触发规则** —— 哪条红线 / 哪个漂移；
    3. **任务目标** —— 它本来该干什么；
    4. **当前操作** —— 它实际在干什么（命令原文）；
    5. **人话解释** —— L2 解释（没有就退回规则层的 reason）。

    :param alert: 告警字典。
    :return: Markdown 文本（不含外层卡片壳）。
    """
    data = alert if isinstance(alert, dict) else {}
    agent = str(data.get("agent") or "未知 Agent")
    rule_id = str(data.get("rule_id") or "未标注规则")
    severity = str(data.get("severity") or data.get("level") or "warn")
    goal = str(data.get("task_goal") or "").strip() or "（未设定）"
    command = str(data.get("command") or "").strip() or "（无命令）"
    reason = str(data.get("reason") or "").strip()
    explanation = str(data.get("explanation") or "").strip()
    triggers = data.get("triggers") or []
    if isinstance(triggers, (list, tuple, set)):
        trigger_text = "、".join(str(item) for item in triggers) or "—"
    else:
        trigger_text = str(triggers)
    timestamp = data.get("timestamp")
    if isinstance(timestamp, (int, float)):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    else:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"## 🚨 哨兵告警 · {severity.upper()}",
        "",
        f"- **Agent**：{agent}",
        f"- **触发规则**：`{rule_id}`",
        f"- **触发类型**：{trigger_text}",
        f"- **任务目标**：{goal}",
        f"- **当前操作**：`{command}`",
        f"- **时间**：{stamp}",
    ]
    if reason:
        lines += ["", f"**规则原因**：{reason}"]
    if explanation:
        lines += ["", f"**人话解释**：{explanation}"]
    guardian = data.get("guardian")
    if isinstance(guardian, dict) and guardian.get("ok"):
        lines += [
            "",
            f"**守护者**：已{'挂起' if guardian.get('method') == 'suspend' else '干预'}"
            f"目标进程（PID {guardian.get('pid')}）—— 等待你在看板上放行",
        ]
    return "\n".join(lines)


def _dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    """钉钉加签 —— 返回 ``sign`` 查询参数值。

    钉钉要求：``sign = base64(hmac_sha256(key=secret, msg="timestamp\\nsecret"))``
    之后再做一次 URL 编码。

    :param secret: 加签密钥。
    :param timestamp_ms: 毫秒时间戳。
    :return: 已 URL 编码的 sign 值。
    """
    sign_str = f"{timestamp_ms}\n{secret}"
    digest = hmac.new(
        secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
    ).digest()
    return quote_plus(base64.b64encode(digest).decode("utf-8"))


def _feishu_sign(secret: str, timestamp_s: int) -> str:
    """飞书加签 —— 返回 ``sign`` 值。

    飞书要求：``sign = base64(hmac_sha256(key="timestamp\\nsecret", msg=b""))``。

    :param secret: 加签密钥。
    :param timestamp_s: 秒级时间戳。
    :return: base64 后的 sign 值。
    """
    key = f"{timestamp_s}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


class WebhookChannel(BaseChannel):
    """Webhook 通道 —— 钉钉 / 飞书 / Server酱 / Bark / 通用 HTTP。

    报文格式：

    * ``dingtalk`` —— ``{"msgtype": "text", "text": {"content": ...}}``
      （``card=True`` 时改为 Markdown 卡片）；
    * ``feishu`` —— ``{"msg_type": "text", "content": {"text": ...}}``
      （``card=True`` 时改为 interactive 卡片）；
    * ``serverchan`` —— ``{"title": ..., "desp": ...}``（Server酱）；
    * ``bark`` —— ``{"title": ..., "body": ..., "level": ..., "sound": ...}``；
    * ``generic`` —— ``{"text": ..., "alert": {...完整告警...}}``。

    **防骚扰**：``dedup_seconds``（默认 60 秒）内**同一条事件**
    （Agent + 规则 + 命令 相同）只推一次。
    半夜被一条重复的 STUCK 吵醒，比漏报一次更让人讨厌。

    **后台投递 + 指数退避**（v1.1.0）
    ================================
    投递**不在调用方线程里**做 —— 原因很直接：
    webhook 是**网络**调用，可能要等 10 秒超时，再乘上 3 次重试就是 30 秒。
    主事件流绝不能为了推一条手机通知而卡住 30 秒（红线 R5）。

    所以 :meth:`send` 只做**校验 + 入队**（微秒级），
    真正的 HTTP 投递由后台线程完成，失败按**指数退避**重试：

    ``1 次 → 等 1s → 2 次 → 等 2s → 3 次 → 放弃``

    只有**临时性**故障才重试（网络错误、超时、5xx、429）；
    4xx（URL 错了、签名错了）重试多少次都一样，立刻放弃。
    """

    name = "webhook"

    #: Webhook 是**通知型**通道 —— 除了合并窗口，
    #: 它自己还带一层指纹去重（``dedup_seconds``），双保险。
    is_notification = True

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        format: str = "generic",
        triggers: Optional[Sequence[str]] = None,
        dedup_seconds: float = DEFAULT_DEDUP_SECONDS,
        secret: Optional[str] = None,
        card: bool = False,
        sound: str = "",
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        queue_size: int = DEFAULT_WEBHOOK_QUEUE_SIZE,
    ) -> None:
        """
        :param url: 接收地址。
        :param timeout: 请求超时（秒）。
        :param format: 报文格式，见 :data:`WEBHOOK_FORMATS`。
        :param triggers: 只推送带这些触发标记的告警；
            为 ``None`` **或空列表**时推送全部（保持 v0.x 的旧行为）。
        :param dedup_seconds: 防骚扰窗口（秒）；``0`` 表示不限制。
        :param secret: 加签密钥（钉钉 / 飞书用）。
        :param card: 是否发送 Markdown 卡片而不是纯文本。
        :param sound: Bark 提示音名（留空用服务端默认）。
        :param max_retries: 失败后的最大重试次数（不含首次尝试）。
        :param retry_backoff: 重试退避基数（秒）；实际等待 = 基数 × 2ⁿ。
        :param queue_size: 投递队列容量；满了就丢弃新请求。
        """
        self.url = str(url or "").strip()
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.format = str(format or "generic").strip().lower()
        if self.format not in WEBHOOK_FORMATS:
            logger.warning(
                "未知的 webhook 报文格式「%s」，已按 generic 处理。", self.format
            )
            self.format = "generic"
        # 空列表 = **不筛选**（推全部），而不是「一个都不推」。
        # 用户在设置页把触发类型清空是「我想全都要」的意思，
        # 如果这里当成「不推任何类型」，通知会**静默消失**，用户还以为哨兵没工作。
        cleaned = (
            tuple(str(item).strip() for item in triggers if str(item).strip())
            if triggers is not None
            else None
        )
        self.triggers: Optional[Tuple[str, ...]] = cleaned or None
        self.dedup_seconds = float(dedup_seconds or 0.0)
        self.secret = str(secret or "").strip()
        self.card = bool(card)
        self.sound = str(sound or "").strip()
        #: 失败后的最大重试次数（不含首次尝试）。
        self.max_retries = max(int(max_retries or 0), 0)
        #: 重试退避基数（秒）。
        self.retry_backoff = max(float(retry_backoff or 0.0), 0.0)
        #: 事件指纹 → 上次推送时刻（用于防骚扰）。
        self._last_sent: Dict[str, float] = {}
        self._lock = threading.Lock()
        #: 待投递队列（有界）。
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue(
            maxsize=max(int(queue_size or 1), 1)
        )
        #: 后台投递线程。
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: 队列里还没处理完的条数（测试与统计用）。
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        #: 最近一次投递结果（``True`` = 成功）。
        self.last_delivered = False
        #: 累计**成功**投递次数（v3.2.0）。
        #:
        #: ⚠️ 不能只看 ``last_delivered`` 来判断「刚才那条晨报送到了没」——
        #: 那可能是一条**更早**的告警留下的。
        #: 调用方要判断「这一条」的结果，就比对前后计数。
        self.delivered_count = 0
        #: 最近一次投递失败的说明（给日志与测试看）。
        self.last_error = ""
        #: 累计投递失败次数（含重试耗尽的）。
        self.failed_sends = 0
        #: 最近一次 HTTP 响应的细节（v2.1.0）。
        #:
        #: ``{"status": int, "body": str}`` ——
        #: 钉钉 / 飞书会在响应体里回 ``errcode``，
        #: 没有它就没法告诉用户「到底哪儿错了」。
        self.last_response: Dict[str, Any] = {"status": 0, "body": ""}

    # -- 报文 -----------------------------------------------------------
    def build_payload(self, alert: Dict[str, Any]) -> Dict[str, Any]:
        """按当前格式构造请求体。

        :param alert: 告警字典。
        :return: 可直接 JSON 序列化的字典。
        """
        message = format_alert(alert)
        card_text = format_remote_card(alert)
        title = self._build_title(alert)

        if self.format == "dingtalk":
            if self.card:
                return {
                    "msgtype": "markdown",
                    "markdown": {"title": title, "text": card_text},
                }
            return {"msgtype": "text", "text": {"content": message}}
        if self.format == "feishu":
            if self.card:
                return {
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {
                                "tag": "plain_text",
                                "content": title,
                            },
                            "template": "red" if self._is_critical(alert) else "orange",
                        },
                        "elements": [
                            {
                                "tag": "div",
                                "text": {"tag": "lark_md", "content": card_text},
                            }
                        ],
                    },
                }
            return {"msg_type": "text", "content": {"text": message}}
        if self.format == "serverchan":
            return {"title": title, "desp": card_text}
        if self.format == "bark":
            payload: Dict[str, Any] = {
                "title": title,
                "body": card_text,
                # critical 用「时间敏感」，iOS 会绕过静音 —— 这才是叫醒服务。
                "level": "timeSensitive" if self._is_critical(alert) else "active",
            }
            if self.sound:
                payload["sound"] = self.sound
            return payload
        payload = alert if isinstance(alert, dict) else {}
        return {
            "text": message,
            "card": card_text,
            "title": title,
            "agent": str(payload.get("agent") or ""),
            "rule_id": str(payload.get("rule_id") or ""),
            "task_goal": str(payload.get("task_goal") or ""),
            "command": str(payload.get("command") or ""),
            "triggers": list(payload.get("triggers") or []),
            "alert": dict(payload),
        }

    @staticmethod
    def _is_critical(alert: Dict[str, Any]) -> bool:
        """判断是不是最严重的那一档（决定要不要「叫醒」）。"""
        data = alert if isinstance(alert, dict) else {}
        return str(data.get("severity") or data.get("level") or "") == "red_line" or (
            str(data.get("verdict") or "").upper() == "DANGEROUS"
        )

    @staticmethod
    def _build_title(alert: Dict[str, Any]) -> str:
        """卡片标题 —— 手机通知栏上就显示这一行。"""
        data = alert if isinstance(alert, dict) else {}
        agent = str(data.get("agent") or "Agent")
        rule_id = str(data.get("rule_id") or "告警")
        return f"🚨 哨兵拦截 · {agent} · {rule_id}"

    # -- 防骚扰 ---------------------------------------------------------
    @staticmethod
    def fingerprint(alert: Dict[str, Any]) -> str:
        """算出这条事件的指纹。

        指纹相同就认为是「同一件事」—— 同一条命令被反复重试时，
        不该往手机上推 N 条。

        :param alert: 告警字典。
        :return: 指纹字符串。
        """
        data = alert if isinstance(alert, dict) else {}
        parts = [
            str(data.get("agent") or ""),
            str(data.get("rule_id") or ""),
            str(data.get("command") or ""),
        ]
        # sha1 只用来生成**去重键**，不涉及任何安全用途。
        return hashlib.sha1(  # noqa: S324
            "|".join(parts).encode("utf-8")
        ).hexdigest()

    def should_send(self, alert: Dict[str, Any]) -> bool:
        """判断这条告警**该不该**推到手机。

        两道闸门：

        1. **触发类型** —— 配了 ``triggers`` 就只推这些类型；
        2. **防骚扰窗口** —— 同指纹事件在窗口内只推一次。

        :param alert: 告警字典。
        :return: 该推送返回 ``True``。
        """
        data = alert if isinstance(alert, dict) else {}
        if self.triggers is not None:
            fired = data.get("triggers") or []
            if isinstance(fired, str):
                fired = [fired]
            fired_set = {str(item) for item in fired}
            if not fired_set & set(self.triggers):
                return False
        if self.dedup_seconds <= 0:
            return True
        key = self.fingerprint(data)
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(key)
            if last is not None and (now - last) < self.dedup_seconds:
                return False
            self._last_sent[key] = now
        return True

    def _release(self, alert: Dict[str, Any]) -> None:
        """推送失败时**撤销**防骚扰占位 —— 没送到手机就该允许重试。"""
        if self.dedup_seconds <= 0:
            return
        key = self.fingerprint(alert)
        with self._lock:
            self._last_sent.pop(key, None)

    def is_url_allowed(self) -> bool:
        """检查 URL 是否可投递（非空且协议为 http/https）。"""
        if not self.url:
            return False
        try:
            scheme = urlparse(self.url).scheme.lower()
        except ValueError:
            return False
        return scheme in ALLOWED_URL_SCHEMES

    # -- 对外接口 -------------------------------------------------------
    def build_briefing_payload(
        self, summary: Dict[str, Any]
    ) -> Dict[str, Any]:
        """按当前格式构造**晨报摘要**的请求体。

        摘要和告警是**两种东西**：
        告警是「刚发生了一件坏事」，摘要是「这一夜的总结」。
        所以标题、语气、字段都不一样 ——
        但复用的是同一套**格式适配**（钉钉 / 飞书 / Server酱 / Bark / 通用）。

        :param summary: 晨报摘要（见
            :func:`src.core.briefing.build_briefing_summary`）。
        :return: 可直接 JSON 序列化的字典。
        """
        data = summary if isinstance(summary, dict) else {}
        title = str(data.get("title") or "🌅 哨兵晨报")
        markdown = str(data.get("markdown") or "")
        text = str(data.get("text") or markdown)
        stats = data.get("stats")
        stats = dict(stats) if isinstance(stats, dict) else {}

        if self.format == "dingtalk":
            if self.card:
                return {
                    "msgtype": "markdown",
                    "markdown": {"title": title, "text": markdown},
                }
            return {"msgtype": "text", "text": {"content": text}}
        if self.format == "feishu":
            if self.card:
                return {
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {"tag": "plain_text", "content": title},
                            "template": "green",
                        },
                        "elements": [
                            {
                                "tag": "div",
                                "text": {"tag": "lark_md", "content": markdown},
                            }
                        ],
                    },
                }
            return {"msg_type": "text", "content": {"text": text}}
        if self.format == "serverchan":
            return {"title": title, "desp": markdown}
        if self.format == "bark":
            return {
                "title": title,
                "body": markdown,
                "level": "active",
            }
        return {
            "type": "briefing",
            "title": title,
            "text": text,
            "card": markdown,
            "stats": stats,
        }

    def send_briefing(self, summary: Dict[str, Any]) -> bool:
        """把晨报摘要**排队**投递给 Webhook。

        ⚠️ 与 :meth:`send` 的两点不同：

        1. **不过防骚扰、不过触发类型过滤** ——
           晨报是**一次性**总结，但「触发类型」过滤**绝对不能**套上来：
           用户配了 ``triggers: [DANGEROUS]`` 的话，
           晨报会被当成「不是 DANGEROUS 告警」而**永远推不出去**；
        2. **报文是摘要**（:meth:`build_briefing_payload`），
           不是告警 —— 字段完全不一样。

        本方法同样**不做网络 I/O**，只做校验与入队（微秒级）。

        :param summary: 晨报摘要。
        :return: 成功**入队**返回 ``True``；被拒绝返回 ``False``。
        """
        if not self.url:
            logger.warning("webhook 地址为空，已跳过晨报投递。")
            return False
        if not self.is_url_allowed():
            logger.warning(
                "webhook 地址协议不被允许（仅支持 http/https）：%s", self.url
            )
            return False
        if requests is None:
            logger.warning("未安装 requests，无法使用 webhook 通道。")
            return False

        job = {
            "alert": {"rule_id": "briefing", "agent": "sentinel"},
            "payload": self.build_briefing_payload(summary),
            "url": self._signed_url(),
        }
        self._ensure_worker()
        with self._lock:
            self._pending += 1
            self._idle.clear()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending = max(self._pending - 1, 0)
                if self._pending == 0:
                    self._idle.set()
            logger.warning(
                "webhook 投递队列已满，丢弃本次晨报推送（地址：%s）。",
                self.url,
            )
            return False
        return True

    def send(self, alert: Dict[str, Any]) -> bool:
        """把一条告警**排队**投递给 Webhook。

        ⚠️ 本方法**不做网络 I/O** —— 它只做校验与入队（微秒级）。
        真正的 HTTP 投递在后台线程里完成，所以调用方（引擎主循环）
        **绝不会被一个 10 秒的网络超时卡住**。

        :param alert: 告警字典。
        :return: 成功**入队**返回 ``True``；
            被拒绝（地址非法 / 触发类型不匹配 / 防骚扰命中 / 队列满）返回 ``False``。
        """
        if not self.url:
            logger.warning("webhook 地址为空，已跳过投递。")
            return False
        if not self.is_url_allowed():
            logger.warning(
                "webhook 地址协议不被允许（仅支持 http/https）：%s", self.url
            )
            return False

        if requests is None:
            logger.warning("未安装 requests，无法使用 webhook 通道。")
            return False

        if not self.should_send(alert):
            logger.info(
                "webhook 防骚扰：%s 秒内同类事件已推送，本次跳过。",
                int(self.dedup_seconds),
            )
            return False

        job = {
            "alert": alert,
            "payload": self.build_payload(alert),
            "url": self._signed_url(),
        }
        self._ensure_worker()
        with self._lock:
            self._pending += 1
            self._idle.clear()
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending = max(self._pending - 1, 0)
                if self._pending == 0:
                    self._idle.set()
            # 队列满了 —— 丢一条，并撤销防骚扰占位（没送到就该允许重试）。
            logger.warning(
                "webhook 投递队列已满，丢弃本次推送（地址：%s）。"
                "请检查网络或调大队列。",
                self.url,
            )
            self._release(alert)
            return False
        return True

    # -- 后台投递 -------------------------------------------------------
    def _ensure_worker(self) -> None:
        """确保后台投递线程已经起来。"""
        if self._worker is not None and self._worker.is_alive():
            return
        # ⚠️ 重新启用（v3.2.0 修的真缺陷）：
        # ``close()`` 会置上停止信号。关掉之后如果又要发
        # （晨报就是收尾时才入队的），新线程**一上来就退出** ——
        # 报文静静地躺在队列里没人送，而调用方以为已经发出去了。
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="sentinel-webhook", daemon=True
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        """后台线程主体：取任务 → 投递（带重试）→ 记账。"""
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._deliver(job)
            except Exception as exc:  # noqa: BLE001 —— 投递线程绝不外泄
                logger.warning(
                    "webhook 后台投递异常：%s: %s", type(exc).__name__, exc
                )
            finally:
                self._queue.task_done()
                with self._lock:
                    self._pending = max(self._pending - 1, 0)
                    if self._pending == 0:
                        self._idle.set()

    def _deliver(self, job: Dict[str, Any]) -> bool:
        """投递一条任务，失败按**指数退避**重试。

        :param job: 投递任务（``alert`` / ``payload`` / ``url``）。
        :return: 投递成功返回 ``True``。
        """
        alert = job.get("alert") or {}
        payload = job.get("payload")
        url = str(job.get("url") or self.url)
        attempts = self.max_retries + 1
        last_reason = ""
        used = 0

        for attempt in range(attempts):
            if self._stop.is_set():
                break
            used = attempt + 1
            ok, reason = self._attempt(url, payload)
            if ok:
                with self._lock:
                    self.last_delivered = True
                    self.last_error = ""
                    self.delivered_count += 1
                return True
            last_reason = reason
            retryable = self._is_retryable(reason)
            if not retryable or attempt >= attempts - 1:
                break
            delay = self.retry_backoff * (2 ** attempt)
            logger.warning(
                "webhook 投递失败（%s），%.1f 秒后重试（第 %d/%d 次）：%s",
                url,
                delay,
                attempt + 1,
                attempts - 1,
                reason,
            )
            if self._wait_backoff(delay):
                break

        # 彻底失败 —— 给一句**看得懂**的中文提示，并撤销防骚扰占位。
        with self._lock:
            self.last_delivered = False
            self.last_error = last_reason
            self.failed_sends += 1
        logger.warning(
            "webhook 投递最终失败（共尝试 %d 次）：%s。"
            "请检查网络连通性、URL 是否正确、是否需要加签密钥。"
            "本次告警**未能**推送到手机。",
            used,
            last_reason or "未知错误",
        )
        self._release(alert)
        return False

    def _wait_backoff(self, seconds: float) -> bool:
        """等一会儿再重试。

        单独抽出来是为了**可测** —— 测试要断言退避是指数增长的，
        不能真的干等好几秒。

        :param seconds: 等待秒数。
        :return: 收到停止信号返回 ``True``（应当放弃重试）。
        """
        return self._stop.wait(max(float(seconds), 0.0))

    def _attempt(self, url: str, payload: Any) -> Tuple[bool, str]:
        """尝试投递一次。

        :param url: 目标地址。
        :param payload: 请求体。
        :return: ``(成功?, 失败原因)``。
        """
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001 —— 通道绝不外泄异常
            return False, f"{type(exc).__name__}: {exc}"

        status = int(getattr(response, "status_code", 0) or 0)
        # v2.1.0：把响应体记下来 —— 钉钉 / 飞书的错误码在里面。
        try:
            body = str(getattr(response, "text", "") or "")
        except Exception:  # noqa: BLE001 —— 读响应体失败不影响投递判定
            body = ""
        with self._lock:
            self.last_response = {"status": status, "body": body[:2000]}
        if 200 <= status < 300:
            return True, ""
        return False, f"HTTP {status}"

    def _is_retryable(self, reason: str) -> bool:
        """判断这个失败**值不值得重试**。

        网络错误与超时 → 值得重试；
        HTTP 5xx / 429 → 值得重试（服务端临时问题）；
        其余 4xx → **不值得**（URL 错了、签名错了，重试也没用）。

        :param reason: 失败原因文本。
        :return: 该重试返回 ``True``。
        """
        text = str(reason or "")
        if text.startswith("HTTP "):
            try:
                status = int(text[5:].split(maxsplit=1)[0])
            except (ValueError, IndexError):
                return False
            return status in RETRYABLE_STATUS
        # 网络错误 / 超时 —— 典型的临时故障。
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """等队列里**已入队**的任务全部投递完。

        测试与命令行脚本用它来确定性地等待投递结果。

        :param timeout: 最长等待秒数。
        :return: 全部完成返回 ``True``；超时返回 ``False``。
        """
        return self._idle.wait(timeout=max(float(timeout), 0.0))

    def close(self) -> None:
        """停掉后台投递线程（**先把队列跑完**）。

        ⚠️ **必须先 flush 再停**（v3.2.0 修的真缺陷）
        =============================================
        原来的写法是直接 ``self._stop.set()`` ——
        而投递线程的循环条件是 ``while not self._stop.is_set()``，
        于是**队列里还没投出去的报文会被直接丢掉**。

        这个坑在「晨报推送」上暴露得特别明显：
        晨报是**收尾时**才入队的（此刻用户正要关进程），
        入队完很快就 ``close()`` —— 报文还在队列里，线程就停了，
        手机上**什么也没收到**，而哨兵还印了一句「已推送到你的手机」。

        所以现在：**先把队列跑完（最多等 ``timeout`` 秒），再停线程**。
        """
        self.flush(timeout=10.0)
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5.0)
        self._worker = None

    def _signed_url(self) -> str:
        """按平台规则给 URL 加签。

        钉钉 / 飞书开了「加签」就必须在 URL 上带 ``timestamp`` 与 ``sign``，
        否则服务端直接拒收。

        :return: 实际要请求的 URL。
        """
        if not self.secret:
            return self.url
        url = self.url
        if self.format == "dingtalk":
            timestamp_ms = int(time.time() * 1000)
            sign = _dingtalk_sign(self.secret, timestamp_ms)
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}timestamp={timestamp_ms}&sign={sign}"
        if self.format == "feishu":
            timestamp_s = int(time.time())
            sign = _feishu_sign(self.secret, timestamp_s)
            joiner = "&" if "?" in url else "?"
            return f"{url}{joiner}timestamp={timestamp_s}&sign={sign}"
        return url
