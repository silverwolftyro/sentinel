"""Web Diff 预览 + Webhook 真实自检测试（v2.1.0）。

覆盖：快照时的变动清单、按需 unified diff、
以及「启动时真的发一条出去」的自检链路。
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.core.safepoint import SafePointService
from src.core.webhook_probe import (
    build_probe_alert,
    configured_webhooks,
    local_ips,
    startup_probe,
)


def _make_tree(root, files):
    """在 ``root`` 下造出若干文件。"""
    for name, content in files.items():
        path = os.path.join(root, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    return root


# ---------------------------------------------------------------------------
# 变动清单
# ---------------------------------------------------------------------------
def test_capture_records_changes_against_previous_point(tmp_path):
    """第二次快照要记下「相对上一次变了什么」。"""
    work = _make_tree(tmp_path / "work", {"a.py": "one\n"})
    service = SafePointService(root=str(tmp_path / "points"))

    service.capture(work, label="基线安全点 · 会话起点")
    with open(os.path.join(work, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("two\n")
    with open(os.path.join(work, "b.py"), "w", encoding="utf-8") as handle:
        handle.write("new\n")
    point = service.capture(work, label="拦截点")

    manifest = json.loads(
        open(point.manifest_path, encoding="utf-8").read()
    )
    changes = manifest["changes"]
    assert "a.py" in changes["modified"]
    assert "b.py" in changes["added"]
    assert changes["removed"] == []


def test_capture_records_removed_files(tmp_path):
    """文件被删了，也要记下来。"""
    work = _make_tree(tmp_path / "work", {"a.py": "one\n", "b.py": "two\n"})
    service = SafePointService(root=str(tmp_path / "points"))

    service.capture(work, label="基线安全点 · 会话起点")
    os.remove(os.path.join(work, "b.py"))
    point = service.capture(work, label="拦截点")

    manifest = json.loads(
        open(point.manifest_path, encoding="utf-8").read()
    )
    assert "b.py" in manifest["changes"]["removed"]


def test_first_snapshot_has_empty_changes(tmp_path):
    """第一次快照没有「上一次」，变动清单就是空的。"""
    work = _make_tree(tmp_path / "work", {"a.py": "one\n"})
    service = SafePointService(root=str(tmp_path / "points"))

    point = service.capture(work, label="基线安全点 · 会话起点")

    manifest = json.loads(
        open(point.manifest_path, encoding="utf-8").read()
    )
    assert manifest["changes"] == {
        "modified": [], "added": [], "removed": []
    }


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------
def test_diff_reports_modified_added_removed(tmp_path):
    """diff 要同时报出「改了 / 加了 / 删了」。"""
    work = _make_tree(
        tmp_path / "work", {"a.py": "one\n", "gone.py": "bye\n"}
    )
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(work, label="基线安全点 · 会话起点")

    with open(os.path.join(work, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("two\n")
    os.remove(os.path.join(work, "gone.py"))
    with open(os.path.join(work, "new.py"), "w", encoding="utf-8") as handle:
        handle.write("hi\n")

    result = service.diff()

    assert result["ok"] is True
    assert "a.py" in result["modified"]
    assert "new.py" in result["added"]
    assert "gone.py" in result["removed"]
    assert result["modified_count"] == 1
    assert result["added_count"] == 1
    assert result["removed_count"] == 1


def test_diff_contains_unified_hunks(tmp_path):
    """diff 要给出真正的行级差异（红绿背景靠它渲染）。"""
    work = _make_tree(tmp_path / "work", {"a.py": "one\ntwo\nthree\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(work, label="基线安全点 · 会话起点")

    with open(os.path.join(work, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("one\nTWO\nthree\n")

    result = service.diff()

    assert result["files"]
    detail = result["files"][0]
    assert detail["path"] == "a.py"
    assert detail["added"] >= 1
    assert detail["removed"] >= 1
    joined = "\n".join(detail["lines"])
    assert "-two" in joined
    assert "+TWO" in joined


def test_diff_skips_binary_files(tmp_path):
    """二进制文件做行级 diff 只会得到乱码 —— 不做。"""
    work = tmp_path / "work"
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "a.bin"), "wb") as handle:
        handle.write(b"\x00\x01\x02binary")
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(work, label="基线安全点 · 会话起点")

    with open(os.path.join(work, "a.bin"), "wb") as handle:
        handle.write(b"\x00\x01\x03changed")

    result = service.diff()

    assert result["modified_count"] == 1
    assert result["files"] == []
    assert result["skipped"] == ["a.bin"]


def test_diff_without_safe_point_fails_gracefully(tmp_path):
    """没有安全点就**如实说**没有，别假装有。"""
    service = SafePointService(root=str(tmp_path / "points"))

    result = service.diff()

    assert result["ok"] is False
    assert "没有" in result["error"]


# ---------------------------------------------------------------------------
# Webhook 自检
# ---------------------------------------------------------------------------
def test_local_ips_returns_list():
    """本机 IP 至少要能列出来（哪怕是虚拟网卡）。"""
    ips = local_ips()
    assert isinstance(ips, list)
    assert all(isinstance(item, str) for item in ips)


def test_probe_alert_contains_version_and_ip():
    """自检消息里必须有版本号与本机 IP。"""
    alert = build_probe_alert("v2.1.0")

    text = alert["explanation"]
    assert "v2.1.0" in text
    assert "IP" in text
    assert alert["triggers"] == ["webhook_startup_test"]


def test_configured_webhooks_skips_disabled_and_empty():
    """没启用、或者没填地址的，都不算。"""
    config = {
        "alert": {
            "channels": [
                {"type": "webhook", "enabled": False, "url": "https://a"},
                {"type": "webhook", "enabled": True, "url": ""},
                {"type": "console", "enabled": True},
                {"type": "webhook", "enabled": True, "url": "https://b"},
            ]
        }
    }

    entries = configured_webhooks(config)

    assert len(entries) == 1
    assert entries[0]["url"] == "https://b"


def test_startup_probe_is_skipped_when_disabled():
    """开关关掉就**一条都不发**。"""
    config = {
        "alert": {
            "webhook_startup_test": False,
            "channels": [
                {"type": "webhook", "enabled": True, "url": "https://b"}
            ],
        }
    }

    assert startup_probe(config, "v2.1.0") is None


def test_startup_probe_really_posts_to_server():
    """自检必须**真的发出去** —— 而且要能拿到响应体。"""
    received = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 —— BaseHTTPRequestHandler 的约定
            length = int(self.headers.get("Content-Length") or 0)
            received.append(self.rfile.read(length))
            body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}/hook".format(server.server_address[1])
    config = {
        "alert": {
            "channels": [
                {"type": "webhook", "enabled": True, "url": url,
                 "format": "generic", "timeout": 5}
            ]
        }
    }
    try:
        results = startup_probe(config, "v2.1.0")
    finally:
        server.shutdown()
        server.server_close()

    assert results is not None
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert results[0]["status"] == 200
    assert "errcode" in results[0]["body"]
    # 真的发出去了一条。
    assert received
    payload = json.loads(received[0].decode("utf-8"))
    assert payload


def test_startup_probe_reports_server_rejection():
    """服务端拒收（错误码非 0）要**如实报失败**，不能装作成功。"""
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            body = json.dumps({"errcode": 310000, "errmsg": "ip not in list"}).encode(
                "utf-8"
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:{}/hook".format(server.server_address[1])
    config = {
        "alert": {
            "channels": [
                {"type": "webhook", "enabled": True, "url": url,
                 "format": "dingtalk", "timeout": 5}
            ]
        }
    }
    try:
        results = startup_probe(config, "v2.1.0")
    finally:
        server.shutdown()
        server.server_close()

    assert results is not None
    # HTTP 200 但业务错误码非 0 —— 通道认为投递成功，
    # 但**自检必须提醒用户**响应体里有错。
    assert results[0]["status"] == 200
    assert "310000" in results[0]["body"]
