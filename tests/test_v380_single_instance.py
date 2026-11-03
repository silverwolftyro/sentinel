"""v3.8.0 回归 —— 单例锁 · 进程树同生共死 · 托盘销毁顺序。

背景
====

用户长期反馈两个痛点：双击两次 exe 出现**两个托盘图标**，
以及关掉黑框之后后台还留着 ``sentinel.exe``（幽灵进程）。

v3.8.0 用两条路堵：

1. **单例锁** —— Windows 命名互斥体（``CreateMutexW``），
   别的实例在跑就**优雅退出**（退出码 3）；
2. **Job Object**（``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``）——
   哨兵一死，Job 里的成员（哨兵自己 + Agent 子进程）**一起被回收**。

本模块把这些行为**真的跑一遍**，
而不是只检查「文件里有没有这个字符串」。
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from src.gui import single_instance as si

#: 仓库根目录。
ROOT = Path(__file__).resolve().parents[1]

#: 入口脚本路径。
ENTRY_PATH = ROOT / "_构建与发布" / "scripts" / "sentinel_entry.py"


def _load_entry() -> Any:
    """按路径加载入口脚本（它不在包内）。"""
    spec = importlib.util.spec_from_file_location(
        "sentinel_entry_v380_under_test", ENTRY_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


entry = _load_entry()


@pytest.fixture(autouse=True)
def _释放单例锁():
    """每个用例前后都把进程内的单例锁清干净。

    ⚠️ 模块级的 ``_SINGLETON`` 是**故意**做成进程内幂等的，
        所以测试之间必须自己收尾，否则会互相污染。
    """
    si.release_single_instance()
    yield
    si.release_single_instance()


class Test单例锁:
    """单例锁本身的行为。"""

    def test_第一次能拿到锁(self) -> None:
        """第一个实例当然要拿得到锁。"""
        guard = si.SingleInstance("SentinelTestSingleInstanceA")
        assert guard.acquire() is True
        assert guard.acquired is True
        assert guard.mode in ("mutex", "file")
        guard.release()

    def test_第二个实例拿不到锁(self) -> None:
        """★ 双开的核心：第二个实例必须被拒。

        ⚠️ 这里用**同一个名字**再拿一次 ——
            命名互斥体对同一个名字只允许一个持有者。
        """
        name = "SentinelTestSingleInstanceB"
        first = si.SingleInstance(name)
        assert first.acquire() is True
        try:
            second = si.SingleInstance(name)
            assert second.acquire() is False
            assert second.acquired is False
        finally:
            first.release()

    def test_释放之后能再拿到(self) -> None:
        """释放了锁就该能再拿 —— 不然「彻底退出后重启」会被自己拦住。"""
        name = "SentinelTestSingleInstanceC"
        first = si.SingleInstance(name)
        assert first.acquire() is True
        first.release()
        second = si.SingleInstance(name)
        assert second.acquire() is True
        second.release()

    def test_不同名字互不影响(self) -> None:
        """锁是按名字走的 —— 不同名字不打架。"""
        first = si.SingleInstance("SentinelTestSingleInstanceD")
        second = si.SingleInstance("SentinelTestSingleInstanceE")
        assert first.acquire() is True
        assert second.acquire() is True
        first.release()
        second.release()

    def test_模块接口进程内幂等(self) -> None:
        """★ 同一进程重复拿，拿到的必须是**同一个**对象。

        ⚠️ 这一点很关键：``sentinel_entry.py`` 会转手调用
            ``run_with_sentinel.main()``，两处都要上锁，
            但它们在**同一个进程**里 —— 不能自己把自己拦下来。

        ⚠️ 这里用**测试专用名字** ——
            不能去抢真实哨兵的名字：
            万一用户正开着哨兵，这个用例就会假失败。
        """
        name = "SentinelTestModuleSingleton"
        first = si.acquire_single_instance(name)
        assert first is not None
        second = si.acquire_single_instance(name)
        assert second is first
        si.release_single_instance()

    def test_退出码跟正常退出区分得开(self) -> None:
        """「已经在运行」要用**自己的**退出码，不能跟正常退出混。"""
        assert si.ALREADY_RUNNING_EXIT_CODE == 3
        assert si.ALREADY_RUNNING_EXIT_CODE not in (0, 2)


class Test已经在运行的提示:
    """提示文案与输出。"""

    def test_提示文案说清楚是已经在运行(self) -> None:
        """用户一眼就要看懂「它没坏，是已经在跑」。"""
        text = si.already_running_message()
        assert "哨兵已在运行中" in text
        assert "不会启动第二个实例" in text

    def test_终端提示能写出来(self) -> None:
        """终端提示必须真的写出去 —— 打包成 exe 后没有别的提示渠道。"""
        buffer = io.StringIO()
        si.report_already_running(stream=buffer)
        text = buffer.getvalue()
        assert "哨兵已在运行中" in text
        assert "彻底退出" in text

    def test_弹窗可以被环境变量关掉(self, monkeypatch) -> None:
        """服务器上跑就得能关掉弹窗 —— 否则会卡在模态框上。"""
        monkeypatch.setenv("SENTINEL_SINGLE_INSTANCE_DIALOG", "0")
        assert si.show_already_running_dialog() is False


class Test进程树同生共死:
    """Job Object 的行为。"""

    def test_环境变量可以关掉(self, monkeypatch) -> None:
        """关掉之后必须**优雅降级**，而不是报错。"""
        monkeypatch.setenv("SENTINEL_JOB_OBJECT", "0")
        group = si.ProcessGroup()
        assert group.install() is False
        assert group.enabled is False
        assert "SENTINEL_JOB_OBJECT" in group.reason

    def test_进程内幂等(self) -> None:
        """装一次就够 —— 反复装会漏掉句柄。"""
        first = si.install_process_group()
        second = si.install_process_group()
        assert first is second

    @pytest.mark.skipif(os.name != "nt", reason="Job Object 是 Windows 专属")
    def test_windows上真的生效(self, monkeypatch) -> None:
        """★ 本机实测：Job 建得出来、哨兵真的被放进了 Job。"""
        monkeypatch.setenv("SENTINEL_JOB_OBJECT", "1")
        group = si.ProcessGroup()
        assert group.install() is True
        assert group.enabled is True
        assert "KILL_ON_JOB_CLOSE" in group.reason

    @pytest.mark.skipif(os.name == "nt", reason="Windows 才有 Job Object")
    def test_非windows优雅降级(self) -> None:
        """非 Windows 上没有 Job Object —— 但不能报错。"""
        group = si.ProcessGroup()
        assert group.install() is False
        assert group.reason


class _StopDispatch(Exception):
    """用来在入口的分发中途叫停。"""


class Test入口的单例拦截:
    """``sentinel_entry.py`` 里的拦截规则。"""

    def test_第二个实例被优雅拒绝(self, monkeypatch) -> None:
        """★ 双击第二次 → 提示 + 退出码 3，**不**继续往下跑。"""
        calls: list[str] = []
        monkeypatch.setattr(
            entry, "_acquire_single_instance", lambda: calls.append("lock") or False
        )
        monkeypatch.setattr(entry, "_report_already_running", lambda: calls.append("tip"))
        monkeypatch.setattr(
            entry,
            "_ensure_project_root",
            lambda: (_ for _ in ()).throw(_StopDispatch("不该走到这里")),
        )

        code = entry._dispatch([])

        assert code == si.ALREADY_RUNNING_EXIT_CODE
        assert calls == ["lock", "tip"]


    def test_version不上锁(self, monkeypatch) -> None:
        """``--version`` 同理 —— 它只是一行版本号。"""
        monkeypatch.setattr(
            entry,
            "_acquire_single_instance",
            lambda: pytest.fail("--version 不该上锁"),
        )
        assert entry._dispatch(["--version"]) == 0

    def test_scan不上锁(self, monkeypatch) -> None:
        """★ ``--scan`` 是一次性的命令行扫描。

        ⚠️ 它不开托盘、不开服务，本来就没有「双开托盘」的风险，
            不该被单例锁拦下来。
        """
        monkeypatch.setattr(
            entry,
            "_acquire_single_instance",
            lambda: pytest.fail("--scan 不该上锁"),
        )
        monkeypatch.setattr(
            entry,
            "_ensure_project_root",
            lambda: (_ for _ in ()).throw(_StopDispatch("到这儿就够了")),
        )
        with pytest.raises(_StopDispatch):
            entry._dispatch(["--scan"])


class Test托盘销毁顺序:
    """★ 托盘图标必须在 ``os._exit`` **之前**收掉。"""
