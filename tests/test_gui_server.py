"""哨兵 GUI 监控面板测试。

全部离线：不绑定真实端口、不访问网络。用 ``fastapi.testclient.TestClient``
在进程内驱动 ASGI 应用，文件读写都落在 pytest 的 ``tmp_path`` 里。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from src.core.safepoint import SafePointService
from src.gui.server import (
    AlertStore,
    EventHub,
    FileTailer,
    create_app,
    format_sse,
    main,
    normalize_alert,
    parse_alert_line,
    stream_events,
)

#: 静态资源目录（用来断言前端源码里的关键约定）。
STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "gui",
    "static",
)

# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------
ALERT_PAYLOAD = {
    "timestamp": 1793026126.39,
    "verdict": "DANGEROUS",
    "level": "critical",
    "rule_id": "dangerous_command",
    "reason": "匹配到高危命令: rm -rf",
    "evidence": "rm -rf",
    "source": "mcode",
    "event_type": "tool_call",
    "tool": "Bash",
    "command": "rm -rf /tmp/x",
    "arguments": {"command": "rm -rf /tmp/x"},
    "explanation": "清理临时构建产物。",
    "explanation_degraded": False,
}

ALERT_JSON_LINE = (
    "2026-10-26 22:48:46,393 WARNING ALERT_JSON "
    + json.dumps(ALERT_PAYLOAD, ensure_ascii=False)
)

HUMAN_LINE = (
    "2026-10-26 22:48:46,393 WARNING [CRITICAL] DANGEROUS: "
    "匹配到高危命令: rm -rf | 工具=Bash | 命令=rm -rf /tmp/x"
)


class _FakeRequest:
    """最小 Request 替身，让 SSE 生成器可以离线测试。

    :param disconnect_after: 第几次 ``is_disconnected()`` 开始返回真。
    """

    def __init__(self, disconnect_after: int = 1) -> None:
        self._remaining = int(disconnect_after)

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


def _make_app(tmp_path, **kwargs):
    """造一个指向临时目录的应用（默认不启动后台线程）。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")
    return create_app(
        str(watch),
        str(alert_log),
        start_watcher=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
def test_index_returns_dashboard_html(tmp_path):
    """GET / 返回面板 HTML，并含关键面板标记。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert 'id="status-bar"' in body
    assert 'id="alert-list"' in body
    assert 'id="log-lines"' in body
    assert 'id="scan-button"' in body


def test_static_assets_are_served(tmp_path):
    """静态资源由 StaticFiles 伺服。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        css = client.get("/static/dashboard.css")
        js = client.get("/static/dashboard.js")

    assert css.status_code == 200
    assert ".alert-card" in css.text
    assert js.status_code == 200
    assert "EventSource" in js.text


def test_copy_prompt_button_is_not_gated_by_guardian():
    """只读便利按钮不能被守护者的武装状态一起禁用。

    复制提示、发送指令、一键回滚都是**只读**操作 ——
    守护者没武装时（演示模式、还没拿到 PID）用户也应当能点，
    否则这些按钮等于白做。
    （``disabled`` 的按钮连 click 事件都不会触发。）
    """
    js = open(
        os.path.join(STATIC_DIR, "dashboard.js"), encoding="utf-8"
    ).read()

    # 禁用干预按钮的选择器必须**排除**辅助按钮。
    assert ".alert-actions .btn:not(.alert-card__aux)" in js
    # 不能退回「一网打尽」的写法。
    assert '".alert-actions .btn"' not in js
    # 三个辅助按钮都要挂上这枚标记。
    for marker in ("alert-card__copy", "alert-card__send",
                   "alert-card__rollback"):
        assert marker in js
    assert js.count("alert-card__aux") >= 4


def test_alert_list_is_not_rebuilt_when_unchanged():
    """告警列表**没变**时不得清空重建。

    看板每 5 秒拉一次告警列表。早先的实现是「清空 → 重建所有卡片」，
    于是用户正要按下的【复制纠偏提示】按钮被抽走重建 ——
    点了没反应（这条在实机上被撞到过）。
    """
    js = open(
        os.path.join(STATIC_DIR, "dashboard.js"), encoding="utf-8"
    ).read()

    # 必须有「渲染签名」这套机制。
    assert "renderedAlertIds" in js
    assert "function renderAlerts(" in js
    # 签名相同就**直接返回**，不去动 DOM。
    assert "signature === state.renderedAlertIds" in js


def test_normalize_alert_synthesizes_missing_alert_id():
    """老版本写的告警行没有 ``alert_id`` —— 要给它合成一个**稳定**的。

    没有 ID 的卡片，【复制纠偏提示】会静默失败（点了没反应）。
    """
    raw = {
        "timestamp": 1793026126.39196,
        "verdict": "LOOP",
        "rule_id": "repeat_read_loop",
        "reason": "连续 3 次读取同一个文件",
    }

    first = normalize_alert(dict(raw))
    second = normalize_alert(dict(raw))

    assert first["alert_id"]
    # 同一行日志必须每次得到同一个 ID（否则按 ID 找不回）。
    assert first["alert_id"] == second["alert_id"]
    # 不一样的告警不能撞号。
    other = normalize_alert({**raw, "reason": "别的告警"})
    assert other["alert_id"] != first["alert_id"]


def test_normalize_alert_keeps_existing_alert_id():
    """本来就有的 ``alert_id`` 不能被改写。"""
    raw = {
        "timestamp": 1793026126.39196,
        "alert_id": "1793091301482-000001",
        "verdict": "STUCK",
    }

    assert normalize_alert(raw)["alert_id"] == "1793091301482-000001"


# ---------------------------------------------------------------------------
# 状态接口
# ---------------------------------------------------------------------------
def test_api_state_returns_expected_shape(tmp_path):
    """GET /api/state 返回约定的 JSON 结构。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    data = response.json()
    for key in (
        "status",
        "watch_path",
        "alert_log_path",
        "counts",
        "alerts",
        "recent_lines",
        "running",
    ):
        assert key in data
    assert isinstance(data["counts"], dict)
    assert isinstance(data["alerts"], list)
    assert isinstance(data["recent_lines"], list)
    assert data["status"] in {"idle", "running", "paused"}


def test_api_alerts_returns_list(tmp_path):
    """GET /api/alerts 返回告警列表。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/alerts")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["alerts"], list)
    assert "counts" in data


# ---------------------------------------------------------------------------
# Origin / Host 防护
# ---------------------------------------------------------------------------
def test_origin_guard_rejects_foreign_origin(tmp_path):
    """不在白名单的 Origin 一律 403。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/api/state", headers={"origin": "http://evil.example.com"}
        )

    assert response.status_code == 403
    assert response.json()["ok"] is False


def test_origin_guard_allows_loopback_origin(tmp_path):
    """白名单内的 Origin 放行。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        allowed = client.get(
            "/api/state", headers={"origin": "http://127.0.0.1:8000"}
        )
        local = client.get(
            "/api/state", headers={"origin": "http://localhost:8000"}
        )

    assert allowed.status_code == 200
    assert local.status_code == 200


def test_requests_without_origin_are_allowed(tmp_path):
    """没有 Origin 头（同源导航 / curl）放行。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200


def test_create_app_refuses_non_loopback_host(tmp_path):
    """没有 --allow-remote 时拒绝监听非回环地址。"""
    watch = tmp_path / "a.jsonl"
    watch.write_text("", encoding="utf-8")

    with pytest.raises(ValueError):
        create_app(str(watch), str(tmp_path / "b.log"), host="0.0.0.0")


def test_main_refuses_non_loopback_host(tmp_path):
    """命令行入口同样拒绝非回环地址（直接退出，不启动服务）。"""
    code = main(
        ["--watch", str(tmp_path / "a.jsonl"), "--host", "0.0.0.0"]
    )

    assert code == 2


# ---------------------------------------------------------------------------
# SSE
# ---------------------------------------------------------------------------
def test_sse_stream_yields_at_least_one_event():
    """SSE 生成器至少产出一帧，且帧格式是 ``data: {json}``。"""
    async def scenario():
        hub = EventHub()
        store = AlertStore()
        queue = hub.subscribe()
        hub.publish({"type": "alert", "verdict": "DANGEROUS"})
        generator = stream_events(
            _FakeRequest(), queue, hub, store
        )
        return [frame async for frame in generator]
    frames = asyncio.run(scenario())

    assert frames
    assert all(frame.startswith("data: ") for frame in frames)
    assert '"type": "status"' in frames[0]
    assert any('"verdict": "DANGEROUS"' in frame for frame in frames)


def test_hub_delivers_events_from_another_thread():
    """后台线程投递的事件能安全进入事件循环。"""
    async def scenario():
        hub = EventHub()
        hub.attach_loop(asyncio.get_running_loop())
        queue = hub.subscribe()
        thread = threading.Thread(
            target=hub.publish,
            args=({"type": "line", "text": "hello"},),
        )
        thread.start()
        thread.join()
        return await asyncio.wait_for(queue.get(), timeout=2.0)

    event = asyncio.run(scenario())

    assert event["type"] == "line"
    assert event["text"] == "hello"


def test_format_sse_shape():
    """SSE 帧格式固定为 ``data: {json}\\n\\n``。"""
    frame = format_sse({"type": "line", "text": "x"})

    assert frame.startswith("data: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame[len("data: "):].strip())["text"] == "x"


# ---------------------------------------------------------------------------
# 告警解析
# ---------------------------------------------------------------------------
def test_parse_alert_line_extracts_structured_alert():
    """ALERT_JSON 行能被解析成结构化告警。"""
    alert = parse_alert_line(ALERT_JSON_LINE)

    assert alert is not None
    assert alert["verdict"] == "DANGEROUS"
    assert alert["rule_id"] == "dangerous_command"
    assert alert["command"] == "rm -rf /tmp/x"


def test_parse_alert_line_ignores_human_lines():
    """给人看的那一行不会被当成结构化告警。"""
    assert parse_alert_line(HUMAN_LINE) is None
    assert parse_alert_line("2026-10-26 22:48:46,393 WARNING 提示") is None


def test_parse_alert_line_rejects_broken_json():
    """JSON 非法时返回 None，不抛异常。"""
    assert parse_alert_line("WARNING ALERT_JSON {不是 JSON}") is None
    assert parse_alert_line("WARNING ALERT_JSON 不是 JSON") is None


def test_normalize_alert_fills_defaults():
    """缺省字段被补齐，方便前端直接渲染。"""
    normalized = normalize_alert({"verdict": "LOOP"})

    assert normalized["verdict"] == "LOOP"
    assert normalized["explanation_degraded"] is False
    assert normalized["arguments"] == {}
    assert normalized["rule_id"] == "-"


# ---------------------------------------------------------------------------
# 增量跟随
# ---------------------------------------------------------------------------
def test_tailer_reads_only_new_bytes(tmp_path):
    """tailer 只读新增字节，不会重复吐旧内容。"""
    path = tmp_path / "agent.jsonl"
    path.write_text("line-1\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["line-1"]
    assert tailer.poll() == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write("line-2\n")

    assert tailer.poll() == ["line-2"]
    assert tailer.poll() == []


def test_tailer_resets_offset_on_truncation(tmp_path):
    """文件被原地截断后，偏移归零重新读。"""
    path = tmp_path / "alert.log"
    path.write_text("aaaa\nbbbb\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["aaaa", "bbbb"]

    path.write_text("c\n", encoding="utf-8")

    assert tailer.poll() == ["c"]


def test_tailer_detects_rotation(tmp_path):
    """同名新文件（轮转）会把偏移归零。"""
    path = tmp_path / "agent.jsonl"
    path.write_text("old-1\n", encoding="utf-8")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["old-1"]

    if not os.stat(str(path)).st_ino:
        pytest.skip("当前文件系统不提供 st_ino，无法验证轮转检测")

    rotated = tmp_path / "rotated.jsonl"
    rotated.write_text("new-1\nnew-2\n", encoding="utf-8")
    os.replace(str(rotated), str(path))

    assert tailer.poll() == ["new-1", "new-2"]


def test_tailer_handles_crlf_and_broken_bytes(tmp_path):
    """CRLF 与非法字节都不会让 tailer 崩掉。"""
    path = tmp_path / "mixed.log"
    path.write_bytes(b"a\r\nb\r\n")
    tailer = FileTailer(str(path))

    assert tailer.poll() == ["a", "b"]

    with path.open("ab") as handle:
        handle.write(b"\xff\xfe\n")

    assert tailer.poll() == ["\ufffd\ufffd"]


def test_tailer_waits_for_complete_line(tmp_path):
    """半行不会被当成完整行吐出来。"""
    path = tmp_path / "partial.log"
    path.write_bytes(b"half")
    tailer = FileTailer(str(path))

    assert tailer.poll() == []

    with path.open("ab") as handle:
        handle.write(b"-done\n")

    assert tailer.poll() == ["half-done"]


# ---------------------------------------------------------------------------
# 扫描注入
# ---------------------------------------------------------------------------
def test_scan_endpoint_calls_injected_callback(tmp_path):
    """POST /api/scan 调用注入的回调并返回 {"ok": true}。"""
    calls = []

    def scan(path):
        calls.append(path)
        return {"counts": {"ON_TRACK": 3, "DANGEROUS": 1}}

    app = _make_app(tmp_path, scan_callback=scan)
    with TestClient(app) as client:
        response = client.post("/api/scan")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(calls) == 1


def test_scan_endpoint_without_callback_is_rejected(tmp_path):
    """没有注入回调时明确报错，而不是假装成功。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/scan")

    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_scan_endpoint_isolates_callback_failure(tmp_path):
    """回调抛异常时服务仍然活着，只是这次扫描失败。"""
    def scan(path):
        raise RuntimeError("boom")

    app = _make_app(tmp_path, scan_callback=scan)
    with TestClient(app) as client:
        response = client.post("/api/scan")
        alive = client.get("/api/state")

    assert response.status_code == 500
    assert response.json()["ok"] is False
    assert alive.status_code == 200


# ---------------------------------------------------------------------------
# 后台跟随集成
# ---------------------------------------------------------------------------
def test_watcher_picks_up_new_alerts(tmp_path):
    """告警日志新增一行，tailer 会把它变成结构化告警。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")

    app = create_app(
        str(watch),
        str(alert_log),
        start_watcher=True,
        poll_interval=0.05,
    )
    with TestClient(app) as client:
        with alert_log.open("a", encoding="utf-8") as handle:
            handle.write(ALERT_JSON_LINE + "\n")
            handle.flush()

        payload = {}
        deadline = 5.0
        waited = 0.0
        while waited < deadline:
            payload = client.get("/api/state").json()
            if payload["alerts"]:
                break
            waited += 0.05
            threading.Event().wait(0.05)

    assert payload.get("alerts"), "tailer 没有在预期时间内抓到新告警"
    alert = payload["alerts"][-1]
    assert alert["verdict"] == "DANGEROUS"
    assert alert["rule_id"] == "dangerous_command"
    assert payload["counts"].get("DANGEROUS") == 1


def test_watcher_tails_watched_log_lines(tmp_path):
    """被监控日志的新行会进入最近行列表。"""
    watch = tmp_path / "agent.jsonl"
    watch.write_text("", encoding="utf-8")
    alert_log = tmp_path / "alerts.log"
    alert_log.write_text("", encoding="utf-8")

    app = create_app(
        str(watch),
        str(alert_log),
        start_watcher=True,
        poll_interval=0.05,
    )
    with TestClient(app) as client:
        with watch.open("a", encoding="utf-8") as handle:
            handle.write('{"type": "text", "text": "你好"}\n')
            handle.flush()

        payload = {}
        deadline = 5.0
        waited = 0.0
        while waited < deadline:
            payload = client.get("/api/state").json()
            if payload["recent_lines"]:
                break
            waited += 0.05
            threading.Event().wait(0.05)

    assert payload.get("recent_lines"), "tailer 没有抓到被监控日志的新行"
    assert "你好" in payload["recent_lines"][-1]


# ---------------------------------------------------------------------------
# v2.1.0：回滚并重试 / 查看改动
# ---------------------------------------------------------------------------
def _make_session_app(tmp_path, **kwargs):
    """造一个带安全点服务与回调的单会话应用。"""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "app.py").write_text("good\n", encoding="utf-8")
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(str(work), label="基线安全点 · 会话起点")
    return _make_app(
        tmp_path,
        safe_point_service=service,
        workdir=str(work),
        **kwargs,
    )


def test_resume_endpoint_calls_callback(tmp_path):
    """POST resume 要把提示交给回调，并回一句固定播报。"""
    calls = []

    def _resume(prompt, meta):
        calls.append((prompt, meta))
        return {"ok": True, "message": "已复活"}

    app = _make_session_app(tmp_path, resume_callback=_resume)
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/default/alerts/1793091301482-000001/resume"
        )

    assert response.status_code in (200, 404)


def test_resume_endpoint_without_callback_reports_clearly(tmp_path):
    """没有回调时要说**人话**，不能 500。"""
    app = _make_session_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/sessions/default/alerts/1793091301482-000001/resume"
        )

    assert response.status_code in (200, 404)
    if response.status_code == 200:
        body = response.json()
        assert body["ok"] is False
        assert "复活" in body["error"]


def test_diff_endpoint_returns_file_changes(tmp_path):
    """GET diff 要返回真实的文件差异。"""
    app = _make_session_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/api/sessions/default/alerts/1793091301482-000001/diff"
        )

    assert response.status_code in (200, 404)
    if response.status_code == 200:
        body = response.json()
        assert "ok" in body


def test_diff_endpoint_without_service_reports_clearly(tmp_path):
    """没有安全点服务时要说**人话**。"""
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        response = client.get(
            "/api/sessions/default/alerts/1793091301482-000001/diff"
        )

    assert response.status_code in (200, 404)
    if response.status_code == 200:
        body = response.json()
        assert body["ok"] is False
        assert "安全点" in body["error"]


def test_resume_plan_endpoint_only_calculates(tmp_path):
    """复活计划只算不动 —— 文件不能被改。"""
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "app.py").write_text("good\n", encoding="utf-8")
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(str(work), label="基线安全点 · 会话起点")
    (work / "app.py").write_text("broken\n", encoding="utf-8")
    app = _make_app(
        tmp_path,
        safe_point_service=service,
        workdir=str(work),
    )
    with TestClient(app) as client:
        response = client.get("/api/sessions/default/resume-plan")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plan"]["restored_count"] == 1
    # 文件还是坏的 —— 计划没有动手。
    assert (work / "app.py").read_text(encoding="utf-8") == "broken\n"


def test_build_resume_prompt_mentions_the_reason():
    """复活提示要说清「为什么被拦」与「已经回滚」。"""
    from src.gui.server import _build_resume_prompt

    prompt = _build_resume_prompt({
        "verdict": "DANGEROUS",
        "reason": "红线级命中：批量删除",
        "command": "find . -name '*.tmp' -delete",
    })

    assert "DANGEROUS" in prompt
    assert "批量删除" in prompt
    assert "find . -name '*.tmp' -delete" in prompt
    assert "回滚" in prompt


def test_dashboard_js_has_new_buttons():
    """前端要有【查看改动】与【回滚并重试】。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(
        os.path.join(root, "src", "gui", "static", "dashboard.js"),
        encoding="utf-8",
    ).read()

    assert "查看改动" in js
    assert "回滚并重试" in js
    assert "buildViewDiffButton" in js
    assert "buildResumeButton" in js
    # 五个只读便利按钮 + 禁用选择器 —— 每个都得挂 alert-card__aux，
    # 否则守护者没武装时会被一起禁用（v1.2.0 / v2.0.0 各撞过一次）。
    for marker in (
        "alert-card__copy",
        "alert-card__send",
        "alert-card__rollback",
        "alert-card__diff",
        "alert-card__resume",
    ):
        assert marker in js
    assert ":not(.alert-card__aux)" in js
    assert js.count("alert-card__aux") >= 6


def test_dashboard_js_has_no_adjacent_string_literals():
    """相邻的两个字符串字面量会被 ASI 拆成两条语句。

    ⚠️ 这类代码 ``node --check`` **查不出来**（语法合法），
    但第二段会变成空操作 —— 表现是「弹窗里的文字少了一半」。
    实机截图时撞过一次，所以这里加一条回归。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(
        os.path.join(root, "src", "gui", "static", "dashboard.js"),
        encoding="utf-8",
    ).read()

    import re

    hits = re.findall(r'"[ \t]*\r?\n[ \t]*"', js)
    assert not hits, (
        "dashboard.js 里有相邻的字符串字面量（ASI 会把第二段拆成空操作）"
    )
