"""v3.3.0 —— 「彻底铲除开机自启与后台静默驻留」的验收测试。

这一组是**验收断言**，不是功能测试
================================

用户的要求原话是：「**彻底拒绝任何形式的开机自启动和后台静默驻留**」，
工具必须「**用完即走，想开才开**」。

「彻底」意味着**光改文档不算数** —— 必须能在**代码层**证明：

1. 自启相关的**文件**一个都不在仓库里了；
2. 自启相关的**代码**一个都不在了（``--daemon``、自检线程、VBS 启动器）；
3. **本机残留**能被动出来、并有一条**只删不建**的清理路径；
4. 入口启动时会**主动提醒**用户去清理旧残留。

所以这里的断言方向和 v3.1.0 那组**正好相反**：
v3.1.0 断言「自启脚本存在而且能用」，
v3.3.0 断言「**这些东西一个都不许再存在**」。

⚠️ 全部**离线** —— 不碰注册表、不碰「启动」文件夹、不改任何系统状态。
"""

from __future__ import annotations

import importlib.util
import io
import os
from typing import List

from src.core.autostart_residue import (
    RESIDUE_LINK_NAME,
    detect_residue,
    render_notice,
)

#: 项目根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 打包脚本目录。
SCRIPTS_DIR = os.path.join(ROOT, "_构建与发布", "scripts")


def _read(*parts: str) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对项目根的路径片段。
    :return: 文件文本。
    """
    with io.open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _read_bytes(*parts: str) -> bytes:
    """读仓库里的一个二进制文件。

    :param parts: 相对项目根的路径片段。
    :return: 文件原始字节。
    """
    with io.open(os.path.join(ROOT, *parts), "rb") as handle:
        return handle.read()


def _load_entry():
    """加载打包入口脚本（它不在 ``src`` 包里，是独立脚本）。

    :return: 模块对象。
    """
    path = os.path.join(SCRIPTS_DIR, "sentinel_entry.py")
    spec = importlib.util.spec_from_file_location(
        "sentinel_entry_v330_noautostart", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. 自启相关的文件必须一个都不在
# ---------------------------------------------------------------------------
class Test自启文件已被删除:
    """「彻底」的第一层：文件层面的证据。"""

    def test_根目录不再有install_startup_bat(self):
        """v3.1.0 那个双击即装的 ``.bat`` 必须消失。"""
        assert not os.path.exists(os.path.join(ROOT, "install_startup.bat"))

    def test_脚本目录不再有install_startup_ps1(self):
        """底层那个 PowerShell 脚本必须消失。"""
        assert not os.path.exists(
            os.path.join(SCRIPTS_DIR, "install_startup.ps1")
        )

    def test_仓库里没有任何哨兵自己的隐藏启动器(self):
        """隐藏启动器（``.vbs``）是「静默」的载体 —— 一个都不许留。"""
        found: List[str] = []
        for base, _dirs, files in os.walk(ROOT):
            found.extend(
                os.path.join(base, name)
                for name in files
                if name.lower().endswith(".vbs")
            )
        mine = [
            path
            for path in found
            if os.path.basename(path).lower() == "sentinel_startup.vbs"
        ]
        assert mine == [], mine

    def test_源码目录里没有任何vbs(self):
        """哨兵自己的代码与打包产物里不许再出现 ``.vbs``。"""
        roots = [
            os.path.join(ROOT, "src"),
            os.path.join(ROOT, "_构建与发布"),
        ]
        found: List[str] = []
        for base in roots:
            for dirpath, _dirs, files in os.walk(base):
                found.extend(
                    os.path.join(dirpath, name)
                    for name in files
                    if name.lower().endswith(".vbs")
                )
        assert found == [], found

    def test_根目录只剩启动哨兵这一个脚本(self):
        """根目录的白名单：只有 ``启动哨兵.bat``。"""
        scripts = [
            name
            for name in os.listdir(ROOT)
            if name.lower().endswith(".bat")
            or name.lower().endswith(".cmd")
        ]
        assert scripts == ["启动哨兵.bat"], scripts

    def test_清理脚本在(self):
        """「删残留」的那条路必须留着 —— 用户需要它。"""
        assert os.path.isfile(
            os.path.join(SCRIPTS_DIR, "remove_startup_residue.ps1")
        )


# ---------------------------------------------------------------------------
# 2. 自启相关的代码必须一个都不在
# ---------------------------------------------------------------------------
class Test自启代码已被删除:
    """「彻底」的第二层：代码层面的证据。"""


    def test_守护模式的实现已经不在了(self):
        """自检线程、HTTP 探测、重启循环 —— 全部移除。"""
        from src.core import session_server as server_module

        for gone in (
            "_serve_with_daemon",
            "_start_daemon_watchdog",
            "_probe_http",
            "_DAEMON_STATE",
            "DAEMON_CRASH_LOG",
        ):
            assert not hasattr(server_module, gone), gone


    def test_入口脚本不认识守护模式(self):
        """``_is_daemon()`` 已删除 —— 入口不再有「后台」这个概念。"""
        entry = _load_entry()
        assert not hasattr(entry, "_is_daemon")



    def test_入口帮助里写明了无自启(self):
        """帮助文本必须**说真话** —— 用户看的就是它。"""
        text = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert "无自启" in text
        assert "用完即走" in text


# ---------------------------------------------------------------------------
# 3. 残留检测：只读，且能主动报出来
# ---------------------------------------------------------------------------
class Test残留检测:
    """已装过的机器上还有残留 —— 哨兵必须**能发现**。"""

    def test_检测器不写任何东西(self):
        """⚠️ 只检测不删除 —— 删用户系统里的文件是**用户的决定**。"""
        text = _read("src", "core", "autostart_residue.py")
        for bad in (
            "os.remove",
            "os.unlink",
            "shutil.rmtree",
            "winreg.SetValue",
            "winreg.CreateKey",
            "Remove-Item",
        ):
            assert bad not in text, bad

    def test_临时目录里没有隐藏启动器(self):
        """干净目录里不该报出隐藏启动器。"""
        residue = detect_residue(os.path.dirname(os.path.abspath(__file__)))
        vbs_hits = [
            item for item in residue if item["kind"] == "隐藏启动器"
        ]
        assert vbs_hits == []

    def test_没残留时不打扰用户(self):
        """干净机器上**一个字都不该说** —— 别制造焦虑。"""
        assert render_notice([]) == ""

    def test_有残留时给出显眼提示(self):
        """发现残留必须**显眼**，并说清楚怎么清。"""
        notice = render_notice(
            [
                {
                    "kind": "快捷方式",
                    "path": r"C:\Users\某\AppData\Roaming\Startup\哨兵 Sentinel.lnk",
                    "how_to_clean": "删掉这个文件，或跑清理脚本。",
                }
            ]
        )
        assert notice != ""
        assert "开机自启残留" in notice
        assert RESIDUE_LINK_NAME in notice
        assert "remove_startup_residue.ps1" in notice

    def test_提示里写明哨兵不再自启(self):
        """提示必须说清楚「**现在**哨兵不自启了」。"""
        notice = render_notice(
            [{"kind": "快捷方式", "path": "x", "how_to_clean": "y"}]
        )
        assert "不再有" in notice
        assert "用完即走" in notice


# ---------------------------------------------------------------------------
# 4. 清理脚本：只许清理，不许新建
# ---------------------------------------------------------------------------
class Test清理脚本:
    """``remove_startup_residue.ps1`` 的行为约定。"""

    def test_ps1带UTF8BOM(self):
        """PowerShell 5.1 读没有 BOM 的 ``.ps1``，中文会乱码直接解析失败。"""
        raw = _read_bytes(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        assert raw.startswith(b"\xef\xbb\xbf")

    def test_ps1自推路径(self):
        """中文路径不能从命令行收 —— 要用脚本级 ``$PSScriptRoot``。"""
        text = _read(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        assert "$PSScriptRoot" in text

    def test_ps1会删掉三类残留(self):
        """快捷方式、隐藏启动器、注册表启动项 —— 都要能清。"""
        text = _read(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        assert 'GetFolderPath("Startup")' in text
        assert "sentinel_startup.vbs" in text
        assert "CurrentVersion\\Run" in text

    def test_ps1只删不建(self):
        """⚠️ **只许清理，不许新建** —— 新建就等于把自启又装回去。"""
        text = _read(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        for forbidden in (
            "WScript.Shell",
            "CreateShortcut",
            "New-ItemProperty",
            "Set-ItemProperty",
            "reg add",
            "reg.exe",
            "Install-Sentinel",
        ):
            assert forbidden not in text, forbidden

    def test_ps1有只读开关(self):
        """``-Status`` 只看不删 —— 用户得先能「看一眼」。"""
        text = _read(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        assert "[switch]$Status" in text

    def test_ps1说清楚哨兵现在怎么用(self):
        """收尾提示要写明「只在你手动启动时才运行」。"""
        text = _read(
            "_构建与发布", "scripts", "remove_startup_residue.ps1"
        )
        assert "手动启动" in text


# ---------------------------------------------------------------------------
# 5. 入口：启动时主动提醒清理残留
# ---------------------------------------------------------------------------
class Test入口残留提醒:
    """哨兵每次启动都要**主动看一眼**残留。"""

    def test_入口有残留提醒函数(self):
        """入口脚本里得有这个函数。"""
        entry = _load_entry()
        assert hasattr(entry, "_warn_autostart_residue")

    def test_入口启动时会调用它(self):
        """⚠️ 有了函数还不够 —— 得**真的被调用**。"""
        text = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert "_warn_autostart_residue()" in text
        # 至少在两处（正常启动 + 出错兜底）被调用。
        assert text.count("_warn_autostart_residue()") >= 2

    def test_入口出错也提醒(self):
        """「用完即走」的同时，得让人知道旧残留还在。"""
        text = _read("_构建与发布", "scripts", "sentinel_entry.py")
        assert "render_notice" in text


# ---------------------------------------------------------------------------
# 6. 打包脚本：不得再生成任何自启产物
# ---------------------------------------------------------------------------
class Test打包脚本不再造自启:
    """打包是「产物」的源头 —— 源头不许再造自启动器。"""

    def test_打包脚本不写vbs(self):
        """打包脚本里不许再生成 ``.vbs``。"""
        text = _read("_构建与发布", "scripts", "build_exe.py")
        assert "sentinel_startup" not in text
        assert ".vbs" not in text.lower()

    def test_打包脚本不碰启动文件夹(self):
        """打包过程**不许**写「启动」文件夹。"""
        text = _read("_构建与发布", "scripts", "build_exe.py")
        for bad in ("Startup", "shell:startup", "WScript.Shell"):
            assert bad not in text, bad


# ---------------------------------------------------------------------------
# 7. 时间盒的 Guardian 联动：跟着武装状态走
# ---------------------------------------------------------------------------
class Test时间盒挂起策略:
    """「配置了 Guardian 就自动挂起」的三态语义。"""

    def _guardian(self, enabled: bool, mode: str):
        """造一个假的守护者。

        :param enabled: 是否启用。
        :param mode: 干预模式。
        :return: 假守护者对象。
        """
        return type(
            "_Guardian",
            (),
            {"enabled": enabled, "mode": mode},
        )()

    def test_守护者武装着就会挂起(self):
        """用户授权了「该拦的给我拦」—— 死磕也该拦。"""
        from src.core.timebox import TimeBoxDetector

        detector = TimeBoxDetector(guardian_suspend=None)
        guardian = self._guardian(True, "guardian")
        assert detector.guardian_suspend_allowed(guardian) is True

    def test_守护者没武装就不挂(self):
        """没武装 = 只观察 —— 不能动别人的进程。"""
        from src.core.timebox import TimeBoxDetector

        detector = TimeBoxDetector(guardian_suspend=None)
        guardian = self._guardian(False, "guardian")
        assert detector.guardian_suspend_allowed(guardian) is False

    def test_干预模式不是guardian就不挂(self):
        """干预模式是「观察」时，不该挂起。"""
        from src.core.timebox import TimeBoxDetector

        detector = TimeBoxDetector(guardian_suspend=None)
        guardian = self._guardian(True, "observe")
        assert detector.guardian_suspend_allowed(guardian) is False

    def test_显式关掉就永远不挂(self):
        """显式 ``false`` 优先级最高 —— 用户说了算。"""
        from src.core.timebox import TimeBoxDetector

        detector = TimeBoxDetector(guardian_suspend=False)
        guardian = self._guardian(True, "guardian")
        assert detector.guardian_suspend_allowed(guardian) is False

    def test_显式打开但没有守护者就不挂(self):
        """没有守护者就没东西可挂。"""
        from src.core.timebox import TimeBoxDetector

        detector = TimeBoxDetector(guardian_suspend=True)
        assert detector.guardian_suspend_allowed(None) is False


# ---------------------------------------------------------------------------
# 8. 看门狗：只读监控，绝不自动拉起
# ---------------------------------------------------------------------------
class Test看门狗只读边界:
    """用户要求：「不要尝试自动拉起，保持只读监控的边界」。"""

    def test_看门狗模块不导入subprocess(self):
        """拉起进程需要 ``subprocess`` —— 不导入它就不可能偷偷拉。"""
        import src.core.engine_watchdog as watchdog_module

        assert not hasattr(watchdog_module, "subprocess")

    def test_配置里的重启段被忽略(self):
        """配置里还留着 ``llm.watchdog.restart``，但它**被忽略**。"""
        from src.core.engine_watchdog import EngineWatchdog

        watchdog = EngineWatchdog(
            "http://127.0.0.1:8888/v1",
            prober=lambda: {"ok": False, "detail": "连不上"},
            restart={"enabled": True, "command": ["whatever"]},
        )
        watchdog.check_once()
        assert watchdog.restart_attempts == 0

    def test_配置模板写明了不被自动拉起(self):
        """配置模板不能骗人 —— 得写清楚这段是「保留但忽略」。"""
        text = _read("config", "config.example.yaml")
        assert "watchdog" in text
        assert "不再自动拉起" in text or "被忽略" in text


# ---------------------------------------------------------------------------
# 9. 辅助：确保下面这些模块真的能被导入
# ---------------------------------------------------------------------------
def test_残留检测模块可以正常导入() -> None:
    """冒烟测试 —— 模块坏了上面全测不出来。"""
    assert callable(detect_residue)
    assert callable(render_notice)
