"""文件增量跟随器 —— 叶子模块，不依赖 ``src`` 下任何其他模块。

为什么单独抽出来
================
「只读地跟着一个正在被写的文件走」这件事，有**两处**需要它：

* :mod:`src.adapters.base` 的 ``tail_stream()`` —— 跟随被监控 Agent 的日志；
* :mod:`src.gui.server` 的看板 —— 跟随哨兵自己的告警日志。

这两处的**难点完全相同**：文件轮转、原地截断、半行等待补齐。
在两个地方各写一遍，几十行微妙逻辑迟早会分叉 —— 所以抽到这里，
两边共用同一份实现。

它是一个**叶子模块**（与 :mod:`src.config_loader` 同类），
因此**不产生层级倒置**：采集层（①）与界面层（④）都可以放心 import 它。

只读保证
========
本模块**只以 ``"rb"`` 只读模式**打开文件，从不写入、截断、删除
（架构红线 R1：哨兵只读监听被监控的文件）。

轮转与截断
==========
日志文件在真实环境里会被轮转（``logrotate``、``copytruncate``），
或者被程序直接清空重写。两种情况都必须识别，否则会：

* **轮转**（同名换新文件）—— 旧偏移指向新文件中间，新文件开头被跳过；
* **截断**（文件被清空）—— 旧偏移超出新长度，尾部残留被当成新内容。

识别手段是**文件身份**（Windows 上是 NTFS 的文件索引 ``st_ino``）与
**文件大小**：身份变了就是轮转，大小小于偏移就是截断。
两者都会把偏移重置为 0 —— 与 ``tail -F`` 的行为一致。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterator, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["FileTailer", "follow_lines"]


class FileTailer:
    """单文件增量跟随器（只读）。

    只读取**新增字节**，并记住文件身份（Windows 上是 NTFS 文件索引，
    即 ``os.stat().st_ino``）。两种异常情况会被自动识别：

    * **轮转** —— 同一个路径换成了另一个文件（``st_ino`` 变了）；
    * **原地截断** —— 文件被截短（``size < offset``）。

    两者都会把偏移重置为 0，避免「新文件的前半段被跳过」或
    「旧文件尾部残留」这类典型截尾错误。

    内存不会无限增长
    ===============

    ⚠️ v3.2.0 加的**硬上限**：末尾那半行（还没等到 ``\\n`` 的部分）
    缓存在 ``_buffer`` 里。如果被跟随的文件写出**一条超长行**
    （比如一整段 base64、一整个 minified JS），
    缓冲区就会**一直涨** —— 挂一夜 = 吃光内存。

    所以缓冲超过 ``max_buffer``（默认 1 MiB）就**丢掉最老的部分**，
    并记一条 warning（只在**首次**超限时记，避免刷屏）。
    丢的是「一行超长的残片」，不影响其余行的解析。

    :param path: 要跟随的文件路径。
    :param encoding: 文本解码用的编码。
    :param chunk_limit: 单次读取的字节上限（防止一次性吃掉大文件）。
    :param max_buffer: 末尾半行缓冲的**字节上限**（超过就丢最老的）。
    """

    def __init__(
        self,
        path: str,
        *,
        encoding: str = "utf-8",
        chunk_limit: int = 4 * 1024 * 1024,
        max_buffer: int = 1024 * 1024,
    ) -> None:
        self.path = os.path.abspath(path)
        self.encoding = encoding
        self.chunk_limit = max(int(chunk_limit), 1)
        #: 末尾半行缓冲的上限（字节）。
        self.max_buffer = max(int(max_buffer), 1024)
        #: 已消费的字节数。
        self.offset = 0
        #: 文件身份标识；``None`` 表示还不知道。
        self.identity: Optional[int] = None
        #: 末尾不完整的一行，等后续写入补齐。
        self._buffer = ""
        #: 是否已经因为「超长行」丢过内容（只记一次日志）。
        self._buffer_truncated = False

    # -- 对外接口 -------------------------------------------------------
    def poll(self) -> List[str]:
        """读取自上次调用以来新增的**完整行**。

        :return: 新增的行；没有新内容时返回空列表。
        """
        return self._consume(self.read_new_bytes())

    def prime(self, max_bytes: int = 0) -> List[str]:
        """加载历史内容（最多 ``max_bytes`` 字节）并把偏移推到末尾。

        文件末尾若是不完整的半行，会留在缓冲区里等后续写入补齐，
        避免「半行」与「补齐后的整行」被当成两条记录。

        :param max_bytes: 只读文件末尾这么多字节；``0`` 表示整个文件。
        :return: 读到的完整行。
        """
        if max_bytes > 0:
            data = self.read_tail_bytes(max_bytes)
        else:
            data = self.read_all_bytes()
        return self._consume(data)

    def skip_to_end(self) -> None:
        """把偏移直接推到文件末尾，**不产出任何行**。

        这是 ``tail -f`` 的语义：只关心「从现在起」新增的内容。
        """
        try:
            stat_result = os.stat(self.path)
        except OSError:
            return
        self.identity = self._identity_of(stat_result)
        self.offset = int(stat_result.st_size)
        self._buffer = ""

    def read_new_bytes(self) -> bytes:
        """读取自上次读取以来的新增字节，并推进偏移。

        :return: 新增字节；没有新内容时返回 ``b""``。
        """
        try:
            stat_result = os.stat(self.path)
        except OSError as exc:
            logger.debug("无法 stat %s：%s", self.path, exc)
            return b""

        identity = self._identity_of(stat_result)
        if identity is not None:
            if self.identity is not None and identity != self.identity:
                logger.info(
                    "检测到文件轮转（同名新文件），重置偏移：%s", self.path
                )
                self._reset()
            # ⚠️ 必须**无条件**更新身份标识。
            # 若只在「身份未知」时赋值，轮转之后 self.identity 仍是旧值，
            # 于是**每一次**轮询都会被误判成「又轮转了」，
            # 把整个文件反复重读 —— 表现为告警无限重复。
            # （这个 Bug 是 tests/test_tail_stream.py 的轮转用例抓出来的。）
            self.identity = identity

        size = int(stat_result.st_size)
        if size < self.offset:
            logger.info("检测到文件被截断，重置偏移：%s", self.path)
            self._reset()
        if size == self.offset:
            return b""

        try:
            with open(self.path, "rb") as handle:
                handle.seek(self.offset)
                data = handle.read(min(size - self.offset, self.chunk_limit))
        except OSError as exc:
            logger.warning("读取 %s 失败：%s", self.path, exc)
            return b""
        self.offset += len(data)
        return data

    def read_all_bytes(self) -> bytes:
        """从头读取整个文件，并把偏移推到末尾。"""
        return self._read_from(0)

    def read_tail_bytes(self, max_bytes: int) -> bytes:
        """读取文件末尾最多 ``max_bytes`` 字节，并把偏移推到末尾。"""
        limit = max(int(max_bytes), 0)
        try:
            size = int(os.stat(self.path).st_size)
        except OSError as exc:
            logger.debug("无法 stat %s：%s", self.path, exc)
            return b""
        return self._read_from(max(0, size - limit))

    # -- 内部实现 -------------------------------------------------------
    @staticmethod
    def _identity_of(stat_result: os.stat_result) -> Optional[int]:
        """取文件身份标识。

        Windows 上 ``st_ino`` 就是 NTFS 的文件索引（FileIndex），
        「同名新文件」会得到不同的值 —— 正是轮转检测需要的信号。

        :param stat_result: ``os.stat()`` 的结果。
        :return: 身份标识；取不到时返回 ``None``。
        """
        ino = int(getattr(stat_result, "st_ino", 0) or 0)
        return ino or None

    def _reset(self) -> None:
        """把偏移与半行缓冲复位（轮转 / 截断时调用）。"""
        self.offset = 0
        self._buffer = ""

    def _read_from(self, start: int) -> bytes:
        """从指定偏移读到文件末尾，并把偏移推到末尾。"""
        try:
            stat_result = os.stat(self.path)
            identity = self._identity_of(stat_result)
            if self.identity is None:
                self.identity = identity
            with open(self.path, "rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError as exc:
            logger.warning("读取 %s 失败：%s", self.path, exc)
            return b""
        self.offset = start + len(data)
        return data

    def _consume(self, data: bytes) -> List[str]:
        """把新增字节切成完整行，末尾半行留在缓冲区里。

        ⚠️ 缓冲区有**硬上限**（``max_buffer``）——
        被跟随的文件写出超长行时，缓冲区不会无限增长。
        """
        if not data:
            return []
        self._buffer += data.decode(self.encoding, errors="replace")
        self._trim_buffer()
        parts = self._buffer.split("\n")
        self._buffer = parts.pop()
        lines: List[str] = []
        for raw in parts:
            line = raw.rstrip("\r").strip()
            if line:
                lines.append(line)
        return lines

    def _trim_buffer(self) -> None:
        """把末尾半行缓冲**裁到上限**以内。

        丢的是「一行超长的残片」的最老部分 ——
        它本来就不是一条完整的记录，留着也没有意义。
        """
        overflow = len(self._buffer) - self.max_buffer
        if overflow <= 0:
            return
        self._buffer = self._buffer[overflow:]
        if not self._buffer_truncated:
            self._buffer_truncated = True
            logger.warning(
                "被跟随的文件写出了超长行（超过 %d 字节还没有换行），"
                "已丢弃最早的残片以控制内存：%s",
                self.max_buffer,
                self.path,
            )


def follow_lines(
    path: str,
    *,
    interval: float = 0.5,
    from_end: bool = True,
    encoding: str = "utf-8",
) -> Iterator[str]:
    """无限跟随一个文件，逐行产出**新增**的行。

    这是 ``tail -f`` 的语义实现。文件被轮转或截断时自动重置偏移，
    不会崩、不会漏掉新文件的开头。

    :param path: 要跟随的文件路径。
    :param interval: 轮询间隔（秒）。
    :param from_end: 为 ``True`` 时从文件末尾开始（只关心新增内容）；
        为 ``False`` 时先读完历史内容再跟随。
    :param encoding: 文本解码用的编码。
    :yield: 新增的完整行。
    """
    tailer = FileTailer(path, encoding=encoding)
    if from_end:
        tailer.skip_to_end()
    else:
        for line in tailer.prime():
            yield line
    while True:
        lines = tailer.poll()
        for line in lines:
            yield line
        if not lines:
            time.sleep(max(float(interval), 0.01))
