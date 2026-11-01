"""Agent 子进程的**派发与加固**。

这个模块把派发一个 Agent CLI 时的四个「不报错、只在生产里挂死」
的坑集中处理掉，移植自开源项目 ``agent_bridge`` 的
``bridge/runner.py``。

四个坑
======

1. **stdin 必须 DEVNULL**
   —— 否则 Agent CLI 会继承宿主进程的 stdin（哨兵跑在嵌入式 Python
   上时那是 MCP 的 JSON-RPC 流），两边抢同一个管道，输出全是乱码。

2. **超时必须连进程树一起清**
   —— 只杀父进程会留下孤儿进程占着文件与端口；
   Windows 上要 ``taskkill /F /T /PID``（``/T`` = 连子孙）。

3. **输出管道必须持续读取，但缓冲要限幅**
   —— 没人读管道，管道写满，子进程就**永久阻塞**；
   但反过来无限收集输出又会把宿主内存吃光。
   做法是「后台线程一直读 + 固定宽限期的尾部缓冲」。

4. **不能靠退出码判断成败**
   —— 未登录的 CLI 退出码常常是 0，必须另做措辞识别，
   见 :mod:`src.agents.auth`。

来源
====

``agent_bridge`` 的 ``bridge/runner.py``（``_execute_blocking`` /
``_PipeReader`` / ``_ENV_STRIP``）。哨兵原有的接管链路
（``_构建与发布/scripts/run_with_sentinel.py``）已经踩过其中两个坑，
本模块把剩下的补齐，并让两者共用同一套实现。
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from src.agents.auth import detect_auth_failure, detect_runtime_failure

logger = logging.getLogger(__name__)

__all__ = [
    "ENV_STRIP",
    "STREAM_GRACE",
    "BoundedBuffer",
    "LaunchOutcome",
    "kill_process_tree",
    "run_agent_process",
    "sanitize_env",
    "spawn_agent",
]

#: 派发前要**剔除**的环境变量。
#:
#: ⚠️ 哨兵打包成 exe 后跑在自带的嵌入式 Python 上，
#: ``PYTHONHOME`` / ``PYTHONPATH`` 这类变量一旦传给 Agent 自己的
#: Python 解释器（venv、或用户在 Agent 里起的脚本），对方会以
#: ``Failed to import encodings module`` 直接失败。
ENV_STRIP: Tuple[str, ...] = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONUSERBASE",
    "PYTHONLEGACYWINDOWSSTDIO",
)

#: 子进程退出后，等待最后一段输出落完的宽限期（秒）。
STREAM_GRACE = 5.0

#: 输出缓冲上限（字符）。超出的部分从**头部**丢弃。
DEFAULT_OUTPUT_LIMIT = 256 * 1024

_IS_WINDOWS = sys.platform == "win32"


class BoundedBuffer:
    """限幅的文本缓冲 —— 只保留尾部 ``limit`` 个字符。

    为什么是「留尾部」：Agent 的输出里最有价值的是**最后**那几句
    （结论、报错摘要），前面的启动噪声可以丢。

    :param limit: 字符上限。
    """

    def __init__(self, limit: int = DEFAULT_OUTPUT_LIMIT) -> None:
        self.limit = max(0, int(limit))
        self._chunks: List[str] = []
        self._size = 0
        self._dropped = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        """追加一段文本。

        超过上限时从**头部**丢弃 —— Agent 输出里最有价值的是最后几句。

        :param text: 待追加的文本。
        """
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size > self.limit:
                if len(self._chunks) > 1:
                    head = self._chunks.pop(0)
                    self._size -= len(head)
                    self._dropped += len(head)
                    continue
                # 只剩一个块 —— 从它的头部裁。
                only = self._chunks[0]
                if len(only) <= self.limit:
                    break
                cut = len(only) - self.limit
                self._chunks[0] = only[cut:]
                self._size -= cut
                self._dropped += cut

    @property
    def truncated(self) -> bool:
        """是否因为超限丢过内容。"""
        return self._dropped > 0

    @property
    def dropped(self) -> int:
        """被丢弃的字符数。"""
        return self._dropped

    def value(self) -> str:
        """取当前内容。

        :return: 尾部保留的文本。
        """
        with self._lock:
            return "".join(self._chunks)


@dataclass
class LaunchOutcome:
    """一次派发的结果。

    :param argv: 实际执行的命令行。
    :param exit_code: 退出码；超时被杀时是 ``None``。
    :param stdout: 采集到的标准输出（可能已限幅）。
    :param stderr: 采集到的标准错误（可能已限幅）。
    :param timed_out: 是否超时。
    :param needs_login: 是否被判定为「未登录」。
    :param runtime_error: 是否被判定为「运行期依赖没就绪」。
    :param duration_ms: 耗时（毫秒）。
    :param stdout_truncated: 标准输出是否被限幅丢弃过。
    :param stderr_truncated: 标准错误是否被限幅丢弃过。
    """

    argv: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    needs_login: bool = False
    runtime_error: bool = False
    duration_ms: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def ok(self) -> bool:
        """是否算「成功跑完」。

        ⚠️ 未登录与运行期失败**都不算成功** ——
        退出码 0 不代表任务做完了。
        """
        if self.timed_out or self.needs_login or self.runtime_error:
            return False
        return self.exit_code == 0

    @property
    def summary(self) -> str:
        """一句话摘要（给日志用）。"""
        if self.timed_out:
            return "超时"
        if self.needs_login:
            return "未登录"
        if self.runtime_error:
            return "运行期失败"
        return "退出码 {}".format(self.exit_code)


def sanitize_env(
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """清洗要传给子进程的环境变量。

    :param env: 基础环境；``None`` 表示用当前进程的环境。
    :return: 清洗后的环境字典。
    """
    source = dict(os.environ if env is None else env)
    for name in ENV_STRIP:
        source.pop(name, None)
    return source


def spawn_agent(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.Popen:
    """拉起一个 Agent 进程。

    ⚠️ 三个关键设定：

    * ``stdin=DEVNULL`` —— 见模块文档的坑 1；
    * ``bufsize=0`` —— 不缓冲，读取线程拿到多少是多少；
    * POSIX 下 ``start_new_session=True`` —— 让
      :func:`kill_process_tree` 能整组杀掉。

    :param argv: 命令行。
    :param cwd: 工作目录。
    :param env: 环境变量；``None`` 表示用清洗后的当前环境。
    :return: 已启动的进程对象。
    :raises ValueError: 命令行为空。
    """
    if not argv:
        raise ValueError("命令行不能为空")

    kwargs: Dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
        "cwd": cwd,
        "env": sanitize_env(env),
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NO_WINDOW", 0
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(argv), **kwargs)


def kill_process_tree(proc: subprocess.Popen) -> None:
    """连带子进程树一起结束一个进程。

    ⚠️ 只杀父进程会留下孤儿 —— 它们照样占着文件句柄与端口。

    :param proc: 目标进程。
    """
    if proc.poll() is not None:
        return
    if _IS_WINDOWS:
        try:
            subprocess.run(
                [
                    "taskkill",
                    "/F",
                    "/T",
                    "/PID",
                    str(proc.pid),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(
                    subprocess, "CREATE_NO_WINDOW", 0
                ),
            )
        except OSError as exc:
            logger.warning("taskkill 失败（%s），退回 terminate", exc)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (OSError, ProcessLookupError) as exc:
            logger.warning("killpg 失败（%s），退回 kill", exc)

    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        logger.warning(
            "子进程 %s 收尾超时（5 秒），改用 kill 强杀", proc.pid
        )

    try:
        proc.kill()
    except OSError as exc:
        logger.warning("强杀子进程 %s 失败：%s", proc.pid, exc)


def _drain(
    stream: Optional[object],
    buffer: BoundedBuffer,
    stop: threading.Event,
) -> None:
    """把管道读干净（**必须一直读**）。

    :param stream: 二进制流对象。
    :param buffer: 收集到的缓冲。
    :param stop: 停止标志。
    """
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(65536)  # type: ignore[attr-defined]
            if not chunk:
                break
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", errors="replace")
            buffer.append(chunk)
            if stop.is_set():
                break
    except (OSError, ValueError) as exc:
        logger.debug("读取子进程输出结束：%s", exc)
    finally:
        try:
            stream.close()  # type: ignore[attr-defined]
        except (OSError, ValueError) as exc:
            logger.debug("关闭子进程输出流失败：%s", exc)


def run_agent_process(
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    timeout: float = 600.0,
    env: Optional[Dict[str, str]] = None,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    auth_patterns: Sequence[str] = (),
    fail_patterns: Sequence[str] = (),
) -> LaunchOutcome:
    """跑完一个 Agent 进程并收集结果。

    这是「收集-返回」形态的派发（上游 ``bridge/runner.py`` 的形态）。
    哨兵的**流式**接管走的是另一条路（``run_with_sentinel.py``），
    但四条加固经验两边共用。

    :param argv: 命令行。
    :param cwd: 工作目录。
    :param timeout: 超时秒数。
    :param env: 环境变量；``None`` 表示用清洗后的当前环境。
    :param output_limit: 输出缓冲上限（字符）。
    :param auth_patterns: 该 Agent 专属的鉴权失败措辞。
    :param fail_patterns: 该 Agent 专属的运行期失败措辞。
    :return: 派发结果。
    """
    started = time.monotonic()
    outcome = LaunchOutcome(argv=list(argv))

    try:
        proc = spawn_agent(argv, cwd=cwd, env=env)
    except (OSError, ValueError) as exc:
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        outcome.stderr = "拉起失败：{}".format(exc)
        return outcome

    out_buf = BoundedBuffer(output_limit)
    err_buf = BoundedBuffer(output_limit)
    stop = threading.Event()

    readers = [
        threading.Thread(
            target=_drain,
            args=(proc.stdout, out_buf, stop),
            name="agent-stdout",
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(proc.stderr, err_buf, stop),
            name="agent-stderr",
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    try:
        outcome.exit_code = proc.wait(timeout=max(1.0, float(timeout)))
    except subprocess.TimeoutExpired:
        outcome.timed_out = True
        kill_process_tree(proc)
        try:
            outcome.exit_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            outcome.exit_code = None
    finally:
        stop.set()
        # 子进程退出后，孙进程可能还握着管道写端 ——
        # 给一个宽限期让正常输出落完，到点就放弃等待。
        for reader in readers:
            reader.join(timeout=STREAM_GRACE)

    outcome.stdout = out_buf.value()
    outcome.stderr = err_buf.value()
    outcome.stdout_truncated = out_buf.truncated
    outcome.stderr_truncated = err_buf.truncated
    outcome.duration_ms = int((time.monotonic() - started) * 1000)

    auth_hit = detect_auth_failure(
        outcome.stdout, outcome.stderr, auth_patterns
    )
    if auth_hit:
        outcome.needs_login = True
        outcome.stderr = (auth_hit + "\n" + outcome.stderr).strip()
    else:
        fail_hit = detect_runtime_failure(
            outcome.stdout, outcome.stderr, fail_patterns
        )
        if fail_hit:
            outcome.runtime_error = True
            outcome.stderr = (fail_hit + "\n" + outcome.stderr).strip()

    return outcome
