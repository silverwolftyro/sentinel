"""v3.9.0 回归：底层判定宽容度修复、摘要剥离与移动端适配。

这一版**不改红线**（R1–R9 与 v2.1.0 完全一致），只做四件事：

1. **子阶段宽容度** —— 开发任务天然包含「改 CHANGELOG / bump 版本号 /
   跑验证」这些发版前的常规铺垫。本版从两层放宽：
   * Prompt 层（:mod:`src.llm.prompts`）加【子阶段宽容度】强约束；
   * 确定性层（:func:`src.core.engine.detect_routine_maintenance`）
     命中白名单就**根本不发 L2 请求**，直接判 ``ON_TRACK``。
2. **极限摘要剥离** —— 100 档只留自然语言描述，代码块整段丢掉，
   长文本只取前两句（:mod:`src.core.live_audit` + 前端同算法）。
3. **短输出不被截断** —— ``max_tokens`` 下限 50；极短输出请求
   强制注入 ``reasoning_effort: "none"``。
4. **移动端适配** —— 窄屏 768px 下单列 Tab、隐藏分隔条、按钮 ≥ 44px。

⚠️ 本文件里有几条测试**读源码文本**。
这是有意的：那些约定一旦被改坏，运行时不一定报错，
但用户立刻能感觉到卡顿 / 溢出 / 误报。

⚠️ 本文件里的夹具来自**真实生产推理**
（``tests/fixtures/v390_real_drift_falsepositives.json``），
不是构造出来的 —— 那 3 个用例当年真的被判过 ``DRIFT``。
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any, Dict, List, Optional

import pytest

from src.core.engine import (
    IntentTracker,
    detect_routine_maintenance,
)
from src.core.live_audit import (
    extract_snippet,
    first_sentences,
    strip_code_blocks,
)
from src.llm.api_client import MIN_MAX_TOKENS, OpenAICompatibleClient
from src.llm.base import DriftResult
from src.llm.prompts import (
    CASCADE_FILTER_TEMPLATE,
    CASCADE_VERIFY_TEMPLATE,
    DRIFT_EVALUATE_TEMPLATE,
)

#: 仓库根目录（本文件在 ``tests/`` 下）。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 真实误报夹具。
FIXTURE_PATH = os.path.join(
    ROOT, "tests", "fixtures", "v390_real_drift_falsepositives.json"
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _read(*parts: str) -> str:
    """读一个仓库里的文本文件。"""
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _read_js(name: str) -> str:
    """读前端脚本。"""
    return _read("src", "gui", "static", name)


def _read_css() -> str:
    """读看板样式表。"""
    return _read("src", "gui", "static", "dashboard.css")


def _load_fixture() -> Dict[str, Any]:
    """读真实误报夹具。"""
    with io.open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class _ExplodingEvaluator:
    """一被调用就炸的判定器 —— 用来证明「白名单不发请求」。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __call__(self, goal: str, summary: str) -> DriftResult:
        self.calls.append((goal, summary))
        raise AssertionError("白名单命中时不该调用 L2 判定器")


class _CountingEvaluator:
    """记录调用次数的判定器。"""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __call__(self, goal: str, summary: str) -> DriftResult:
        self.calls.append((goal, summary))
        return DriftResult(drift=False, reason="仍在推进目标")


def _thinking(text: str) -> Dict[str, Any]:
    """构造一个 thinking 事件。"""
    return {
        "source": "mcode",
        "type": "thinking",
        "tool": None,
        "text": text,
        "is_error": False,
    }


def _tool(
    name: str,
    command: str = "",
    file_path: str = "",
) -> Dict[str, Any]:
    """构造一个 tool_call 事件。"""
    arguments: Dict[str, Any] = {}
    if command:
        arguments["command"] = command
    if file_path:
        arguments["file_path"] = file_path
    return {
        "source": "mcode",
        "type": "tool_call",
        "tool": {
            "name": name,
            "arguments": arguments,
            "command": command,
        },
        "text": "",
        "is_error": False,
    }


def _drive(
    tracker: IntentTracker,
    events: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """把一窗事件灌进追踪器，返回最后一次判定结果。"""
    result: Optional[Dict[str, Any]] = None
    for event in events:
        result = tracker.observe(event)
    return result


# ---------------------------------------------------------------------------
# 任务一 A：Prompt 层的子阶段宽容度
# ---------------------------------------------------------------------------
class Test子阶段宽容度Prompt:
    """三个 System Prompt 都要有【子阶段宽容度】强约束。"""

    def test_判定模板必须点名常见演进文件(self) -> None:
        """漂移判定模板要点名 CHANGELOG 等文件。"""
        assert "子阶段宽容度" in DRIFT_EVALUATE_TEMPLATE
        for token in ("CHANGELOG.md", "package.json", "version.txt"):
            assert token in DRIFT_EVALUATE_TEMPLATE, token

    def test_判定模板必须点名标准验证命令(self) -> None:
        """漂移判定模板要点名 pytest / npm run build。"""
        assert "pytest" in DRIFT_EVALUATE_TEMPLATE
        assert "npm run build" in DRIFT_EVALUATE_TEMPLATE

    def test_判定模板必须写明不得判为漂移(self) -> None:
        """必须明确「不得判定为漂移」。"""
        section = DRIFT_EVALUATE_TEMPLATE.split(
            "【子阶段宽容度", 1
        )[1].split("【", 1)[0]
        assert "不得" in section
        assert "判定为漂移" in section

    def test_极速过滤模板也要有(self) -> None:
        """L2 极速过滤模板也要带约束（它最容易误报）。"""
        assert "子阶段宽容度" in CASCADE_FILTER_TEMPLATE

    def test_独立复核模板也要有(self) -> None:
        """L3 独立复核模板也要带约束。"""
        assert "子阶段宽容度" in CASCADE_VERIFY_TEMPLATE

    def test_复核模板要能纠正极速过滤的误判(self) -> None:
        """L3 要能把 L2 的这类误判纠正回来。"""
        assert "ON_TRACK" in CASCADE_VERIFY_TEMPLATE


# ---------------------------------------------------------------------------
# 任务一 B：确定性白名单
# ---------------------------------------------------------------------------
class Test确定性白名单:
    """白名单命中就直接判 ON_TRACK，不发 L2 请求。"""

    def test_编辑变更日志要放行(self) -> None:
        """Edit CHANGELOG.md → 放行。"""
        events = [
            _tool("Edit", file_path="D:/proj/CHANGELOG.md"),
            _thinking("写完变更说明了。"),
            _tool("Read", file_path="D:/proj/src/app.py"),
            _thinking("再确认一下调用点。"),
            _tool("Grep", command="grep -rn foo src/"),
        ]
        reason = detect_routine_maintenance(events)
        assert reason is not None
        assert "changelog.md" in reason

    def test_改包配置要放行(self) -> None:
        """Edit package.json → 放行。"""
        events = [_tool("Edit", file_path="D:/proj/package.json")]
        reason = detect_routine_maintenance(events)
        assert reason is not None
        assert "package.json" in reason

    def test_改版本号文件要放行(self) -> None:
        """Edit version.txt → 放行。"""
        events = [_tool("Edit", file_path="D:/proj/version.txt")]
        reason = detect_routine_maintenance(events)
        assert reason is not None
        assert "version.txt" in reason

    def test_标准验证命令要放行(self) -> None:
        """pytest / npm run build → 放行。"""
        assert detect_routine_maintenance(
            [_tool("Bash", command="pytest tests/auth")]
        ) is not None
        assert detect_routine_maintenance(
            [_tool("Bash", command="npm run build")]
        ) is not None
        assert detect_routine_maintenance(
            [_tool("Bash", command="python -m pytest -q")]
        ) is not None

    def test_提到验证命令的普通命令不放行(self) -> None:
        """``echo "npm test"`` 只是**提到**，不算验证命令。"""
        assert (
            detect_routine_maintenance(
                [_tool("Bash", command='echo "npm test"')]
            )
            is None
        )

    def test_无关动作不放行(self) -> None:
        """改业务代码不算常规铺垫。"""
        events = [
            _tool("Edit", file_path="D:/proj/src/orders/service.py"),
            _thinking("把订单逻辑改一改。"),
            _tool("Bash", command="git status --short"),
        ]
        assert detect_routine_maintenance(events) is None

    def test_思考里声明要改变更日志也要放行(self) -> None:
        """真实生产日志里的误报就是纯思考窗口 —— 必须接得住。"""
        events = [
            _thinking("测试都过了，按项目惯例把这次变更写进 CHANGELOG。"),
            _thinking("顺便把版本号往上提一位。"),
            _thinking("再看看帮助文本的版本段落顺序。"),
            _thinking("顺序确认了，新版本段放在最前面。"),
            _thinking("开始写吧。"),
        ]
        reason = detect_routine_maintenance(events)
        assert reason is not None
        assert "changelog" in reason

    def test_白名单命中时根本不发请求(self) -> None:
        """放行 = 省算力（evaluator 一次都不该被调用）。"""
        evaluator = _ExplodingEvaluator()
        tracker = IntentTracker(goal="发布 v3.9.0", evaluator=evaluator)
        events = [
            _tool("Bash", command="pytest tests/ -q"),
            _thinking("验证过了。"),
            _tool("Edit", file_path="D:/proj/CHANGELOG.md"),
            _thinking("把版本号也提一下。"),
            _tool("Read", file_path="D:/proj/src/__init__.py"),
        ]
        result = _drive(tracker, events)
        assert result is not None
        assert result["drift"] is False
        assert result["bypassed"] is True
        assert result["alert"] is False
        assert evaluator.calls == []
        assert tracker.bypassed == 1

    def test_放行会把连续漂移计数清零(self) -> None:
        """常规铺垫把之前的漂移嫌疑洗掉。"""
        evaluator = _CountingEvaluator()
        tracker = IntentTracker(goal="发布 v3.9.0", evaluator=evaluator)
        _drive(tracker, [_tool("Bash", command=f"step{i}") for i in range(5)])
        assert tracker.consecutive_drift == 0
        _drive(
            tracker,
            [
                _tool("Bash", command="pytest tests/ -q"),
                _thinking("跑一下验证。"),
                _tool("Edit", file_path="D:/proj/CHANGELOG.md"),
                _thinking("记一笔。"),
                _tool("Read", file_path="D:/proj/README.md"),
            ],
        )
        assert tracker.consecutive_drift == 0
        assert tracker.bypassed == 1

    def test_不放行的窗口照常判定(self) -> None:
        """白名单不能变成「一律放行」。"""
        evaluator = _CountingEvaluator()
        tracker = IntentTracker(goal="重构 auth 模块", evaluator=evaluator)
        events = [_tool("Bash", command=f"step{i}") for i in range(5)]
        result = _drive(tracker, events)
        assert result is not None
        assert len(evaluator.calls) == 1
        assert result["bypassed"] is False


# ---------------------------------------------------------------------------
# 任务四：真实误报夹具
# ---------------------------------------------------------------------------
class Test真实误报夹具:
    """真实生产日志里的 3 个「正常改 CHANGELOG」窗口。"""

    def test_夹具文件存在且有三个用例(self) -> None:
        """夹具本身要存在、且正好三个。"""
        fixture = _load_fixture()
        assert len(fixture["cases"]) == 3

    def test_夹具出处可核验(self) -> None:
        """夹具必须写明原始日志的指纹。"""
        source = _load_fixture()["source_log"]
        assert source["bytes"] == 1262856
        assert re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        assert source["records_total"] == 61

    def test_两个用例当年真的被判过漂移(self) -> None:
        """误报用例的基线判定必须是 drift=true。"""
        fixture = _load_fixture()
        kinds = [case["kind"] for case in fixture["cases"]]
        assert kinds.count("false_positive") == 2
        assert kinds.count("control") == 1
        for case in fixture["cases"]:
            if case["kind"] == "false_positive":
                assert case["baseline_verdict"]["drift"] is True

    def test_三个用例都要被白名单接住(self) -> None:
        """核心断言：正常改 CHANGELOG **不再触发 DRIFT**。"""
        fixture = _load_fixture()
        for case in fixture["cases"]:
            evaluator = _ExplodingEvaluator()
            tracker = IntentTracker(
                goal=case["goal"][:200], evaluator=evaluator
            )
            result = _drive(tracker, case["window_events"])
            assert result is not None, case["case_id"]
            assert result["drift"] is False, case["case_id"]
            assert result["bypassed"] is True, case["case_id"]
            assert result["alert"] is False, case["case_id"]
            assert evaluator.calls == [], case["case_id"]
            assert case["expect"]["reason_contains"] in result["reason"]

    def test_误报用例的窗口确实提到了变更日志(self) -> None:
        """夹具必须名副其实 —— 窗口里真的有 CHANGELOG。"""
        fixture = _load_fixture()
        for case in fixture["cases"]:
            assert "changelog" in case["window_steps_text"].lower()


# ---------------------------------------------------------------------------
# 红线不受影响
# ---------------------------------------------------------------------------
class Test红线不受影响:
    """白名单只作用于**软告警层**，L1 红线照常拦。"""

    def test_同一窗口里的红线命令仍被拦下(self) -> None:
        """窗口里既有 CHANGELOG 又有 ``rm -rf /`` → 红线必须响。"""
        from src.adapters.mcode import McodeAdapter
        from src.rules.basic import RuleEngine

        lines = [
            json.dumps(
                {
                    "type": "tool_call",
                    "tool_call": {
                        "name": "Edit",
                        "arguments": {
                            "file_path": "D:/proj/CHANGELOG.md"
                        },
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "type": "tool_call",
                    "tool_call": {
                        "name": "Bash",
                        "arguments": {"command": "rm -rf /"},
                    },
                },
                ensure_ascii=False,
            ),
        ]
        log_path = os.path.join(ROOT, "logs", "v390-demo", "_redline.jsonl")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with io.open(log_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")

        tracker = IntentTracker(goal="发布 v3.9.0", evaluator=_CountingEvaluator())
        from src.core.engine import SentinelEngine

        engine = SentinelEngine(
            McodeAdapter(),
            RuleEngine({"rules": {"enabled": True}}),
            alert_log_path=os.path.join(
                ROOT, "logs", "v390-demo", "_redline-alert.log"
            ),
            console=False,
            intent_tracker=tracker,
        )
        try:
            summary = engine.run(log_path)
        finally:
            engine.close()

        red_lines = [
            alert
            for alert in summary["alerts"]
            if alert.get("severity") == "red_line"
        ]
        assert red_lines, "红线命令必须仍然被拦下"

    def test_白名单函数只返回理由不改规则(self) -> None:
        """白名单是**纯函数** —— 它不碰规则层。"""
        from src.rules.basic import RuleEngine

        engine = RuleEngine({"rules": {"enabled": True}})
        verdict = engine.check_event(
            {
                "type": "tool_call",
                "tool": {
                    "name": "Bash",
                    "arguments": {"command": "rm -rf /"},
                    "command": "rm -rf /",
                },
            }
        )
        assert verdict["verdict"] != "ON_TRACK"


# ---------------------------------------------------------------------------
# 任务二：摘要剥离
# ---------------------------------------------------------------------------
class Test摘要代码块剥离:
    """极限摘要里不该出现代码块。"""

    def test_围栏块整段丢掉(self) -> None:
        """带语言标记的围栏块要整段消失。"""
        text = "先说结论。\n\n```python\nprint('hi')\n```\n\n再说细节。"
        result = strip_code_blocks(text)
        assert "```" not in result
        assert "print" not in result
        assert "先说结论。" in result
        assert "再说细节。" in result

    def test_未闭合围栏丢掉其后全部(self) -> None:
        """没闭合的围栏 = 后面全都不可信。"""
        result = strip_code_blocks("前面这段保留。\n```\n后面的都不要")
        assert "后面" not in result
        assert "前面这段保留。" in result

    def test_行内三个反引号不算围栏(self) -> None:
        """``a`b`` 这种行内代码不该被当成围栏。"""
        result = strip_code_blocks("使用 `code` 引用一段文字。")
        assert "`code`" in result

    def test_取前两句(self) -> None:
        """中文长文本只留前两句。"""
        text = "第一句。第二句。第三句。第四句。"
        assert first_sentences(text, 2) == "第一句。第二句。"

    def test_英文句末不能劈开文件名(self) -> None:
        """``engine.py`` 里的点号不是句末。"""
        text = "I touched engine.py and ran v1.5 checks. It is fine."
        result = first_sentences(text, 1)
        assert "engine.py" in result
        assert "v1.5" in result

    def test_摘要出口必须剥离代码块(self) -> None:
        """``extract_snippet`` 的文本分支要先剥离。"""
        snippet = extract_snippet(
            {
                "type": "thinking",
                "text": "结论在这。\n\n```bash\nrm -rf /\n```\n\n补充一句。",
            }
        )
        assert "```" not in snippet
        assert "rm -rf" not in snippet


# ---------------------------------------------------------------------------
# 任务三：Token 预算
# ---------------------------------------------------------------------------
class TestToken预算:
    """短输出请求不该被推理内容吃光额度。"""

    def test_下限常量(self) -> None:
        """下限就是 50。"""
        assert MIN_MAX_TOKENS == 50

    def test_构造时抬高过小的额度(self) -> None:
        """max_tokens=4 也得按 50 走。"""
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            api_key="k",
            model="m",
            max_tokens=4,
            config={"llm": {}},
        )
        assert client.max_tokens == MIN_MAX_TOKENS

    def test_短输出强制关掉推理(self) -> None:
        """short_output=True → reasoning_effort 强制 none。"""
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            api_key="k",
            model="m",
            reasoning_effort="high",
            short_output=True,
            config={"llm": {}},
        )
        assert client.reasoning_effort == "none"

    def test_短输出同时关掉_json模式(self) -> None:
        """JSON 模式与「只回答一个字」矛盾，必须关。"""
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            api_key="k",
            model="m",
            json_mode=True,
            short_output=True,
            config={"llm": {}},
        )
        assert client.json_mode is False

    def test_运行期被改小也会兜底(self) -> None:
        """Payload 里有第二道防线。"""
        client = OpenAICompatibleClient(
            base_url="http://127.0.0.1:8888/v1",
            api_key="k",
            model="m",
            short_output=True,
            config={"llm": {}},
        )
        client.max_tokens = 1
        captured: Dict[str, Any] = {}

        class _Resp:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> Dict[str, Any]:
                return {"choices": [{"message": {"content": "ok"}}]}

        class _Session:
            def post(self, url, json=None, headers=None, timeout=None):
                captured.update(json or {})
                return _Resp()

        client.session = _Session()
        client.chat("系统", "你好")
        assert captured["max_tokens"] == MIN_MAX_TOKENS
        assert captured["reasoning_effort"] == "none"
        assert "response_format" not in captured


# ---------------------------------------------------------------------------
# 移动端适配
# ---------------------------------------------------------------------------
class Test移动端适配:
    """768px 断点下的窄屏布局。"""

    def test_存在768断点(self) -> None:
        """看板样式表要有 768px 的移动端块。"""
        assert "@media (max-width: 768px)" in _read_css()

    def test_窄屏隐藏分隔条(self) -> None:
        """拖拽分隔条在窄屏下必须消失。"""
        css = _read_css()
        block = css.split("@media (max-width: 768px)", 1)[1]
        assert re.search(r"\.resizer[^{]*\{[^}]*display\s*:\s*none", block)

    def test_窄屏按钮不小于44像素(self) -> None:
        """触摸目标至少 44px。"""
        css = _read_css()
        block = css.split("@media (max-width: 768px)", 1)[1]
        heights = [
            int(value)
            for value in re.findall(r"min-height\s*:\s*(\d+)px", block)
        ]
        assert heights, "窄屏块里应该有 min-height"
        assert max(heights) >= 44

    def test_底部标签栏存在(self) -> None:
        """模板里要有四个 Tab 的导航条。"""
        html = _read("src", "gui", "templates", "dashboard.html")
        assert 'id="mobile-tabs"' in html

    def test_看板脚本有移动端标签逻辑(self) -> None:
        """前端要有 Tab 切换的实现。"""
        js = _read_js("dashboard.js")
        assert "initMobileTabs" in js
        assert "applyMobileTab" in js

    def test_路由脚本会同步标签状态(self) -> None:
        """换页后 body 上的 data 属性要跟着换。"""
        assert "swapBody" in _read_js("router.js")


# ---------------------------------------------------------------------------
# 前后端算法一致
# ---------------------------------------------------------------------------
class Test前后端一致:
    """极限摘要的剥离算法前后端必须一致。"""

    def test_前端有同名的两个函数(self) -> None:
        """前端镜像了后端的两个原语。"""
        js = _read_js("dashboard.js")
        assert "stripLogCodeBlocks" in js
        assert "firstSentences" in js

    def test_前端暴露了100档常量(self) -> None:
        """100 档只取前两句。"""
        js = _read_js("dashboard.js")
        assert "LOG_EXTREME_SENTENCES" in js
        assert re.search(r"LOG_EXTREME_SENTENCES\s*=\s*2", js)


# ---------------------------------------------------------------------------
# 前端行为（用 Node 真跑一遍）
# ---------------------------------------------------------------------------
class Test前端行为:
    """用 Node 把 dashboard.js 跑起来，断言它的**行为**。"""

    def test_夹具全绿(self) -> None:
        """Node 在 → 跑夹具；不在 → 跳过（Node 不是运行依赖）。"""
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            pytest.skip("本机没有 Node，跳过前端行为测试")
        harness = os.path.join(
            ROOT, "tests", "fixtures", "v390_frontend_harness.js"
        )
        result = subprocess.run(
            [node, harness],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            # Node 输出是 UTF-8，而本机默认编码是 GBK，必须显式指定，
            # 否则中文用例名会让解码线程抛 UnicodeDecodeError。
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FAIL" not in result.stdout
        assert "通过" in result.stdout
