"""把 Night Crawl 收到的**晨报报文**渲染成一张截图（v3.2.0）。

为什么要有这个脚本
==================

验收要「晨报推送到手机/Webhook 的报文截图」——
但 Mock Webhook 收到的是一段 **JSON**，直接贴进聊天框没人看得出
「手机上到底长什么样」。

这个脚本做两件事：

1. 从 Night Crawl 的 JSON 报告里取出**原文收到**的晨报报文；
2. 渲染成一个「手机消息气泡 + 原始 JSON」的页面并截图。

⚠️ **诚实第一**：气泡里的文字**一字不改**地来自真实收到的报文；
原始 JSON 也**原样**列在下面（可核对）。
页面不会替哨兵美颜任何数字。

用法::

    python _构建与发布/scripts/capture_briefing_push.py \\
        --report "<nightcrawl.json>" --out "<绝对路径>.png"
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional

#: 仓库根目录。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))


def _prepare_stdout() -> None:
    """控制台可能是 GBK —— 先切 UTF-8。"""
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


def _load_report(path: str) -> Dict[str, Any]:
    """读 Night Crawl 的 JSON 报告。

    :param path: 报告路径。
    :return: 报告字典。
    """
    with io.open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def pick_briefing(report: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从报告里挑出**晨报**报文。

    :param report: 报告字典。
    :return: 晨报报文（``{"body": …}``）；没有就返回 ``None``。
    """
    payloads = report.get("webhook_payloads") or []
    for item in payloads:
        body = item.get("body") or {}
        if str(body.get("type") or "") == "briefing":
            return item
    return None


def build_html(item: Dict[str, Any], report: Dict[str, Any]) -> str:
    """渲染成一段「手机消息 + 原始 JSON」的 HTML。

    :param item: 晨报报文。
    :param report: 整份报告（用来展示统计）。
    :return: HTML 字符串。
    """
    body = item.get("body") or {}
    stats = body.get("stats") or {}
    title = str(body.get("title") or "")
    text = str(body.get("text") or "")
    # 气泡上方已经单列了标题 —— 正文里那份重复的标题要去掉，
    # 否则同一条晨报会在气泡里出现两遍。
    if title and text.startswith(title):
        text = text[len(title):].lstrip("\n")
    raw = json.dumps(body, ensure_ascii=False, indent=2)

    rows: List[str] = []
    for key, label in (
        ("intercepts", "总拦截次数"),
        ("red_line", "红线次数"),
        ("token_saved", "挽回 Token"),
        ("total_events", "处理事件"),
        ("duration_text", "值守时长"),
    ):
        if key not in stats:
            continue
        rows.append(
            '<div class="row"><span class="k">{}</span>'
            '<span class="v">{}</span></div>'.format(
                html.escape(label), html.escape(str(stats.get(key)))
            )
        )

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>晨报推送报文 · v3.2.0</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0; padding: 34px 38px; background: #0d1117; color: #c9d1d9;
    font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
  }}
  h1 {{ font-size: 21px; margin: 0 0 4px; color: #e6edf3; }}
  .sub {{ font-size: 13px; color: #7d8590; margin-bottom: 22px; }}
  .phone {{
    max-width: 460px; margin: 0 auto 26px; border: 1px solid #30363d;
    border-radius: 16px; padding: 18px; background: #161b22;
  }}
  .phone .who {{ font-size: 12px; color: #7d8590; margin-bottom: 9px; }}
  .bubble {{
    background: #1f6feb22; border: 1px solid #1f6feb55; border-radius: 12px;
    padding: 13px 15px; font-size: 14px; line-height: 1.85; white-space: pre-wrap;
    color: #e6edf3;
  }}
  .bubble .t {{
    display: block; font-weight: 700; margin-bottom: 6px; font-size: 15px;
  }}
  .stats {{ margin-top: 14px; }}
  .row {{
    display: flex; justify-content: space-between; font-size: 13px;
    padding: 6px 0; border-bottom: 1px dashed #30363d;
  }}
  .row:last-child {{ border-bottom: none; }}
  .k {{ color: #7d8590; }}
  .v {{ color: #79c0ff; font-variant-numeric: tabular-nums; }}
  pre {{
    max-width: 900px; margin: 0 auto; background: #161b22;
    border: 1px solid #30363d; border-radius: 12px; padding: 16px 18px;
    font-size: 12.5px; line-height: 1.6;
    white-space: pre-wrap; word-break: break-all;
    color: #adbac7; font-family: Consolas, "Cascadia Mono", monospace;
  }}
  .note {{
    max-width: 900px; margin: 12px auto 0; font-size: 12px; color: #7d8590;
    line-height: 1.7;
  }}
  .ok {{ color: #3fb950; }}
</style></head><body>
<h1>晨报推送报文 · 哨兵 Sentinel v3.2.0</h1>
<div class="sub">来源：Night Crawl 演习的 Mock Webhook —— <b>原文收到</b>，
一字未改（下面同时给出手机气泡与原始 JSON，可对照核对）。</div>

<div class="phone">
  <div class="who">哨兵 Sentinel · 夜间值守晨报</div>
  <div class="bubble"><span class="t">{title}</span>{text}</div>
  <div class="stats">{rows}</div>
</div>

<pre>{raw}</pre>
<div class="note">
  演练结果：处理事件 <b>{events}</b> 条 · 告警 <b>{alerts}</b> 条 ·
  命中规则 <b>{rules}</b> · Webhook 共收到 <b>{total}</b> 条
  （告警 {wa} / 晨报 {wb}）· 红线挂起 <b>{suspend}</b> ·
  内存：{memory}
  <br>判定：{checks}
</div>
</body></html>
""".format(
        title=html.escape(title),
        text=html.escape(text),
        rows="".join(rows),
        raw=html.escape(raw),
        events=report.get("events_processed", "-"),
        alerts=report.get("alerts", "-"),
        rules=html.escape(", ".join(report.get("alert_rules") or []) or "-"),
        total=report.get("webhook_total", "-"),
        wa=report.get("webhook_alerts", "-"),
        wb=report.get("webhook_briefings", "-"),
        suspend="是" if report.get("guardian_intercept") else "否",
        memory=html.escape(
            str((report.get("memory") or {}).get("note") or "-")
        ),
        checks=html.escape(
            " · ".join(
                "{}{}".format("✓" if ok else "✗", name)
                for name, ok in (report.get("checks") or {}).items()
            )
        ),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口。

    :param argv: 参数列表。
    :return: 进程退出码。
    """
    _prepare_stdout()
    parser = argparse.ArgumentParser(
        prog="capture_briefing_push",
        description="把 Night Crawl 收到的晨报报文渲染成截图",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Night Crawl 的 JSON 报告路径",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="截图输出路径（PNG）",
    )
    parser.add_argument(
        "--html",
        default="",
        help="中间 HTML 的落盘路径（默认放在输出旁边）",
    )
    parser.add_argument("--port", type=int, default=9335)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=1100)
    parser.add_argument("--wait", type=float, default=4.0)
    args = parser.parse_args(argv)

    report = _load_report(args.report)
    item = pick_briefing(report)
    if item is None:
        print("报告里没有晨报报文 —— 推给手机的那一条没到。")
        return 1

    html_path = args.html or (os.path.splitext(args.out)[0] + ".html")
    with io.open(html_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_html(item, report))
    print("中间 HTML：{}".format(html_path))

    # 借看板截图脚本的 CDP 能力渲染同一份 HTML。
    capture = os.path.join(ROOT, "_构建与发布", "scripts", "capture_dashboard.py")
    import subprocess

    url = "file:///" + html_path.replace("\\", "/")
    cmd = [
        sys.executable,
        capture,
        "--url",
        url,
        "--out",
        args.out,
        "--port",
        str(args.port),
        "--width",
        str(args.width),
        "--height",
        str(args.height),
        "--wait",
        str(args.wait),
    ]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(result.stderr or "")
    if result.returncode != 0:
        return result.returncode
    print("已保存：{}".format(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
