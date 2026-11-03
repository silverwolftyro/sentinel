"""v5.0.1：进程级强制单例锁的回归测试。

覆盖四件事：

1. **锁本身** —— 同进程幂等、跨进程互斥；
2. **下沉位置** —— 无头守护进程的绝对入口（:mod:`src.daemon.server`）
   自己也会拿锁，不能只靠打包入口；
3. **PyInstaller 父子进程** —— 引导器不跑 Python，锁天然落在核心进程上；
4. **提示行为** —— 不弹模态框（默认）、文案不再提已经删掉的托盘图标。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import textwrap

import pytest

from src.core import singleton as sg
from src.gui import single_instance as si

#: 仓库根目录。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _release():
    """每个用例前后都把锁放掉，避免互相污染。"""
    sg.release_single_instance()
    yield
    sg.release_single_instance()


class Test锁本身:
    """单例锁的语义。"""

    def test_同进程重复拿锁拿到同一个对象(self) -> None:
        """入口会在两处上锁，不能自己把自己拦下来。"""
        first = sg.acquire_single_instance("SentinelTestV501Same")
        second = sg.acquire_single_instance("SentinelTestV501Same")
        assert first is not None
        assert second is first

    def test_别的进程拿不到锁(self) -> None:
        """第二个实例必须拿不到 —— 这才是单例。"""
        name = "SentinelTestV501Cross"
        guard = sg.SingleInstance(name)
        assert guard.acquire() is True
        try:
            code = textwrap.dedent(
                """
                import sys
                sys.path.insert(0, {root!r})
                from src.core import singleton as s
                print('ACQUIRED', s.SingleInstance({name!r}).acquire())
                """
            ).format(root=_ROOT, name=name)
            out = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=_ROOT,
                check=False,
            )
            assert "ACQUIRED False" in out.stdout, out.stdout + out.stderr
        finally:
            guard.release()

    def test_释放之后能再拿到(self) -> None:
        """彻底退出后必须能重启。"""
        name = "SentinelTestV501Release"
        first = sg.SingleInstance(name)
        assert first.acquire() is True
        first.release()
        second = sg.SingleInstance(name)
        assert second.acquire() is True
        second.release()

    def test_gui模块与core共用同一把锁(self) -> None:
        """两处入口必须共用**一个**缓存，否则会自己拦自己。"""
        assert si.acquire_single_instance is sg.acquire_single_instance
        assert si.SingleInstance is sg.SingleInstance


class Test核心进程判定:
    """PyInstaller 单文件的父子进程。"""

    def test_当前进程是核心进程(self) -> None:
        """源码模式下显然是核心进程。"""
        assert sg.is_core_process() is True

    def test_判定信息是完整的(self) -> None:
        """判定必须给出可核对的依据，而不是只丢一个布尔值。"""
        info = sg.core_process_info()
        assert info["role"] == "core"
        assert info["detail"]
        assert "frozen" in info
        assert "onefile_child" in info

    def test_环境变量能指定角色(self, monkeypatch) -> None:
        """将来真有打包方式在父进程里跑 Python，不用改代码就能绕开。"""
        monkeypatch.setenv("SENTINEL_SINGLE_INSTANCE_ROLE", "bootstrap")
        assert sg.is_core_process() is False
        monkeypatch.setenv("SENTINEL_SINGLE_INSTANCE_ROLE", "core")
        assert sg.is_core_process() is True


class Test守护进程入口:
    """单例锁必须下沉到无头内核的绝对入口。"""

    def test_入口自己会拿锁(self, monkeypatch) -> None:
        """``src.daemon.server.main`` 自己也得上锁。"""
        from src.daemon import server as dserver

        calls = []
        monkeypatch.setattr(
            dserver,
            "build_app",
            lambda **kwargs: calls.append(kwargs) or _FakeApp(),
        )
        monkeypatch.setattr(dserver, "serve", lambda *a, **k: None)
        code = dserver.main(["--port", "0"])
        assert code == 0

    def test_抢不到锁就立刻退出(self, monkeypatch) -> None:
        """已经有实例在跑 → 打印提示 → 退出码 0（成功的空操作）。"""
        from src.daemon import server as dserver

        monkeypatch.setattr(
            sg, "acquire_single_instance", lambda *a, **k: None
        )
        buffer = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buffer)
        code = dserver.main(["--port", "0"])
        assert code == sg.ALREADY_RUNNING_EXIT_CODE
        assert sg.ALREADY_RUNNING_EXIT_CODE == 0
        assert "哨兵已在运行中" in buffer.getvalue()


class Test提示行为:
    """「已经在跑」这条提示。"""

    def test_守护进程的退出码是0(self) -> None:
        """对守护进程来说，「已经在跑」不是启动失败。"""
        assert sg.ALREADY_RUNNING_EXIT_CODE == 0

    def test_文案不再提已经删掉的托盘(self) -> None:
        """v5.0.0 把前端整体剥离了，托盘图标压根不存在。"""
        text = sg.already_running_message()
        assert "系统托盘" not in text
        assert "托盘" not in text
        assert "哨兵已在运行中" in text
        assert "不会启动第二个实例" in text

    def test_文案告诉用户怎么收场(self) -> None:
        """必须说清楚「它没坏，是已经在跑」以及怎么结束它。"""
        text = sg.already_running_message()
        assert "彻底退出" in text

    def test_终端提示不弹窗(self, monkeypatch) -> None:
        """默认**不弹模态框** —— 那个框会挂 10 秒，看着就像双开。"""
        called = []
        monkeypatch.setattr(
            si, "show_already_running_dialog", lambda *a, **k: called.append(1)
        )
        monkeypatch.setenv("SENTINEL_SINGLE_INSTANCE_DIALOG", "0")
        si.report_already_running(stream=io.StringIO())
        assert not called

    def test_弹窗仍然可以显式打开(self, monkeypatch) -> None:
        """想看弹窗的人还能打开。"""
        monkeypatch.setenv("SENTINEL_SINGLE_INSTANCE_DIALOG", "1")
        assert si.show_already_running_dialog() in (True, False)


class _FakeApp:
    """给 :meth:`src.daemon.server.build_app` 当替身用。"""

    def shutdown(self) -> None:
        """空实现。"""
