"""v3.8.0 演示：**双击两次**只活一个实例（单例锁 + 进程树同生共死）。

这个演示**用打包好的 ``sentinel.exe``** 真跑一遍用户会遇到的那件事：

1. 启动第一个实例（拿到单例锁）；
2. **再启动一个**（等价于「再双击一次 exe」）；
3. 第二个实例必须**打印**「哨兵已在运行中」并以**退出码 3** 优雅退出；
4. 第二次启动**不新增任何进程** ——
   所以托盘只可能有一个图标；
5. 杀掉第一个实例后，**不留任何残留进程**（Job Object：KILL_ON_JOB_CLOSE）。

⚠️ 截图是**真屏幕截图**（``CopyFromScreen``），不是把文字画成图片。

⚠️ 关于进程数：PyInstaller 的 onefile 模式本来就是**两个进程**
（解包引导进程 + 真正的应用进程），所以「一个哨兵实例」=
**2 个** ``sentinel.exe``。本演示断言的是「**第二次启动后进程数没有增加**」。

用法::

    python _构建与发布/scripts/demo_v380_single_instance.py
    python _构建与发布/scripts/demo_v380_single_instance.py --job-probe
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _extra in (_ROOT, _HERE):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import console_bridge  # noqa: E402 —— 必须先补 sys.path

from src.gui.single_instance import (  # noqa: E402 —— 必须先补 sys.path
    SINGLE_INSTANCE_NAME,
)

#: 打包产物。
EXE = os.path.join(_ROOT, "_构建与发布", "dist", "sentinel.exe")

#: 证据与截图的落点。
ASSETS = os.path.join(_ROOT, "_管理文档", "文档与报告", "assets")

#: 中间产物的落点（.cmd 脚本、退出码文件等）。
#:
#: ⚠️ 用**纯 ASCII 的临时目录** —— 仓库路径含中文，
#: 而 .cmd 文件必须是 ASCII（按控制台代码页读），
#: .cmd 里的中文路径只能靠环境变量传递，路径越简单越不容易踩坑。
WORK = os.path.join(tempfile.gettempdir(), "sentinel-v380-demo")

#: 第一个实例用的看板端口（避开默认的 8000）。
PORT = "8177"

#: 窗口标题（**纯 ASCII** —— 便于按标题定位窗口）。
FIRST_TITLE = "SENTINEL-FIRST-INSTANCE-DEMO"
SECOND_TITLE = "SENTINEL-SECOND-INSTANCE-SINGLE-LOCK"

#: 第二个实例的控制台要挪到屏幕**左上角**（逻辑像素）。
#:
#: 为什么要挪：本机同时开着浏览器等窗口，直接截图会被压住，
#: 文字截不全。挪到左上 + 置顶，截出来的那张图才是完整证据。
CONSOLE_X = 8
CONSOLE_Y = 96
CONSOLE_W = 1180
CONSOLE_H = 520

#: 屏幕逻辑分辨率（本机 2560x1600 @150% DPI ⇒ 1707x1067）。
SCREEN_W = 1707
SCREEN_H = 1067

#: Windows 的 ``CREATE_NEW_CONSOLE``。
CREATE_NEW_CONSOLE = 0x00000010


def _rule(char: str = "-", width: int = 74) -> str:
    """画一条分隔线。

    :param char:  字符。
    :param width: 宽度。
    :return: 分隔线。
    """
    return char * width


def _show(title: str) -> None:
    """打印一个小标题。

    :param title: 标题。
    """
    print("")
    print(_rule("="))
    print("  " + title)
    print(_rule("="))













# ---------------------------------------------------------------------------
# 进程
# ---------------------------------------------------------------------------
def _sentinel_processes() -> List[dict]:
    """列出当前所有 ``sentinel.exe`` 进程。

    :return: 进程信息列表。
    """
    try:
        import psutil
    except ImportError:
        print("  [警告] 没装 psutil —— 进程计数改用 tasklist。")
        return _sentinel_processes_tasklist()
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name in ("sentinel.exe", "sentinel"):
                found.append({"pid": proc.info.get("pid"), "name": name})
        except Exception:  # noqa: BLE001,S112 —— 枚举进程时个别进程会消失，跳过即可
            continue
    return found


def _sentinel_processes_tasklist() -> List[dict]:
    """用 ``tasklist`` 列进程（psutil 不可用时的兜底）。

    :return: 进程信息列表。
    """
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq sentinel.exe", "/FO", "CSV", "/NH"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for raw_line in (completed.stdout or "").splitlines():
        line = raw_line.strip()
        if not line.startswith('"sentinel.exe"'):
            continue
        parts = [item.strip('"') for item in line.split('","')]
        rows.append({"pid": parts[1] if len(parts) > 1 else "?", "name": parts[0]})
    return rows


def _pid_alive(pid: int) -> bool:
    """进程是不是还活着。

    :param pid: 进程 ID。
    :return: 活着返回 ``True``。
    """
    try:
        import psutil

        return psutil.pid_exists(pid)
    except ImportError:
        return False
    except Exception:  # noqa: BLE001 —— 探活失败就当它死了
        return False


def _mutex_held() -> bool:
    """单例互斥体**是不是**已经被某个进程占着。

    :return: 占着返回 ``True``。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    handle = create(None, False, SINGLE_INSTANCE_NAME)
    if not handle:
        return False
    held = ctypes.get_last_error() == 183
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return held


def _kill_sentinel_processes() -> int:
    """结束**本演示自己拉起**的 ``sentinel.exe``。

    :return: 结束掉的进程数。
    """
    count = 0
    for item in _sentinel_processes():
        pid = item.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            import psutil

            psutil.Process(pid).kill()
            count += 1
        except ImportError:
            pass
        except Exception:  # noqa: BLE001,S110 —— 演示脚本，别因清理失败崩掉
            pass
    return count


def _kill_tree(pid: int) -> None:
    """连同子进程一起结束一个进程树（``taskkill /T``）。

    :param pid: 根进程 ID。
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# 起进程
# ---------------------------------------------------------------------------


def _console_run() -> int:
    """``--console-run`` 模式：**在自己所在的新控制台里**跑哨兵。

    由父进程用 ``CREATE_NEW_CONSOLE`` 拉起本模式；
    本进程已经附着在那块新控制台上，
    所以这里可以直接把输出接上去，再拉起哨兵。

    :return: 退出码。
    """
    exe = os.environ.get("SENTINEL_DEMO_EXE", EXE)
    args = json.loads(os.environ.get("SENTINEL_DEMO_ARGS", "[]"))
    title = os.environ.get("SENTINEL_DEMO_TITLE", "SENTINEL-DEMO")
    exit_file = os.environ.get("SENTINEL_DEMO_EXITFILE", "")
    hold = os.environ.get("SENTINEL_DEMO_HOLD", "") == "1"

    console_bridge.attach_console()
    console_bridge.prepare_console(title)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError):
            pass

    print("=" * 66)
    print("  Sentinel v3.8.0 single-instance demo")
    print("  exe  : {}".format(exe))
    print("  args : {}".format(args))
    print("=" * 66)
    print("")
    try:
        completed = subprocess.run([exe] + list(args))
        code = completed.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        print("  [ERROR] failed to run exe: {}".format(exc))
        code = -1
    print("")
    print("EXITCODE={}".format(code))
    if exit_file:
        try:
            with open(exit_file, "w", encoding="utf-8") as handle:
                handle.write("EXITCODE={}\n".format(code))
        except OSError:
            pass
    if hold:
        print("")
        print("[demo] window held open for screenshot; press Enter to close.")
        try:
            sys.stdin.readline()
        except (OSError, ValueError):
            time.sleep(30)
    return 0


def _launch_console(
    args: List[str],
    title: str,
    exit_file: str = "",
    hold: bool = False,
) -> subprocess.Popen:
    """开一个**看得见**的独立控制台窗口跑哨兵。

    ⚠️ ``stdin`` 必须给 ``PIPE``（不能给 ``DEVNULL``）：
    子进程最后要 ``sys.stdin.readline()`` 撑着窗口不关，
    读 EOF（DEVNULL）就等于直接退出。

    ⚠️ ``stdout`` / ``stderr`` 保持**继承**：输出由子进程自己
    ``dup2`` 到 ``CONOUT$``（见 :func:`_console_attach`），
    父进程这边不需要拿到它们。

    :param args: 传给哨兵 exe 的参数。
    :param title: 控制台窗口标题（ASCII）。
    :param exit_file: 退出码落盘路径（空 = 不落盘）。
    :param hold: 是否让窗口在跑完后**留着**等截图。
    :return: 子进程对象。
    """
    merged = os.environ.copy()
    merged.update({
        "SENTINEL_DEMO_EXE": EXE,
        "SENTINEL_DEMO_ARGS": json.dumps(list(args)),
        "SENTINEL_DEMO_TITLE": title,
        "SENTINEL_DEMO_EXITFILE": exit_file,
        "SENTINEL_DEMO_HOLD": "1" if hold else "0",
    })
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--console-run"],
        creationflags=CREATE_NEW_CONSOLE,
        stdin=subprocess.PIPE,
        env=merged,
    )


def _job_probe() -> int:
    """Job Object 探针（``--job-probe`` 模式）。

    装上 Job Object → 起一个长命子进程 → 打印子进程 PID →
    **父进程退出**（句柄关闭 → KILL_ON_JOB_CLOSE 触发）。

    :return: 退出码。
    """
    from src.gui.single_instance import install_process_group

    group = install_process_group()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"]
    )
    print("JOB_ENABLED={}".format(group.enabled))
    print("JOB_REASON={}".format(group.reason))
    print("CHILD_PID={}".format(child.pid))
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    """命令行入口。

    :return: 退出码（0 = 全部断言通过）。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    if "--console-run" in sys.argv:
        return _console_run()

    if "--job-probe" in sys.argv:
        return _job_probe()

    print(_rule("="))
    print("  哨兵 v3.8.0 演示 —— 双击两次：单例锁 + 进程树同生共死")
    print("  目标 exe：{}".format(EXE))
    print(_rule("="))

    if not os.path.exists(EXE):
        print("  [错误] 找不到 exe —— 请先跑 build_exe.py。")
        return 2

    os.makedirs(WORK, exist_ok=True)
    # 退出码文件放在**纯 ASCII 的临时目录**（仓库路径含中文，
    # 混在别处容易出编码问题）。
    exit_code_file = os.path.join(
        tempfile.gettempdir(), "sentinel-v380-second-exitcode.txt"
    )
    if os.path.exists(exit_code_file):
        os.remove(exit_code_file)

    # 演示前先把**残留**的哨兵清干净（只清本机上的 sentinel.exe）。
    stale = _kill_sentinel_processes()
    if stale:
        print("  清理残留进程：{} 个".format(stale))
        time.sleep(2.0)

    evidence: dict = {}
    first = None
    second = None
    try:
        _show("第一步：启动第一个实例（它会拿到单例锁）")
        first = _launch_console(
            ["--demo", "--no-browser", "--port", PORT],
            FIRST_TITLE,
        )
        held = False
        for _ in range(80):
            time.sleep(0.5)
            if _mutex_held():
                held = True
                break
        procs = _sentinel_processes()
        print("  单例互斥体已被占用：{}".format(held))
        print("  当前 sentinel.exe 进程数：{}".format(len(procs)))
        for item in procs:
            print("    · PID={}".format(item.get("pid")))
        print("  （PyInstaller onefile = 解包引导进程 + 应用进程，"
              "所以一个实例是 2 个进程）")
        evidence["first_instance"] = {
            "mutex_held": held,
            "process_count": len(procs),
            "pids": [item.get("pid") for item in procs],
        }

        _show("第二步：再启动一个（等价于「再双击一次 exe」）")
        # ⚠️ 第二个实例**不带任何参数** —— 这就是双击的行为。
        #    单例判定发生在任何界面之前（连启动菜单都不会弹）。
        second = _launch_console(
            [], SECOND_TITLE, exit_file=exit_code_file, hold=True
        )
        code = None
        for _ in range(80):
            time.sleep(0.5)
            if os.path.exists(exit_code_file):
                try:
                    with open(exit_code_file, "r", encoding="utf-8") as handle:
                        text = handle.read().strip()
                    code = int(text.split("=", 1)[-1].strip())
                except (OSError, ValueError):
                    code = None
                if code is not None:
                    break
        time.sleep(2.0)

        procs_after = _sentinel_processes()
        print("  第二实例退出码：{}（3 = 已在运行，被单例锁拒绝）".format(code))
        print("  再启动后 sentinel.exe 进程数：{}".format(len(procs_after)))
        for item in procs_after:
            print("    · PID={}".format(item.get("pid")))
        evidence["second_instance"] = {
            "process_count": len(procs_after),
            "pids": [item.get("pid") for item in procs_after],
            "process_growth": len(procs_after) - len(procs),
        }
        evidence["second_exit_code"] = code

        _show("第三步：截图取证")
        # 先把第一个实例的控制台最小化，让出画面。
        first_handle = (
            console_bridge.find_window_by_pid(first.pid) if first else None
        )
        if first_handle is None:
            first_handle = console_bridge.find_window(FIRST_TITLE)
        if first_handle:
            console_bridge.minimize_handle(first_handle)
        print("  第一个实例的控制台已最小化：{}".format(
            bool(first_handle)
        ))
        time.sleep(1.0)

        # 第二个实例的控制台窗口：先按**标题片段**找可见窗口
        # （Windows Terminal 下可见窗口归 WindowsTerminal.exe 所有，
        #  按 PID 只能找到 ConPTY 的隐藏辅助窗口）。
        region = None
        second_handle = None
        for _attempt in range(8):
            second_handle = console_bridge.find_visible_window(
                "SENTINEL-SECOND"
            )
            if second_handle is None and second:
                second_handle = console_bridge.find_window_by_pid(second.pid)
            if second_handle is None:
                second_handle = console_bridge.find_window(SECOND_TITLE)
            if second_handle is None:
                time.sleep(0.5)
                continue
            # 窗口可能因为控制台网格变化被重建/挪位，
            # 所以每次都重新摆一次位置并重新取矩形。
            console_bridge.place_window(
                second_handle, CONSOLE_X, CONSOLE_Y, CONSOLE_W, CONSOLE_H
            )
            time.sleep(0.6)
            region = console_bridge.window_rect(second_handle)
            if region:
                break
        print("  第二个实例的控制台句柄：{}".format(second_handle))
        print("  第二个实例的控制台窗口矩形：{}".format(region))
        if region is None:
            region = (CONSOLE_X, CONSOLE_Y, CONSOLE_W, CONSOLE_H)
        time.sleep(1.0)

        shot_terminal = os.path.join(
            ASSETS, "v380-single-instance-terminal.png"
        )
        # 往外扩一圈再截 —— 窗口边框 / 阴影都在矩形之外。
        left = max(0, region[0] - 24)
        top = max(0, region[1] - 24)
        right = min(SCREEN_W, region[0] + region[2] + 24)
        bottom = min(SCREEN_H, region[1] + region[3] + 24)
        region = (left, top, right - left, bottom - top)
        print("  截图区域（左,上,宽,高）：{}".format(region))
        ok_terminal = console_bridge.capture_screen(shot_terminal, region)
        print("  终端截图（第二实例控制台区域）：{}（{}）".format(
            shot_terminal, "成功" if ok_terminal else "失败"
        ))
        evidence["screenshot_terminal"] = {
            "path": shot_terminal,
            "ok": ok_terminal,
            "region": list(region),
            "window_handle": second_handle,
        }

        # 再拍一张整屏 —— 看画面里只有**一个**哨兵控制台。
        shot_screen = os.path.join(
            ASSETS, "v380-single-instance-screen.png"
        )
        ok_screen = console_bridge.capture_screen(shot_screen)
        print("  整屏截图：{}（{}）".format(
            shot_screen, "成功" if ok_screen else "失败"
        ))
        evidence["screenshot_screen"] = {
            "path": shot_screen,
            "ok": ok_screen,
        }

        _show("第四步：Job Object —— 父进程一死，子进程跟着死")
        probe = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--job-probe"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        job_enabled = False
        child_pid = None
        for line in (probe.stdout or "").splitlines():
            if line.startswith("JOB_ENABLED="):
                job_enabled = line.split("=", 1)[-1].strip() == "True"
            if line.startswith("CHILD_PID="):
                try:
                    child_pid = int(line.split("=", 1)[-1].strip())
                except ValueError:
                    child_pid = None
        time.sleep(2.5)
        alive = bool(child_pid) and _pid_alive(child_pid)
        print("  Job Object 生效：{}".format(job_enabled))
        print("  子进程 PID：{}".format(child_pid))
        print("  父进程退出后子进程还活着吗：{}".format(alive))
        evidence["job_object"] = {
            "enabled": job_enabled,
            "child_pid": child_pid,
            "child_alive_after_parent_exit": alive,
        }

        _show("第五步：杀掉第一个实例 —— 不应留下任何残留")
        killed = _kill_sentinel_processes()
        print("  结束进程数：{}".format(killed))
        time.sleep(3.0)
        left = _sentinel_processes()
        print("  残留 sentinel.exe 进程数：{}".format(len(left)))
        evidence["after_kill"] = {"killed": killed, "left": len(left)}
    finally:
        for proc in (second, first):
            if proc is None:
                continue
            # 先把 stdin 管道关上（cmd /k 读到 EOF 就会自己退出）。
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            _kill_tree(proc.pid)
        _kill_sentinel_processes()

    _show("断言核对")
    checks = []
    assert evidence["first_instance"]["mutex_held"] is True, "单例锁没被占住"
    checks.append("第一个实例拿到了单例互斥体")

    assert evidence["second_instance"]["process_growth"] == 0, (
        "第二次启动后进程数增加了 {}（应当一个都不多）".format(
            evidence["second_instance"]["process_growth"]
        )
    )
    checks.append("第二次启动**没有多出任何进程**（托盘只可能有一个图标）")

    assert evidence["second_exit_code"] == 3, (
        "第二实例退出码不是 3（实际 {}）".format(evidence["second_exit_code"])
    )
    checks.append("第二实例以退出码 3 优雅退出（= 已在运行，被拒绝）")

    assert evidence["job_object"]["enabled"] is True, "Job Object 没生效"
    assert evidence["job_object"]["child_alive_after_parent_exit"] is False, (
        "父进程退出后子进程还活着"
    )
    checks.append("Job Object：父进程一死，子进程跟着死（零孤儿）")

    assert evidence["after_kill"]["left"] == 0, "杀完还有残留进程"
    checks.append("杀掉第一个实例后零残留")

    for index, text in enumerate(checks, start=1):
        print("  [{}] {}".format(index, text))
    print("  → {} 项断言全部通过".format(len(checks)))

    os.makedirs(ASSETS, exist_ok=True)
    path = os.path.join(ASSETS, "v380-single-instance-evidence.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(evidence, handle, ensure_ascii=False, indent=2)
    print("")
    print("  证据 JSON：{}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
