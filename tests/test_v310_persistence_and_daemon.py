"""v3.1.0 回归：持久化会话记忆与手动挂载引导。

三块：

1. **挂载持久化**（任务一）——
   挂上去的会话写进 ``config/attached_sessions.json``，
   哨兵重启后**自动接回来**；失效的（文件没了 / 静默超过 1 小时）
   被清理掉并打印 ``[SENTINEL] 清理失效的挂载历史: xxx``；
2. **手动重定向指引**（任务二）——
   雷达里压缩日志的按钮**能点**、点了给「怎么接入」的指引；
   设置页有「如何让我的 Agent 被监控」折叠面板，
   列出 mcode / codex / claude / deepseek 各自带重定向的启动命令；
3. **守护与开机自启**（任务三）——
   ⚠️ **v3.3.0 已彻底删除** —— 断言方向反过来了：
   现在断言的是「守护模式与自启脚本**一个都不许再存在**」。
   完整的「无自启」验收断言见 ``tests/test_v330_no_autostart.py``。

⚠️ 本文件里有几条测试**读源码文本**。
这是有意的：像「禁用按钮收不到 click」这种约定，
被改坏时**运行时不一定报错**，但用户点了就是没反应。
"""

from __future__ import annotations

import importlib.util
import json
import os
import time

from src.attachments import (
    ATTACHED_FILE_NAME,
    FORMAT_VERSION,
    STALE_SECONDS,
    AttachmentStore,
    default_attached_path,
)

#: 项目根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts: str) -> str:
    """读仓库里的一个文本文件。

    :param parts: 相对项目根的路径片段。
    :return: 文件文本。
    """
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _load_entry():
    """加载打包入口脚本（它不在 ``src`` 包里，是独立脚本）。"""
    path = os.path.join(
        ROOT, "_构建与发布", "scripts", "sentinel_entry.py"
    )
    spec = importlib.util.spec_from_file_location(
        "sentinel_entry_v310", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_factory(tmp_path, calls=None):
    """造一个假的挂载工厂（不真的起审查引擎）。

    :param tmp_path: pytest 的临时目录。
    :param calls: 记录调用次数的列表（可为 ``None``）。
    :return: 工厂函数。
    """
    recorded = calls if calls is not None else []

    def factory(info):
        """假工厂：只记录调用。"""
        recorded.append(dict(info))
        return {
            "ok": True,
            "alert_log_path": str(tmp_path / "attached.alerts.jsonl"),
            "scan_callback": lambda target: {"ok": True, "counts": {}},
            "workdir": info.get("cwd", ""),
            "tail_runner": None,
            "arm_callback": _arm,
        }

    return factory


def _arm(armed):
    """假的武装回调。

    :param armed: 是否武装。
    :return: 武装结果。
    """
    return {"ok": True, "armed": bool(armed)}




def _touch(path, seconds_ago=0.0):
    """把一个文件的修改时间往前挪。

    :param path: 文件路径。
    :param seconds_ago: 往前挪多少秒。
    """
    stamp = time.time() - float(seconds_ago)
    os.utime(str(path), (stamp, stamp))


# ---------------------------------------------------------------------------
# 1. AttachmentStore（持久化仓库）
# ---------------------------------------------------------------------------
class Test挂载持久化仓库:
    """``src/attachments.py`` 的行为约定。"""

    def test_默认路径在哨兵自己的config目录(self):
        """持久化文件必须落在哨兵自己的 ``config/`` 下。

        绝不写进用户被监控的项目目录（红线 R1）。
        """
        path = default_attached_path()
        assert path.endswith(ATTACHED_FILE_NAME)
        assert os.sep + "config" + os.sep in path
        assert "logs" not in os.path.basename(path)

    def test_默认路径可以被指定目录覆盖(self, tmp_path):
        """打包版要落在 exe 旁边的 ``config/`` 里。"""
        path = default_attached_path(str(tmp_path))
        assert os.path.dirname(path) == os.path.abspath(str(tmp_path))
        assert os.path.basename(path) == ATTACHED_FILE_NAME

    def test_写进去能读回来(self, tmp_path):
        """挂载信息要能原样往返。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = tmp_path / "session.jsonl"
        target.write_text("{}\n", encoding="utf-8")

        assert store.save_entry(
            {
                "path": str(target),
                "agent": "codex",
                "cwd": "D:/work",
                "armed": True,
                "session_id": "abc",
            }
        )

        entries = store.load()
        assert len(entries) == 1
        assert entries[0]["agent"] == "codex"
        assert entries[0]["cwd"] == "D:/work"
        assert entries[0]["armed"] is True
        assert entries[0]["session_id"] == "abc"

    def test_文件里带版本号(self, tmp_path):
        """格式版本要写进文件 —— 以后改结构时能迁移。"""
        path = tmp_path / ATTACHED_FILE_NAME
        store = AttachmentStore(str(path))
        store.save_entry({"path": str(tmp_path / "x.jsonl")})

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == FORMAT_VERSION
        assert isinstance(payload["attachments"], list)

    def test_同一个路径只留一条(self, tmp_path):
        """重复挂载同一个文件不能产生两条记录。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = str(tmp_path / "session.jsonl")

        store.save_entry({"path": target, "agent": "codex"})
        store.save_entry({"path": target, "agent": "codex", "armed": True})

        entries = store.load()
        assert len(entries) == 1
        assert entries[0]["armed"] is True

    def test_武装状态能单独改(self, tmp_path):
        """武装是一个独立开关，要能单独改。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = str(tmp_path / "session.jsonl")
        store.save_entry({"path": target, "agent": "codex"})

        assert store.update(target, armed=True) is True
        assert store.load()[0]["armed"] is True

    def test_改不存在的条目会返回False(self, tmp_path):
        """改一个没挂过的路径不该凭空造一条出来。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        assert store.update(str(tmp_path / "nope.jsonl"), armed=True) is False
        assert store.load() == []

    def test_能删掉一个挂载(self, tmp_path):
        """用户主动卸载 = 不再持久化。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = str(tmp_path / "session.jsonl")
        store.save_entry({"path": target})

        assert store.remove(target) is True
        assert store.load() == []

    def test_删一个没挂过的返回False(self, tmp_path):
        """删不存在的条目不算「变了」。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        assert store.remove(str(tmp_path / "nope.jsonl")) is False

    def test_文件没了就算失效(self, tmp_path):
        """日志文件被删了 → 挂载历史失效。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        store.save_entry({"path": str(tmp_path / "gone.jsonl")})

        kept, dropped = store.prune()
        assert kept == []
        assert len(dropped) == 1
        assert store.load() == []

    def test_静默超过一小时就算失效(self, tmp_path):
        """Agent 早就跑完了的日志 —— 盯着它没有意义。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = tmp_path / "old.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        _touch(target, seconds_ago=STALE_SECONDS + 600)
        store.save_entry({"path": str(target)})

        kept, dropped = store.prune()
        assert kept == []
        assert len(dropped) == 1

    def test_还在写着的不会被清掉(self, tmp_path):
        """正在写入的日志要留下来。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        target = tmp_path / "live.jsonl"
        target.write_text("{}\n", encoding="utf-8")
        store.save_entry({"path": str(target)})

        kept, dropped = store.prune()
        assert len(kept) == 1
        assert dropped == []

    def test_文件是坏JSON也不会崩(self, tmp_path):
        """坏文件不能让哨兵起不来（红线 R5）。"""
        path = tmp_path / ATTACHED_FILE_NAME
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        store = AttachmentStore(str(path))
        assert store.load() == []

    def test_写不进去也不崩(self, tmp_path):
        """存不下来只是存不下来，挂载本身不能因此失败。"""
        blocker = tmp_path / "not-a-folder"
        blocker.write_text("x", encoding="utf-8")
        store = AttachmentStore(str(blocker / ATTACHED_FILE_NAME))
        assert store.save_entry({"path": str(blocker / "x.jsonl")}) is False

    def test_写文件是原子的不留下临时文件(self, tmp_path):
        """原子写：同目录不该残留 ``.tmp``。"""
        store = AttachmentStore(str(tmp_path / ATTACHED_FILE_NAME))
        store.save_entry({"path": str(tmp_path / "a.jsonl")})
        leftovers = [
            name
            for name in os.listdir(str(tmp_path))
            if name.endswith(".tmp")
        ]
        assert leftovers == []


# ---------------------------------------------------------------------------
# 2. 看板 API：挂载 → 持久化 → 重启自动接回来
# ---------------------------------------------------------------------------
class Test挂载持久化与自动重连:
    """任务一在看板这一层的端到端行为。"""












# ---------------------------------------------------------------------------
# 3. 守护模式与开机自启（v3.3.0 已彻底删除）
# ---------------------------------------------------------------------------
class Test守护与自启已删除:
    """v3.1.0 的「守护模式」与「开机自启」，在 v3.3.0 被**彻底删除**。

    ⚠️ 这一节的断言方向**反过来了**：
    v3.1.0 时断言「自启脚本存在而且能用」，
    v3.3.0 起断言「**这些东西一个都不许再存在**」。

    完整的「无自启」验收断言（删了哪些文件、清了哪些位置、
    残留检测怎么提示）放在 ``tests/test_v330_no_autostart.py``。
    """


    def test_入口脚本不再认识守护模式(self):
        """``_is_daemon()`` 已删除 —— 入口不再有「后台」这个概念。"""
        entry = _load_entry()
        assert not hasattr(entry, "_is_daemon")


    def test_不闪退的老行为还在(self):
        """普通模式出错仍然挂住等回车 —— v2.2.1 的承诺没变。"""
        from src.console import fatal_guard

        assert fatal_guard(lambda: 0, wait=True) == 0


# ---------------------------------------------------------------------------
# 4. 手动重定向指引（任务二）
# ---------------------------------------------------------------------------
class Test手动重定向指引:
    """任务二的两处 GUI 引导。"""
