"""正则热重载 —— 半夜调拦截策略，不用重启哨兵。

为什么需要它
============

v2.2.0 之前，L1 的高危命令正则**要么写死在代码里，要么改完 ``config.yaml``
就得重启哨兵**。半夜跑挂机的时候，你发现某条命令该拦却没拦，
唯一的选择是：起来 → 改配置 → 重启 → 祈祷重启后 Agent 还能接上。

这条命令链路太长了。这里把它压成：**改文件 → 保存 → 自动生效**。

⚠️ 为什么没用 ``watchdog``
==========================

任务书里写的是「利用 ``watchdog`` 库（项目初期曾引入过）」。
但翻遍 git 历史能确认的事实是：

* ``watchdog`` **从来没有**进过本项目的依赖清单；
* ``src/core/notifier.py`` 里那个 "watchdog" 是「合并摘要看门狗线程」的
  英文词，**不是**那个库；
* 当前环境里 **也没装** 这个包。

所以本模块走的是项目**一直以来的约定**：``src/tailer.py`` 那套
**mtime 轮询**。理由有三：

1. **零新增依赖** —— 不给分发版 exe 增加一个纯监听用的第三方包；
2. **与既有代码一致** —— ``src/tailer.py`` 已经这么干了，
   两套监听机制并存只会让人困惑；
3. **打包体积不增** —— 单文件 exe 是要发给别人用的，每 MB 都要算。

代价是**轮询有间隔**（默认 1 秒）。对「半夜调拦截策略」这个场景，
1 秒的延迟完全可以接受 —— 人从改完文件到看到反馈，本来就以秒计。

安全底线
========

1. **坏配置绝不让规则变空** —— YAML 解析失败时**保留旧规则**，
   只报错。宁可继续用旧策略，也不能让用户一个手滑就把防线清零。
2. **原子替换** —— 新规则引擎整体建好之后才换上去，
   审查线程永远不会看到「半成品」规则。
3. **绝不抛异常挡住启动** —— 审查侧异常一律降级（架构红线 R5）。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


#: 终端上打印的那句话（用户明确要求的原文）。
RELOAD_MESSAGE = "[SENTINEL] 检测到规则更新，热重载完成。"


def _file_stamp(path: str) -> Tuple[int, int]:
    """取文件的「版本戳」。

    用 ``(mtime_ns, size)`` 而不是单一 mtime ——
    有些文件系统的时间戳精度只有 1 秒，同一秒内改两次就发现不了；
    加上文件大小能覆盖绝大多数「改回原样」的情况。

    :param path: 文件路径。
    :return: ``(mtime_ns, size)``；文件不存在时返回 ``(0, 0)``。
    """
    try:
        stat = os.stat(path)
    except OSError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


class ConfigWatcher:
    """盯住一个文件的修改，后台轮询。

    :param path: 要盯的文件路径。
    :param on_change: 文件变了时的回调，签名 ``(path: str) -> None``。
    :param interval: 轮询间隔（秒）。
    :param settle: 变更后的**静默等待**（秒）——
        编辑器保存往往先清空再写入，等一下再回调，
        免得读到写了一半的文件。
    """

    def __init__(
        self,
        path: str,
        on_change: Callable[[str], None],
        *,
        interval: float = 1.0,
        settle: float = 0.3,
    ) -> None:
        self.path = os.path.abspath(str(path or ""))
        self.on_change = on_change
        self.interval = max(float(interval or 1.0), 0.05)
        self.settle = max(float(settle or 0.0), 0.0)
        self._stamp = _file_stamp(self.path)
        self._pending: Optional[Tuple[int, int]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        #: 变更次数（诊断用）。
        self.change_count = 0

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        """启动后台轮询线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sentinel-config-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止后台轮询线程。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # -- 内部实现 -------------------------------------------------------
    def _run(self) -> None:
        """线程主循环：异常只记日志，永不退出。"""
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 —— 监听失败不致命
                logger.warning(
                    "配置监听失败：%s: %s", type(exc).__name__, exc
                )
            self._stop.wait(self.interval)

    def poll_once(self) -> bool:
        """检查一次文件是否变了。

        ⚠️ 这里做了**去抖**：一次「保存」在文件系统上可能是**两次**变化
        （编辑器先清空再写入，或者写入时缓冲区还没落盘）。
        所以只有当版本戳**连续两轮都稳定**时才回调，
        否则用户改一次配置会看到两条提示。

        :return: 变了并已回调则返回 ``True``。
        """
        stamp = _file_stamp(self.path)
        if stamp == (0, 0):
            # 文件被删了 —— 不回调（用户可能正在重命名/替换它）。
            return False
        if stamp == self._stamp:
            return False
        if self._pending is not None and stamp == self._pending:
            # 上一轮见过这个版本，这一轮没再变 —— 稳定了，可以动手。
            self._stamp = stamp
            self._pending = None
            self._fire()
            return True
        # 变了 —— 先记下来，等它稳。
        self._pending = stamp
        if self.settle:
            time.sleep(self.settle)
            stable = _file_stamp(self.path)
            if stable != stamp:
                # 还在写 —— 下一轮再看。
                self._pending = stable
                return False
        self._stamp = stamp
        self._pending = None
        self._fire()
        return True

    def _fire(self) -> None:
        """触发回调（异常只记日志，绝不让监听线程死掉）。"""
        self.change_count += 1
        try:
            self.on_change(self.path)
        except Exception as exc:  # noqa: BLE001 —— 回调失败不影响监听
            logger.warning(
                "配置变更回调失败：%s: %s", type(exc).__name__, exc
            )


class RuleHotReloader:
    """规则热重载器：盯 ``config.yaml``，变了就换掉规则引擎。

    多路会话时**共用一份配置**，但每路各有自己的引擎 ——
    所以这里接受的是**引擎列表**：一次重载，全部换掉，
    终端上也只打印**一条**提示（不是 N 条）。

    :param config_path: 要盯的配置文件路径。
    :param engine: 哨兵引擎实例 —— 热替换的是它的 ``rule_engine`` 属性。
    :param engines: 多个引擎实例（多路会话时用这个）。
    :param rule_factory: 造规则引擎的工厂，默认用
        :class:`~src.rules.basic.RuleEngine`。
    :param notify: 终端提示回调，签名 ``(message: str) -> None``。
    :param event_sink: 事件落地回调，签名 ``(payload: dict) -> None`` ——
        用来把「热重载」写进时间线。
    :param interval: 轮询间隔（秒）。
    """

    def __init__(
        self,
        *,
        config_path: str,
        engine: Any = None,
        engines: Optional[Any] = None,
        rule_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
        notify: Optional[Callable[[str], None]] = None,
        event_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
        interval: float = 1.0,
    ) -> None:
        self.config_path = os.path.abspath(str(config_path or ""))
        self.engines: List[Any] = []
        if engines:
            self.engines.extend(
                item for item in list(engines) if item is not None
            )
        elif engine is not None:
            self.engines.append(engine)
        self.rule_factory = rule_factory or _default_rule_factory
        self.notify = notify or _default_notify
        self.event_sink = event_sink
        self.watcher = ConfigWatcher(
            self.config_path,
            self._on_change,
            interval=interval,
        )
        #: 最近一次热重载的结果（诊断用）。
        self.last_result: Dict[str, Any] = {}

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        """开始盯着配置文件。"""
        self.watcher.start()

    def stop(self, timeout: float = 2.0) -> None:
        """停止盯文件。"""
        self.watcher.stop(timeout=timeout)

    # -- 热重载 ---------------------------------------------------------
    def reload_now(self) -> Dict[str, Any]:
        """立刻重载一次规则（不等文件变化）。

        :return: ``{"ok": bool, "message": str, "rules": int}``。
        """
        try:
            config = _load_strict(self.config_path)
        except Exception as exc:  # noqa: BLE001 —— 坏配置只报错
            message = "配置读不出来（{}），已保留原规则。".format(
                _short_reason(exc)
            )
            # ⚠️ 这里用 info 而不是 warning ——
            # 用户看到的那句是 ``notify`` 打出来的；
            # 再用 warning 打一遍，用户会在终端看到**两条一模一样的报错**。
            logger.info("规则热重载失败：%s", message)
            self.last_result = {"ok": False, "message": message}
            self.notify(message)
            return self.last_result

        try:
            new_engine = self.rule_factory(config)
        except Exception as exc:  # noqa: BLE001 —— 规则编译失败只报错
            message = "规则编译失败（{}: {}），已保留原规则。".format(
                type(exc).__name__, exc
            )
            logger.info("规则热重载失败：%s", message)
            self.last_result = {"ok": False, "message": message}
            self.notify(message)
            return self.last_result

        count = _count_rules(new_engine)
        replaced = 0
        failures: List[str] = []
        for target in self.engines:
            # ⚠️ **原子替换**：先造好新的，再换上去。
            # 审查线程绝不会看到「半成品」规则。
            try:
                target.rule_engine = new_engine
                replaced += 1
            except Exception as exc:  # noqa: BLE001
                failures.append("{}: {}".format(type(exc).__name__, exc))
        if failures:
            message = "规则替换失败（{}），已保留原规则。".format(
                "；".join(failures)
            )
            logger.warning(message)
            self.last_result = {"ok": False, "message": message}
            self.notify(message)
            return self.last_result
        if not self.engines:
            message = "{}（生效规则 {} 条）".format(RELOAD_MESSAGE, count)
            self.last_result = {"ok": True, "message": message, "rules": count}
            logger.info(message)
            self.notify(RELOAD_MESSAGE)
            return self.last_result

        message = "{}（生效规则 {} 条，已更新 {} 路会话）".format(
            RELOAD_MESSAGE, count, replaced
        )
        self.last_result = {
            "ok": True,
            "message": message,
            "rules": count,
            "sessions": replaced,
        }
        logger.info(message)
        self.notify(RELOAD_MESSAGE)
        self._emit_event(count)
        return self.last_result

    def _on_change(self, path: str) -> None:
        """文件变了 —— 重载。

        :param path: 变化的文件路径。
        """
        logger.info("检测到配置变更：%s", path)
        self.reload_now()

    def _emit_event(self, rules: int) -> None:
        """把「热重载」写进时间线。

        :param rules: 生效的规则条数。
        """
        if self.event_sink is None:
            return
        payload = {
            "kind": "hot_reload",
            "title": "规则热重载完成",
            "detail": "生效规则 {} 条".format(rules),
            "severity": "info",
            "timestamp": time.time(),
            "extra": {"rules": rules, "config": self.config_path},
        }
        try:
            self.event_sink(payload)
        except Exception as exc:  # noqa: BLE001 —— 落地失败不影响重载
            logger.warning("写热重载事件失败：%s: %s", type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# 内部实现
# ---------------------------------------------------------------------------
def _default_rule_factory(config: Dict[str, Any]) -> Any:
    """默认的规则引擎工厂。

    :param config: 配置字典。
    :return: :class:`~src.rules.basic.RuleEngine` 实例。
    """
    from ..rules.basic import RuleEngine

    return RuleEngine(config)


def _default_notify(message: str) -> None:
    """默认的终端提示。

    :param message: 要打印的内容。
    """
    print(message, flush=True)


def _short_reason(exc: BaseException) -> str:
    """把异常压成**一行**人话。

    YAML 的报错信息天生是多行的（还带行列号和源码片段）——
    终端上一行说清就够，用户想细看可以自己打开文件。

    :param exc: 异常对象。
    :return: 一行说明。
    """
    text = str(exc).strip()
    if not text:
        return type(exc).__name__
    first = text.splitlines()[0].strip()
    if len(first) > 110:
        first = first[:107] + "…"
    return "{}：{}".format(type(exc).__name__, first)


def _load_strict(path: str) -> Dict[str, Any]:
    """**严格**地读配置 —— 读不出来就报错。

    为什么不用 :func:`src.config_loader.load_config`：
    它读不出来时返回空字典，而空字典会让规则引擎**一条规则都不剩** ——
    那等于把防线清零。这里宁可报错、保留旧规则。

    :param path: 配置文件路径。
    :return: 配置字典。
    :raises Exception: 文件不存在、YAML 非法、顶层不是字典时抛出。
    """
    import yaml

    if not path or not os.path.isfile(path):
        raise FileNotFoundError("配置文件不存在：{}".format(path))
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        raise ValueError("配置文件是空的")
    if not isinstance(data, dict):
        raise TypeError("配置文件的顶层不是字典")
    return data


def _count_rules(rule_engine: Any) -> int:
    """数一数这个规则引擎里有多少条**可配置**规则生效。

    数的只有两样：配置里的高危命令正则，和已启用的红线类别。
    红线检测器那套正则本身是**类常量**（写死在代码里），
    数进去会让人误以为改配置能改它们 —— 不能这么误导。

    :param rule_engine: 规则引擎实例。
    :return: 规则条数（数不出来时返回 0）。
    """
    total = 0
    dangerous = getattr(rule_engine, "_dangerous", None)
    if isinstance(dangerous, (list, tuple, set, dict)):
        total += len(dangerous)
    catastrophic = getattr(rule_engine, "_catastrophic", None)
    categories = getattr(catastrophic, "categories", None)
    if isinstance(categories, (list, tuple, set)):
        total += len(categories)
    return total
