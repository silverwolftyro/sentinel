"""实时流式监听（``tail_stream``）测试。

核心是**真并发**：后台线程往文件里追加内容，主线程同步读，
验证事件能在写入后**立刻**吐出来 —— 而不是攒到文件写完才出现。

同时覆盖三个真实世界里最容易出错的边界：

* 文件**轮转**（同名换新文件）；
* 文件被**截断**（清空重写）；
* 只写**半行**（不能把半行当完整事件吐出去）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Iterator, List, Tuple

from src.adapters.base import BaseAdapter
from src.adapters.mcode import McodeAdapter


def _line(command: str = "ls -la") -> str:
    """构造一行 mcode 格式的 tool_call 日志。"""
    return json.dumps(
        {
            "type": "tool_call",
            "tool_call": {"name": "Bash", "arguments": {"command": command}},
        },
        ensure_ascii=False,
    )


def _thinking(text: str = "我在看看目录") -> str:
    """构造一行 mcode 格式的 thinking 日志。"""
    return json.dumps(
        {"type": "thinking", "thinking": text}, ensure_ascii=False
    )


class _Writer:
    """后台写手：按节奏往文件里追加行。"""

    def __init__(self, path: str, lines: List[str], delay: float = 0.05):
        self.path = path
        self.lines = lines
        self.delay = delay
        self._thread: threading.Thread | None = None
        self._done = threading.Event()

    def start(self) -> "_Writer":
        """启动写手线程。"""
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            for line in self.lines:
                handle.write(line + "\n")
                handle.flush()
                time.sleep(self.delay)
        self._done.set()

    def wait(self, timeout: float = 5.0) -> None:
        """等写手写完。"""
        assert self._done.wait(timeout), "写手超时"


def _open_stream(
    path: str,
    *,
    interval: float = 0.05,
    from_end: bool = True,
) -> Tuple[Iterator[Dict[str, Any]], threading.Event]:
    """开一个跟随流，并返回它的停止信号。"""
    stop = threading.Event()
    adapter = McodeAdapter()
    stream = adapter.tail_stream(
        path, interval=interval, from_end=from_end, stop_event=stop
    )
    return stream, stop


def _collect(
    stream: Iterator[Dict[str, Any]],
    count: int,
    timeout: float = 8.0,
) -> List[Dict[str, Any]]:
    """从流里取前 ``count`` 个事件（带**硬超时**，避免测试挂死）。

    超时必须**独立于事件到达** —— 一条事件都不来的时候，
    在迭代器里是等不出来的。所以把消费放到后台线程，主线程只等结果。
    """
    collected: List[Dict[str, Any]] = []
    done = threading.Event()
    failure: List[BaseException] = []

    def consume() -> None:
        try:
            for event in stream:
                collected.append(event)
                if len(collected) >= count:
                    break
        except Exception as exc:  # noqa: BLE001 —— 测试里不让异常逃出去
            # 记下来，交给主线程重抛 —— 直接吞掉的话，
            # 「流挂了」会伪装成「没收到事件」，报错都指不到地方。
            failure.append(exc)
        finally:
            done.set()

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    done.wait(timeout)
    if failure:
        raise failure[0]
    return list(collected)


# ---------------------------------------------------------------------------
# 基本实时性
# ---------------------------------------------------------------------------
class TestTailStream:
    """``tail_stream`` 的基本行为。"""

    def test_should_yield_events_as_they_are_written(self, tmp_path: Any) -> None:
        """后台写、主线程读 —— 事件必须在写入后立刻出现。"""
        log = tmp_path / "agent.jsonl"
        log.write_text("", encoding="utf-8")

        stream, stop = _open_stream(str(log))
        writer = _Writer(
            str(log),
            [_line("ls -la"), _thinking("看到了"), _line("cat README.md")],
            delay=0.05,
        ).start()

        events = _collect(stream, 3)
        stop.set()
        writer.wait()

        assert len(events) == 3
        assert events[0]["type"] == "tool_call"
        assert events[1]["type"] == "thinking"
        assert events[2]["tool"]["command"] == "cat README.md"

    def test_should_not_replay_existing_content_by_default(
        self, tmp_path: Any
    ) -> None:
        """默认只关心新增内容（``tail -f`` 语义）。"""
        log = tmp_path / "agent.jsonl"
        log.write_text(_line("old command") + "\n", encoding="utf-8")

        stream, stop = _open_stream(str(log))
        writer = _Writer(str(log), [_line("new command")], delay=0.05).start()

        events = _collect(stream, 1)
        stop.set()
        writer.wait()

        assert len(events) == 1
        assert events[0]["tool"]["command"] == "new command"

    def test_should_replay_history_when_from_start(self, tmp_path: Any) -> None:
        """``from_end=False`` 时先读完历史再跟随。"""
        log = tmp_path / "agent.jsonl"
        log.write_text(_line("old command") + "\n", encoding="utf-8")

        stream, stop = _open_stream(str(log), from_end=False)
        writer = _Writer(str(log), [_line("new command")], delay=0.05).start()

        events = _collect(stream, 2)
        stop.set()
        writer.wait()

        assert len(events) == 2
        assert events[0]["tool"]["command"] == "old command"
        assert events[1]["tool"]["command"] == "new command"

    def test_should_wait_for_file_to_appear(self, tmp_path: Any) -> None:
        """文件还不存在时要等，不能崩也不能直接结束。"""
        log = tmp_path / "late.jsonl"
        stream, stop = _open_stream(str(log))

        def create_later() -> None:
            time.sleep(0.3)
            log.write_text(_line("late command") + "\n", encoding="utf-8")

        thread = threading.Thread(target=create_later, daemon=True)
        thread.start()

        events = _collect(stream, 1)
        stop.set()
        thread.join(timeout=3)

        assert len(events) == 1
        assert events[0]["tool"]["command"] == "late command"

    def test_should_stop_on_stop_event(self, tmp_path: Any) -> None:
        """停止信号置位后要尽快退出。"""
        log = tmp_path / "agent.jsonl"
        log.write_text("", encoding="utf-8")
        stop = threading.Event()

        adapter = McodeAdapter()
        stream = adapter.tail_stream(
            str(log), interval=0.05, stop_event=stop
        )

        def stopper() -> None:
            time.sleep(0.3)
            stop.set()

        thread = threading.Thread(target=stopper, daemon=True)
        thread.start()

        collected = _collect(stream, 1, timeout=6.0)
        thread.join(timeout=3)

        assert collected == []
        assert stop.is_set()

    def test_should_skip_broken_lines(self, tmp_path: Any) -> None:
        """坏行要跳过，不能中断整条流。"""
        log = tmp_path / "agent.jsonl"
        log.write_text("", encoding="utf-8")

        stream, stop = _open_stream(str(log))
        writer = _Writer(
            str(log),
            ["{ 这不是合法 JSON", _line("good command")],
            delay=0.05,
        ).start()

        events = _collect(stream, 1)
        stop.set()
        writer.wait()

        assert len(events) == 1
        assert events[0]["tool"]["command"] == "good command"


# ---------------------------------------------------------------------------
# 轮转与截断
# ---------------------------------------------------------------------------
class TestRotation:
    """文件被换掉 / 被清空时的行为。"""

    def test_should_follow_rotated_file(self, tmp_path: Any) -> None:
        """同名换新文件后，要接着读新文件的内容。"""
        log = tmp_path / "agent.jsonl"
        log.write_text(_line("before rotation") + "\n", encoding="utf-8")

        stream, stop = _open_stream(str(log), from_end=False)

        def rotate() -> None:
            time.sleep(0.4)
            # 轮转：先写别的文件，再把原路径换掉
            rotated = tmp_path / "agent.jsonl.1"
            rotated.write_text(_line("rotated away") + "\n", encoding="utf-8")
            os.replace(str(rotated), str(log))
            time.sleep(0.2)
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(_line("after rotation") + "\n")
                handle.flush()

        thread = threading.Thread(target=rotate, daemon=True)
        thread.start()

        # 轮转后我们会从新文件的开头读起（tail -F 语义），
        # 所以一共会看到三条：轮转前 + 新文件原有内容 + 轮转后新增。
        events = _collect(stream, 3, timeout=12.0)
        stop.set()
        thread.join(timeout=5)

        commands = [
            event["tool"]["command"]
            for event in events
            if event.get("type") == "tool_call"
        ]
        assert "after rotation" in commands, (
            f"轮转后没有读到新文件内容，读到的是：{commands}"
        )

    def test_should_follow_truncated_file(self, tmp_path: Any) -> None:
        """文件被清空重写后，要接着读新内容。"""
        log = tmp_path / "agent.jsonl"
        log.write_text(_line("before truncate") + "\n", encoding="utf-8")

        stream, stop = _open_stream(str(log), from_end=False)

        def truncate() -> None:
            time.sleep(0.4)
            # 截断：清空重写
            with open(log, "w", encoding="utf-8") as handle:
                handle.write(_line("after truncate") + "\n")
                handle.flush()

        thread = threading.Thread(target=truncate, daemon=True)
        thread.start()

        events = _collect(stream, 2, timeout=12.0)
        stop.set()
        thread.join(timeout=5)

        commands = [
            event["tool"]["command"]
            for event in events
            if event.get("type") == "tool_call"
        ]
        assert "after truncate" in commands, (
            f"截断后没有读到新内容，读到的是：{commands}"
        )


# ---------------------------------------------------------------------------
# 半行
# ---------------------------------------------------------------------------
class TestPartialLine:
    """只写了一半的行。"""

    def test_should_not_emit_incomplete_line(self, tmp_path: Any) -> None:
        """半行不能被当成完整事件吐出去。"""
        log = tmp_path / "agent.jsonl"
        log.write_text("", encoding="utf-8")

        stream, stop = _open_stream(str(log))

        def write_half_then_finish() -> None:
            time.sleep(0.2)
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(_line("half line")[:20])
                handle.flush()
                time.sleep(0.4)
                handle.write(_line("half line")[20:] + "\n")
                handle.flush()

        thread = threading.Thread(
            target=write_half_then_finish, daemon=True
        )
        thread.start()

        events = _collect(stream, 1, timeout=12.0)
        stop.set()
        thread.join(timeout=5)

        assert len(events) == 1
        assert events[0]["tool"]["command"] == "half line"


# ---------------------------------------------------------------------------
# 只读保证
# ---------------------------------------------------------------------------
class TestReadOnly:
    """只读红线。"""

    def test_should_not_modify_the_watched_file(self, tmp_path: Any) -> None:
        """跟随过程绝不能改动被监控文件。"""
        log = tmp_path / "agent.jsonl"
        original = _line("read only") + "\n"
        log.write_text(original, encoding="utf-8")

        stream, stop = _open_stream(str(log), from_end=False)
        writer = _Writer(str(log), [_line("more")], delay=0.05).start()
        _collect(stream, 2)
        stop.set()
        writer.wait()

        content = log.read_text(encoding="utf-8")
        assert original in content


# ---------------------------------------------------------------------------
# 通用性
# ---------------------------------------------------------------------------
class TestGenericAdapter:
    """``tail_stream`` 是基类能力，不只 mcode 有。"""

    def test_should_work_with_a_custom_adapter(self, tmp_path: Any) -> None:
        """任何适配器都能用同一套跟随逻辑。"""

        class _Plain(BaseAdapter):
            """把每行原样当成一个事件。"""

            name = "plain"

            def parse_payload(self, payload):
                return []

            def parse_line(self, line):
                return [{"source": self.name, "type": "raw", "text": line}]

        log = tmp_path / "plain.log"
        log.write_text("", encoding="utf-8")

        stop = threading.Event()
        adapter = _Plain()
        stream = adapter.tail_stream(
            str(log), interval=0.05, stop_event=stop
        )

        writer = _Writer(str(log), ["第一行", "第二行"], delay=0.05).start()
        events = _collect(stream, 2)
        stop.set()
        writer.wait()

        assert [event["text"] for event in events] == ["第一行", "第二行"]
