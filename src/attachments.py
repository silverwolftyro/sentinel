"""挂载会话的持久化（v3.1.0）。

为什么要有它
============

v3.0.2 的挂载是**一次性**的：用户在雷达面板里点「接入」，
会话就被盯上了 —— 但哨兵一重启（崩溃、更新、机器重启），
所有挂载**全部消失**，用户得重新点一遍。

夜里挂机的场景里，这特别难受：早上起来一看，
昨晚盯的那几个任务全都不在了。

所以把「挂了哪些日志」写进一个**哨兵自己的**状态文件
（``config/attached_sessions.json``），
下次启动时自动把它们接回来。

⚠️ 这个文件写在哪
==================

写在**哨兵自己的** ``config/`` 目录里 ——
**绝不**写进用户被监控的项目目录（红线 R1：不碰用户的东西）。
文件名固定，内容也只是「路径 + 类型 + 武装状态」，没有任何敏感数据。

⚠️ 失效的挂载会被清理
======================

重启时如果某个日志：

* 文件已经不存在了，或者
* 已经**静默超过 1 小时**（mtime 太旧，说明那个 Agent 早就跑完了）

就把它从列表里删掉，并打印一行
``[SENTINEL] 清理失效的挂载历史: xxx``——
不清理的话，这个文件会随着时间越积越多，
而且会一直去盯早就跑完的历史任务。

本模块**不抛异常**：写不进去就读不到，全是降级路径
（审查侧异常一律降级，红线 R5）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

#: 持久化文件的名字（放在哨兵自己的 ``config/`` 目录里）。
ATTACHED_FILE_NAME: str = "attached_sessions.json"

#: 静默多久算「失效」（秒）—— 1 小时。
STALE_SECONDS: float = 3600.0

#: 持久化文件的格式版本（以后改结构时用来迁移）。
FORMAT_VERSION: int = 1

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "ATTACHED_FILE_NAME",
    "FORMAT_VERSION",
    "STALE_SECONDS",
    "AttachmentStore",
    "default_attached_path",
]


def default_attached_path(config_dir: Optional[str] = None) -> str:
    """持久化文件的默认位置。

    :param config_dir: 哨兵自己的配置目录；``None`` 时用仓库根下的
        ``config/``。
    :return: 绝对路径。
    """
    base = config_dir or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
    )
    return os.path.abspath(os.path.join(base, ATTACHED_FILE_NAME))


class AttachmentStore:
    """挂载会话的持久化仓库（v3.1.0）。

    ⚠️ 这个类**不抛异常**
    ===================

    读不到、写不进去、文件是坏 JSON —— 全部降级：

    * 读不到 → 当作「没有挂载过」，照常启动；
    * 写不进去 → 记一条 warning，挂载本身**照样成功**
      （用户最关心的「现在正在盯着」不能因为存不下来就失效）。

    :param path: 持久化文件路径。
    """

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path or ""))

    # -- 读 ---------------------------------------------------------------
    def load(self) -> List[Dict[str, Any]]:
        """读出持久化的挂载列表。

        :return: 挂载条目列表（读不到就是空列表）。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            _LOGGER.warning(
                "读取挂载历史失败（%s）：%s: %s", self.path, type(exc).__name__, exc
            )
            return []

        if not isinstance(payload, dict):
            return []
        raw = payload.get("attachments")
        if not isinstance(raw, list):
            return []

        entries: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            entries.append(
                {
                    "path": os.path.abspath(path),
                    "agent": str(item.get("agent") or ""),
                    "cwd": str(item.get("cwd") or ""),
                    "armed": bool(item.get("armed")),
                    "attached_at": float(item.get("attached_at") or 0.0),
                    "session_id": str(item.get("session_id") or ""),
                }
            )
        return entries

    # -- 写 ---------------------------------------------------------------
    def save_entry(self, entry: Dict[str, Any]) -> bool:
        """把一个挂载**并入**持久化列表。

        同一个路径只留一条（后写的覆盖先写的）。

        :param entry: 挂载条目（至少要有 ``path``）。
        :return: 存进去了就返回 ``True``。
        """
        path = str((entry or {}).get("path") or "").strip()
        if not path:
            return False

        merged = dict(entry)
        merged["path"] = os.path.abspath(path)
        merged.setdefault("attached_at", time.time())

        entries = self.load()
        replaced = False
        for index, item in enumerate(entries):
            if os.path.normcase(item.get("path", "")) == os.path.normcase(
                merged["path"]
            ):
                entries[index] = merged
                replaced = True
                break
        if not replaced:
            entries.append(merged)
        return self._write(entries)

    def update(self, path: str, **changes: Any) -> bool:
        """改一个已持久化挂载的某些字段。

        :param path: 日志路径。
        :param changes: 要改的字段（如 ``armed=True``）。
        :return: 改到了并写回去了就返回 ``True``。
        """
        target = os.path.normcase(os.path.abspath(str(path or "")))
        entries = self.load()
        touched = False
        for item in entries:
            if os.path.normcase(item.get("path", "")) == target:
                item.update(changes)
                touched = True
                break
        if not touched:
            return False
        return self._write(entries)

    def remove(self, path: str) -> bool:
        """把一个挂载从持久化列表里删掉。

        :param path: 日志路径。
        :return: 列表变了就返回 ``True``。
        """
        target = os.path.normcase(os.path.abspath(str(path or "")))
        entries = self.load()
        kept = [
            item
            for item in entries
            if os.path.normcase(item.get("path", "")) != target
        ]
        if len(kept) == len(entries):
            return False
        return self._write(kept)

    def prune(
        self,
        *,
        now: Optional[float] = None,
        stale_seconds: float = STALE_SECONDS,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """清掉**失效**的挂载历史。

        失效的判定：

        * 文件已经不存在；
        * 文件**静默超过** ``stale_seconds``（默认 1 小时）——
          那个 Agent 早就跑完了，盯着它没有意义。

        :param now: 「现在」的 epoch 秒。
        :param stale_seconds: 静默多久算失效。
        :return: ``(留下的, 清掉的)`` 两个列表。
        """
        current = time.time() if now is None else float(now)
        limit = float(stale_seconds)
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []

        for entry in self.load():
            if self._is_stale(entry, current, limit):
                dropped.append(entry)
            else:
                kept.append(entry)

        if dropped:
            self._write(kept)
        return kept, dropped

    def _is_stale(
        self,
        entry: Dict[str, Any],
        now: float,
        stale_seconds: float,
    ) -> bool:
        """判断一个挂载是不是已经失效。

        :param entry: 挂载条目。
        :param now: 「现在」的 epoch 秒。
        :param stale_seconds: 静默多久算失效。
        :return: 失效就返回 ``True``。
        """
        path = str(entry.get("path") or "")
        try:
            stat_result = os.stat(path)
        except (OSError, ValueError):
            # 文件没了 —— 失效。
            return True
        try:
            age = max(0.0, now - float(stat_result.st_mtime))
        except (TypeError, ValueError):
            return True
        return age > stale_seconds

    def _write(self, entries: List[Dict[str, Any]]) -> bool:
        """把挂载列表写回磁盘（**先写临时文件再替换**）。

        ⚠️ 直接覆盖写的话，中途断电会留下半个 JSON ——
        那样下次启动就读不到任何挂载了。
        先写同目录的临时文件、再原子替换，就不会出现半个文件。

        :param entries: 挂载条目列表。
        :return: 写成功了就返回 ``True``。
        """
        payload = {
            "version": FORMAT_VERSION,
            "saved_at": time.time(),
            "attachments": list(entries),
        }
        folder = os.path.dirname(self.path)
        temp_path = ""
        try:
            if folder:
                os.makedirs(folder, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=folder or ".",
                prefix=ATTACHED_FILE_NAME + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = handle.name
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError as exc:
                    # 某些文件系统不支持 fsync —— 不影响正确性，
                    # 但要说一声，别让人以为「刷盘了」。
                    _LOGGER.debug("fsync 失败（不算错）：%s", exc)
            os.replace(temp_path, self.path)
            temp_path = ""
            return True
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.warning(
                "写挂载历史失败（%s）：%s: %s",
                self.path,
                type(exc).__name__,
                exc,
            )
            return False
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError as exc:
                    # 临时文件删不掉只是留了个垃圾文件，不影响功能。
                    _LOGGER.debug("删临时文件失败：%s", exc)
