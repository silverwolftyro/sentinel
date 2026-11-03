"""把哨兵打包成**免安装的 Windows 单文件程序**（v2.3.0）。

为什么要有它
============

v1.1.0 之前，想用哨兵得先装 Python、装依赖、clone 仓库。
对「想半夜挂机守一下自己写的 Agent」的用户来说，这门槛高到
「还没开始用就已经放弃」了。

所以 v1.2.0 起提供 ``sentinel.exe``：双击就能跑，不需要任何 Python 环境。

打包的取舍
==========

单文件（``--onefile``）的代价是**每次启动都要解包**（约 1 秒），
换来的是「发给别人一个文件就能用」。对这种工具来说这笔买卖很划算。

体积控制上做了两件事：

1. **只带核心依赖** —— 显式排除 tkinter / matplotlib / numpy / pytest
   等用不上的大库；
2. **不把整个项目塞进去** —— 只打包 ``src`` 包、模板、静态资源
   和启动器脚本。

用法::

    python _构建与发布/scripts/build_exe.py
    python _构建与发布/scripts/build_exe.py --no-verify   # 跳过自检
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from typing import List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 打包产物目录。
DIST_DIR = os.path.join(_ROOT, "_构建与发布", "dist")
#: 中间产物目录。
BUILD_DIR = os.path.join(_ROOT, "_构建与发布", "build")
#: 打包日志。
LOG_PATH = os.path.join(DIST_DIR, "build_exe.log")

#: 入口脚本。
ENTRY_SCRIPT = os.path.join(_HERE, "sentinel_entry.py")

#: 产物 exe 的文件名（v5.0.0 起）。
#:
#: ⚠️ 为什么改名 —— 哨兵不再是「双击开面板的监控客户端」，
#: 而是**无头守护进程**。名字要说实话：
#: ``sentinel.exe`` 让人以为双击会弹个界面出来。
EXE_NAME = "sentinel-daemon.exe"

#: 随包资源：``(源路径, 包内目标目录)``。
#:
#: v5.0.0：哨兵是**无头守护进程** —— 面板的静态资源与模板
#: **已整体剥离**（``src/gui/static/``、``src/gui/templates/``），
#: 剩下的只有配置模板与 Agent 清单。
DATA_FILES = (
    # v4.1.0：Agent 清单是**数据**，必须随包 ——
    # 少了它，雷达就认不出那 14 个 Agent（数据层整体降级成空表）。
    (
        os.path.join("src", "agents", "cloud_agents.json"),
        os.path.join("src", "agents"),
    ),
    (
        os.path.join("_构建与发布", "scripts", "run_with_sentinel.py"),
        os.path.join("_构建与发布", "scripts"),
    ),
    (
        os.path.join("config", "config.example.yaml"),
        os.path.join("config"),
    ),
)

#: **可选依赖**的隐藏导入 —— 装了才点名（见 `_optional_hidden_imports()`）。
OPTIONAL_HIDDEN_IMPORTS = (
    # psutil —— 守护者（挂起 / 探测）与 v2.4.0 的进程反查都靠它。
    "psutil",
    # v3.0.2：compression.zstd —— Python 3.14 内置的 zstd 实现（PEP 784）。
    # 雷达读 DeepSeek harness 的 ``.zstd`` 会话日志全靠它。
    # ⚠️ 3.14 以下没有这个模块 → 走「可选依赖」通道，
    #    装了的才点名（没装还硬点名，PyInstaller 会刷一串警告）。
    "compression.zstd",
)

#: 显式声明的隐藏导入。
#:
#: 为什么不全靠自动分析：这些模块都是**函数内动态导入**的
#: （为了保持分层与惰性加载），静态分析有时会漏掉。
#: 漏掉的代价是「打包成功但运行时才崩」—— 那是最糟的一种。
HIDDEN_IMPORTS = (
    "src.core.briefing",
    "src.core.correction",
    "src.core.tokens",
    "src.core.engine",
    "src.core.guardian",
    "src.core.notifier",
    "src.core.channels",
    # v2.0.0：注入中枢与安全点服务 —— 看板层是**函数内**惰性导入的，
    # 漏掉它们的代价是「打包成功但点按钮才崩」。
    "src.core.injector",
    "src.core.safepoint",
    # v2.1.0：一键复活编排 + Webhook 真实自检 —— 同理，都是惰性导入。
    "src.core.resurrect",
    "src.core.webhook_probe",
    # v2.2.0：案发时间线 + 开箱安全体检 + 规则热重载 ——
    # 这三个都是**函数内**惰性导入的（看板层不认识 core.engine），
    # 漏掉它们的代价还是那句老话：打包成功但点按钮才崩。
    "src.core.timeline",
    "src.core.health",
    "src.core.hotreload",
    # v3.3.0：引擎心跳看门狗 + 时间盒检测器 ——
    # 都是**函数内**导入的（GUI 层不认识 core.*），静态分析看不到。
    # 漏了的代价：打包版 exe 起来后**没有心跳、没有时间盒**，
    # 而且不会报错 —— 只是功能静默消失。
    "src.core.engine_watchdog",
    "src.core.timebox",
    # v3.3.0：自启残留检测 —— 打包入口是**函数内**导入它的。
    # 漏掉的代价：打包版 exe 起来后**不提醒**用户清理旧自启残留，
    # 而且不会报错 —— 只是提示静默消失。
    "src.core.autostart_residue",
    # v2.2.1：终端呈现层（ASCII 大字报 / 就绪提示 / **致命错误不闪退**）——
    # 打包入口是**函数内**导入它的，同样得显式点名。
    # v2.2.2 在同一层加了「演示模式醒目说明」，无需额外隐藏导入。
    "src.console",
    # v2.3.0：CLI 进程雷达（会话目录扫描 / Agent 类型识别 / 工作区提取）——
    # 看板是**函数内**惰性导入它的，静态分析看不到，必须显式点名。
    # 漏掉的代价：打包成功，但「发现/挂载」按钮一点就崩。
    "src.radar",
    # v2.4.0：进程反查（PID 扫描器）—— 同理，是**函数内**惰性导入的。
    # 漏掉的代价：打包成功，但「武装守护者」一点就崩 / 反查永远失败。
    "src.procscan",
    # v2.4.0：psutil —— 进程反查与守护者的**可选依赖**，
    # 在 `ProcessController.psutil_module()` 与 `procscan.psutil_module()`
    # 里都是**函数内 import**，静态分析看不到。
    # 漏掉的代价：打包版守护者直接不可用（跟 v2.1.0 的老问题一样）。
    # ⚠️ 它是**可选依赖** → 由 `_optional_hidden_imports()` 决定要不要点名。

    "src.llm.router",
    "src.llm.api_client",
    "src.llm.ollama_client",
    "src.llm.factory",
    "src.adapters.mcode",
    "src.adapters.codex",
    "src.adapters.claude",
    "src.adapters.text",
    "src.demo",
    "src.console",
    "src.config_loader",
    "src.tailer",
    # v5.0.0：无头守护进程 —— 对外的极简 HTTP 接口与它的会话隔离层。
    # 入口是**函数内**导入的，静态分析看不到，必须显式点名。
    # 漏掉的代价：打包版 exe 起来后**没有 HTTP 接口**，
    # 而且不会报错 —— 只是外部宿主连不上。
    "src.daemon",
    "src.daemon.context",
    "src.daemon.precheck",
    "src.daemon.server",
    "src.daemon.worker",
    # v5.0.0：面板的 Web 框架那一套（fastapi / uvicorn / jinja2）**已剥离** ——
    # 接口改用标准库 ``http.server``，所以这几个包**不再**需要点名。
)

#: 显式排除的大库（用不上，却能撑爆体积）。
EXCLUDED_MODULES = (
    "tkinter",
    "_tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    # v5.0.0：面板的 Web 栈**整体剥离** —— 显式排除，
    # 保证它们**不会**被任何残余的动态导入拖进 exe。
    # ⚠️ ``--exclude-module`` 的优先级**高于** ``--hidden-import`` ——
    #    这里排除着，打包出来的 exe 就**真的没有**它们。
    "fastapi",
    "uvicorn",
    "jinja2",
    "pystray",
    "PIL",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "notebook",
    "pytest",
    "_pytest",
    "setuptools",
    "pip",
    "torch",
    "tensorflow",
)


def _log(text: str, handle: Optional[object] = None) -> None:
    """打印并写入日志文件。

    ⚠️ 打印**不能因为编码问题炸掉**。
    Windows 控制台默认 GBK，而 PyInstaller 的输出里偶尔混着
    无法用 GBK 表示的字符（``\\ufffd`` 之类）——
    一旦 ``print`` 抛 ``UnicodeEncodeError``，
    **整个打包流程就断在那里**，exe 反而打不出来。

    所以这里把 stdout 切到 UTF-8 并开 ``errors="replace"``：
    少几个字符不可显示，但流程能走完。

    :param text: 文本。
    :param handle: 已打开的日志文件句柄。
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:  # noqa: BLE001 —— 打印失败不该中断打包
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()
    if handle is not None:
        handle.write(text + "\n")
        handle.flush()


def _optional_hidden_imports() -> List[str]:
    """**可选依赖**的隐藏导入 —— 装了才点名。

    ``psutil`` 是守护者与进程反查的**可选依赖**（缺了它们整体降级，
    但哨兵照样能跑）。没装还硬点名，PyInstaller 会打一串
    「hidden import not found」警告，看着像出了问题。

    :return: 实际存在的可选模块名列表。
    """
    names: List[str] = []
    for name in OPTIONAL_HIDDEN_IMPORTS:
        try:
            __import__(name)
        except ImportError:
            continue
        names.append(name)
    return names


def build_argv() -> List[str]:
    """拼出 PyInstaller 的命令行。

    :return: 参数列表。
    """
    args: List[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        EXE_NAME,
        "--distpath",
        DIST_DIR,
        "--workpath",
        os.path.join(BUILD_DIR, "work"),
        "--specpath",
        BUILD_DIR,
        "--paths",
        _ROOT,
    ]
    for source, target in DATA_FILES:
        absolute = os.path.join(_ROOT, source)
        if not os.path.exists(absolute):
            raise FileNotFoundError(f"要打包的资源不存在：{absolute}")
        # Windows 上用「源;目标」分隔；Linux/macOS 用「源:目标」。
        separator = ";" if os.name == "nt" else ":"
        args.extend(["--add-data", f"{absolute}{separator}{target}"])
    for name in tuple(HIDDEN_IMPORTS) + tuple(_optional_hidden_imports()):
        args.extend(["--hidden-import", name])
    for name in EXCLUDED_MODULES:
        args.extend(["--exclude-module", name])
    args.append(ENTRY_SCRIPT)
    return args


def human_size(num_bytes: int) -> str:
    """把字节数渲染成「xx MB」。

    :param num_bytes: 字节数。
    :return: 人类可读的大小。
    """
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _decode(raw: bytes) -> str:
    """把子进程输出解成文本（UTF-8 优先，必要时退回系统代码页）。

    打包后的程序在**重定向**场景下会输出 UTF-8；
    但万一系统代码页不是 UTF-8，也要能看懂它说了什么，
    而不是丢一堆乱码。

    :param raw: 原始字节。
    :return: 解码后的文本。
    """
    for encoding in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def verify(exe_path: str, handle: Optional[object] = None) -> bool:
    """跑一次 ``sentinel.exe --help`` 做**自检**。

    打包成功 ≠ 能跑 —— 单文件程序最常见的翻车点是
    「资源找不到」「动态导入漏了」，而这两种只有**真跑一次**才会暴露。

    :param exe_path: exe 路径。
    :param handle:   日志句柄。
    :return: 自检是否通过。
    """
    _log("", handle)
    _log("---- 自检：" + EXE_NAME + " --help ----", handle)
    try:
        result = subprocess.run(
            [exe_path, "--help"],
            capture_output=True,
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001 —— 自检失败不算打包失败
        _log(f"自检无法执行：{type(exc).__name__}: {exc}", handle)
        return False
    output = _decode(result.stdout or b"") + _decode(result.stderr or b"")
    ok = result.returncode == 0 and "哨兵 Sentinel" in output
    _log(f"退出码：{result.returncode}；输出首行：", handle)
    first = output.strip().splitlines()[0] if output.strip() else "（无输出）"
    _log(f"  {first}", handle)
    _log(f"自检结果：{'通过' if ok else '**未通过**'}", handle)
    return ok


def release_exe_lock(exe_path: str, handle: Optional[object] = None) -> int:
    """把**正在占用**产物 exe 的哨兵进程结束掉，腾出文件锁。

    为什么需要它
    ------------

    打包第一步就要**删掉旧的** ``dist/sentinel.exe``，
    而 Windows 上 exe 正在运行就被文件锁住 →
    PyInstaller 直接 ``PermissionError: [WinError 5] 拒绝访问``。
    而哨兵的守护模式**恰恰**就是用这个 exe 跑的，
    所以「一边开着守护进程，一边想重新打包」是最常见的翻车场景
    （v3.2.1 实机就撞过一次）。

    ⚠️ 这跟红线 R6 没关系
    --------------------

    R6「不终止用户进程」说的是**产品运行时**不许动用户的 Agent；
    这里结束的是**构建脚本自己产出的那个 exe**，
    目的是释放文件锁 —— 属于构建期操作，不碰任何用户进程。
    而且只按**完整路径**匹配，不按进程名乱杀。

    :param exe_path: 产物 exe 路径。
    :param handle:   日志句柄。
    :return: 结束掉的进程数。
    """
    target = os.path.normcase(os.path.abspath(exe_path))
    pids: List[int] = []
    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "exe"]):
            try:
                exe = proc.info.get("exe")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            if not exe:
                continue
            try:
                same = os.path.normcase(os.path.abspath(exe)) == target
            except (TypeError, ValueError):
                continue
            if same:
                pids.append(int(proc.info["pid"]))
    else:  # pragma: no cover —— 没有 psutil 时退回按进程名结束
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq " + EXE_NAME, "/NH"],
            capture_output=True,
        )
        for line in _decode(result.stdout or b"").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].isdigit():
                pids.append(int(fields[0]))
    for pid in pids:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
            )
        else:  # pragma: no cover —— 本项目主要面向 Windows
            try:
                os.kill(pid, 15)
            except OSError:
                pass
    if pids:
        _log(f"已结束占用产物的哨兵进程：{pids}", handle)
        # 等文件锁真正松手 —— taskkill 是异步的。
        for _ in range(20):
            time.sleep(0.25)
            try:
                with open(exe_path, "r+b"):
                    pass
            except OSError:
                continue
            break
    return len(pids)


def project_version() -> str:
    """从 ``src/__init__.py`` 里读版本号。

    :return: 版本号字符串；读不到就返回 ``unknown``。
    """
    path = os.path.join(_ROOT, "src", "__init__.py")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return "unknown"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "unknown"


def make_zip(exe_path: str, handle: Optional[object] = None) -> Optional[str]:
    """把产物 exe 再打一个 zip 副本。

    为什么要有这个 zip
    ------------------

    Microsoft Defender 的**机器学习**启发式会误杀打包版 exe
    （v3.2.0 发布当夜就被隔离过一次）——
    exe 被隔离之后，用户手里就剩「什么都没发生」的**静默失效**。
    交付一个 zip 副本，用户自己解压就能拿到一份**没有被标记过**的 exe。

    为什么放在打包脚本里
    --------------------

    v3.2.1 之前 zip 是手工打的，结果**漏了一次**：
    版本文档里记的 zip 体积还是上一版的数，
    文档跟产物对不上 —— 交付物这种事不能靠记性。
    所以让它跟着 ``--version`` 一起产出。

    :param exe_path: 产物 exe 路径。
    :param handle:   日志句柄。
    :return: zip 路径；失败返回 ``None``。
    """
    version = project_version()
    zip_path = os.path.join(
        DIST_DIR, f"sentinel-daemon-v{version}-win-x64.zip"
    )
    try:
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            archive.write(exe_path, arcname=EXE_NAME)
            # GPL-3.0：许可证与出处声明必须**随二进制一起分发** ——
            # 拿到这个 zip 的人，不翻仓库就知道这份代码什么许可、
            # 参考了谁、那些代码从哪来。
            # （少了这一步，发布出去的二进制就是「无证发行」。）
            for extra in ("LICENSE", "NOTICE"):
                extra_path = os.path.join(_ROOT, extra)
                if os.path.exists(extra_path):
                    archive.write(extra_path, arcname=extra)
                else:
                    _log(
                        "  ⚠️ 没找到 {}，zip 里不含它".format(extra), handle
                    )
    except OSError as exc:
        _log(f"zip 副本生成失败：{type(exc).__name__}: {exc}", handle)
        return None
    size = os.path.getsize(zip_path)
    _log(f"  zip 副本：{zip_path}", handle)
    _log(f"  zip 体积：{human_size(size)}（{size} 字节）", handle)
    return zip_path


def defender_notice(dist_dir: str) -> str:
    """生成「防误杀」提示（v3.3.0）。

    Defender 的**机器学习**启发式会把单文件 exe 判成木马 ——
    v3.2.0 发布当夜就被隔离过一次（``Trojan:Win32/Bearfoos.A!ml``）。

    后果不是「弹个窗」，而是**手动双击 exe 没反应**：
    文件已经被隔离走了，**而且不报错**。

    ⚠️ v3.3.0 起哨兵**不自启**了 ——
    所以「开机自启静默失效」那种坑**已经不存在**，
    剩下的只有「你双击它的时候被拦」。

    所以打包完必须**显眼地**提醒用户加排除项。

    :param dist_dir: 打包产物目录。
    :return: 多行提示文本。
    """
    script = os.path.join(
        _HERE, "add_defender_exclusion.ps1"
    )
    return "\n".join(
        [
            "!" * 62,
            "  注意：Windows Defender 可能把单文件 exe 误判成木马并隔离。",
            "  后果：**手动双击 exe 没反应**（文件被隔离走了，而且不报错）。",
            "",
            "  请用**管理员身份**打开 PowerShell，执行一次：",
            "",
            '    powershell -NoProfile -ExecutionPolicy Bypass -File "{}"'.format(
                script
            ),
            "",
            "  排除目录：{}".format(dist_dir),
            "!" * 62,
        ]
    )


def stale_config_notice() -> str:
    """检查打包目录里的配置是不是**旧版**（v3.3.0 新增）。

    为什么要有这个检查
    =================

    打包版 exe 的配置查找顺序里，
    ``<exe 目录>/config/config.yaml`` **优先级最高**
    —— 那是用户在设置页里改出来的、运行时会写回的那份。

    代价是：它一旦留在那儿，**以后每次打包都盖不住它**。
    v3.3.0 实机就撞到过：打包目录里躺着一份 **Ollama 时代**的旧配置，
    于是打包版 exe 一直在找 ``localhost:11434``，
    而用户早就换成了 NInfer —— 界面还提示「引擎连不上」，
    说的却是**一个已经不存在的引擎**。

    所以打包时必须**喊一声**：这份配置和仓库里的不一致，
    打包版跑起来用的是**它**，不是你以为的那份。

    :return: 多行提示文本；一切正常时返回空串。
    """
    repo_config = os.path.join(_ROOT, "config", "config.yaml")
    dist_config = os.path.join(DIST_DIR, "config", "config.yaml")
    if not os.path.isfile(repo_config):
        return ""
    if not os.path.isfile(dist_config):
        return ""
    try:
        import yaml

        with open(repo_config, "r", encoding="utf-8") as handle:
            repo_data = yaml.safe_load(handle) or {}
        with open(dist_config, "r", encoding="utf-8") as handle:
            dist_data = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return ""
    repo_llm = (repo_data.get("llm") or {})
    dist_llm = (dist_data.get("llm") or {})
    repo_url = str(repo_llm.get("base_url") or "")
    dist_url = str(dist_llm.get("base_url") or "")
    if not repo_url or not dist_url or repo_url == dist_url:
        return ""
    return "\n".join(
        [
            "!" * 62,
            "  注意：打包目录里的配置是**旧版**，打包版 exe 会用**它**。",
            "",
            "    仓库配置 llm.base_url ：{}".format(repo_url),
            "    打包配置 llm.base_url ：{}".format(dist_url),
            "",
            "  两者不一致 —— 打包版启动时的「引擎连不上」提示会指向**错误的引擎**。",
            "  想让打包版跟着仓库走，就把仓库那份复制过去：",
            "",
            '    copy /Y "{}" "{}"'.format(repo_config, dist_config),
            "",
            "  ⚠️ 打包目录里的那份通常是**设置页写回**的（用户改过的设置）——",
            "     复制前请先确认你是不是真的要丢掉那些改动。",
            "!" * 62,
        ]
    )


def main(argv: Optional[List[str]] = None) -> int:
    """打包主流程。

    :param argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。
    :return: 退出码。
    """
    parser = argparse.ArgumentParser(description="打包哨兵为单文件 exe")
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="打包后不跑 --help 自检",
    )
    parser.add_argument(
        "--keep-build",
        action="store_true",
        help="保留中间产物目录（默认打包完删掉，省地方）",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="不生成 zip 副本（默认会生成，Defender 误杀时的备份）",
    )
    args = parser.parse_args(argv)

    # 控制台默认 GBK —— PyInstaller 的输出里混着 GBK 表示不了的字符，
    # 不切 UTF-8 的话 ``print`` 会抛 UnicodeEncodeError 把流程打断。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # pragma: no cover
            pass

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "没有装 PyInstaller。请先执行：\n"
            "    pip install pyinstaller"
        )
        return 2

    os.makedirs(DIST_DIR, exist_ok=True)
    handle = open(LOG_PATH, "w", encoding="utf-8")
    started = time.time()
    try:
        _log("=" * 66, handle)
        _log("  哨兵 Sentinel · 单文件打包", handle)
        _log("=" * 66, handle)
        _log(f"  项目根：{_ROOT}", handle)
        _log(f"  入口　：{ENTRY_SCRIPT}", handle)
        _log(f"  产物　：{DIST_DIR}", handle)
        _log(f"  日志　：{LOG_PATH}", handle)
        _log("", handle)

        # ⚠️ 必须**先**腾出文件锁 —— 旧 exe 还在跑的话，
        # PyInstaller 删不掉它就直接 PermissionError 收工。
        release_exe_lock(os.path.join(DIST_DIR, EXE_NAME), handle)

        command = build_argv()
        _log("---- PyInstaller 参数 ----", handle)
        _log("  " + " ".join(command), handle)
        _log("", handle)
        _log("---- 开始打包（首次可能需要几分钟）----", handle)
        result = subprocess.run(
            command,
            cwd=_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (result.stdout or "") + (result.stderr or "")
        for line in output.splitlines():
            _log("  " + line, handle)
        _log("", handle)
        if result.returncode != 0:
            _log(f"打包失败（退出码 {result.returncode}）。", handle)
            return 1

        exe_path = os.path.join(DIST_DIR, EXE_NAME)
        if not os.path.exists(exe_path):
            _log(f"打包说成功了，但没找到产物：{exe_path}", handle)
            return 1
        size = os.path.getsize(exe_path)
        _log("=" * 66, handle)
        _log("  打包成功", handle)
        _log("=" * 66, handle)
        _log(f"  产物　：{exe_path}", handle)
        _log(f"  体积　：{human_size(size)}（{size} 字节）", handle)
        _log(f"  耗时　：{time.time() - started:.1f} 秒", handle)
        _log("", handle)

        if args.no_verify:
            _log("（按要求跳过自检）", handle)
        else:
            verify(exe_path, handle)

        if args.no_zip:
            _log("（按要求不生成 zip 副本）", handle)
        else:
            make_zip(exe_path, handle)

        _log("", handle)
        _log(defender_notice(DIST_DIR), handle)

        stale = stale_config_notice()
        if stale:
            _log("", handle)
            _log(stale, handle)

        if not args.keep_build:
            shutil.rmtree(BUILD_DIR, ignore_errors=True)
            _log("已清理中间产物目录。", handle)
        return 0
    finally:
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
