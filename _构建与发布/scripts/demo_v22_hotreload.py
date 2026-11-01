"""v2.2.0 规则热重载演示 —— 改 config.yaml，不用重启。

演示三件事：

1. **改配置 → 规则自动换掉**（终端打印那句提示）；
2. **新规则真的生效**（新加的高危命令被拦下来了）；
3. **配置写坏了 → 保留旧规则**（防线不能一次手滑就清零）。

用法::

    python _构建与发布/scripts/demo_v22_hotreload.py
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)))

from src.core.hotreload import RuleHotReloader  # noqa: E402
from src.rules.basic import RuleEngine  # noqa: E402


def _say(text: str) -> None:
    """打印一条演示消息。

    ⚠️ 先把 stdout 切到 UTF-8 —— Windows 控制台默认 GBK，
    报告里那个 ``⚠️`` 直接 print 会 ``UnicodeEncodeError``。
    """
    _prepare_stdout()
    print(text, flush=True)


def _prepare_stdout() -> None:
    """让 stdout 用 UTF-8 输出（只在重定向 / 管道时需要）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(
                encoding="utf-8", errors="replace", line_buffering=True
            )
        except (ValueError, OSError, AttributeError):
            pass


def _write_config(path: str, patterns) -> None:
    """写一份只含高危命令正则的配置。"""
    lines = [
        "rules:",
        "  enabled: true",
        "  dangerous_commands_replace: true",
        "  dangerous_commands:",
    ]
    lines.extend("    - '{}'".format(item) for item in patterns)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def _check(engine, command: str) -> str:
    """问一句「这条命令会不会被拦」。"""
    verdict = engine.rule_engine.check_event(
        {
            "type": "tool_call",
            "tool": {"name": "Bash", "command": command, "arguments": {}},
            "source": "demo",
        }
    )
    return str(verdict.get("verdict") or "ON_TRACK")


def main(argv=None) -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        prog="demo_v22_hotreload",
        description="v2.2.0 规则热重载演示",
    )
    parser.add_argument(
        "--workdir",
        default=os.path.join("logs", "v22_hotreload"),
        help="演示目录",
    )
    args = parser.parse_args(argv)

    workdir = os.path.abspath(args.workdir)
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    config_path = os.path.join(workdir, "config.yaml")
    _write_config(config_path, [r"旧命令"])

    class _Engine:
        """假引擎 —— 只提供一个可替换的 ``rule_engine`` 属性。"""

        def __init__(self) -> None:
            self.rule_engine = RuleEngine(
                {"rules": {"dangerous_commands_replace": True,
                           "dangerous_commands": [r"旧命令"]}}
            )
            self.notifier = "原来的通知器（不会被重建）"

    engine = _Engine()
    messages = []
    events = []
    reloader = RuleHotReloader(
        config_path=config_path,
        engine=engine,
        interval=0.5,
        notify=lambda message: (messages.append(message), _say(message)),
        event_sink=events.append,
    )

    _say("=" * 62)
    _say("  哨兵规则热重载演示")
    _say("=" * 62)
    _say("盯的文件：{}".format(config_path))
    _say("当前规则：旧命令")
    _say("「旧命令」现在会拦吗？ → {}".format(_check(engine, "跑一下 旧命令 啊")))
    reloader.start()
    _say("热重载已开启。下面改配置 —— **不重启任何东西**。")
    _say("-" * 62)

    # --- 第 1 步：改配置，新规则立刻生效 ------------------------------
    _say("【第 1 步】把高危命令从「旧命令」改成「新命令」……")
    time.sleep(1.0)
    _write_config(config_path, [r"新命令"])
    deadline = time.time() + 10.0
    while time.time() < deadline and not messages:
        time.sleep(0.1)
    _say("  → 「新命令」现在会拦吗？ → {}".format(_check(engine, "跑一下 新命令 啊")))
    _say("  → 「旧命令」还拦吗？   → {}".format(_check(engine, "跑一下 旧命令 啊")))
    _say("  → 通知器有没有被重建？ → {}".format(engine.notifier))

    # --- 第 2 步：写坏配置，旧规则必须留着 ----------------------------
    _say("-" * 62)
    _say("【第 2 步】把配置写坏（模拟手滑）……")
    with io.open(config_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("rules: [这不是合法的 YAML\n")
    deadline = time.time() + 10.0
    while time.time() < deadline and len(messages) < 2:
        time.sleep(0.1)
    _say("  → 「新命令」还拦吗？ → {}".format(_check(engine, "跑一下 新命令 啊")))
    _say("  → 防线有没有被清零？ → 没有（上面还是 DANGEROUS 就说明还在）")

    reloader.stop()
    _say("=" * 62)
    _say("时间线里记下的热重载事件：{} 条".format(len(events)))
    for item in events:
        _say("  · {} —— {}".format(item.get("kind"), item.get("title")))
    _say("终端提示（原文）：")
    for message in messages:
        _say("  {}".format(message))
    _say("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
