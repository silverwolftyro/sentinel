"""安全点（快照 / 回滚）测试（v2.0.0）。

覆盖：全量快照、增量降级、清单完整性、回滚恢复、删除新增文件、路径穿越防护。
"""

from __future__ import annotations

import os

from src.core.safepoint import SafePointService


def _make_tree(root, files):
    """在 ``root`` 下造出若干文件。"""
    for name, content in files.items():
        path = os.path.join(root, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    return root


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------
def test_capture_records_manifest(tmp_path):
    """快照要记下目录里当时有哪些文件。"""
    work = _make_tree(
        tmp_path / "work",
        {"a.py": "print(1)\n", "pkg/b.py": "print(2)\n"},
    )
    service = SafePointService(root=str(tmp_path / "points"))

    point = service.capture(work, label="测试快照")

    assert point.id
    assert point.files_count == 2
    assert point.mode == "full"
    assert point.backed_up_count == 2
    assert os.path.exists(point.manifest_path)


def test_capture_skips_excluded_dirs(tmp_path):
    """``node_modules`` / ``.git`` 这些不进快照。"""
    work = _make_tree(
        tmp_path / "work",
        {
            "a.py": "x\n",
            "node_modules/pkg/index.js": "junk\n",
            ".git/config": "[core]\n",
        },
    )
    service = SafePointService(root=str(tmp_path / "points"))

    point = service.capture(work)

    assert point.files_count == 1
    assert point.backed_up_count == 1


def test_big_dir_degrades_to_incremental(tmp_path):
    """目录太大时降级成增量模式（只备份最近改动的）。"""
    work = _make_tree(tmp_path / "work", {"a.py": "x\n"})
    service = SafePointService(
        root=str(tmp_path / "points"),
        max_bytes=1,  # 强制降级
        recent_minutes=30,
    )

    point = service.capture(work)

    assert point.mode == "incremental"
    # 清单依然完整 —— 回滚要靠它判断哪些文件是新增的。
    assert point.files_count == 1


def test_capture_handles_missing_directory(tmp_path):
    """目录不存在时不能抛异常。"""
    service = SafePointService(root=str(tmp_path / "points"))

    point = service.capture(str(tmp_path / "nope"))

    assert point.id == ""
    assert point.files_count == 0


# ---------------------------------------------------------------------------
# 回滚
# ---------------------------------------------------------------------------
def test_restore_reverts_modified_file(tmp_path):
    """被改坏的���件要能恢复成原样。"""
    work = _make_tree(tmp_path / "work", {"a.py": "print('good')\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    point = service.capture(work, label="基线安全点")

    # Agent 把文件改坏了。
    with open(os.path.join(work, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("print('broken')\n")

    result = service.restore(point.id, work)

    assert result["ok"] is True
    assert result["restored_count"] == 1
    with open(os.path.join(work, "a.py"), encoding="utf-8") as handle:
        assert handle.read() == "print('good')\n"


def test_restore_removes_new_files(tmp_path):
    """Agent 新建的文件要删掉。"""
    work = _make_tree(tmp_path / "work", {"a.py": "x\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    point = service.capture(work)

    with open(os.path.join(work, "new.py"), "w", encoding="utf-8") as handle:
        handle.write("boom\n")

    result = service.restore(point.id, work)

    assert result["deleted_count"] == 1
    assert not os.path.exists(os.path.join(work, "new.py"))


def test_plan_does_not_touch_files(tmp_path):
    """计划只算不动 —— 用户要先看再点头。"""
    work = _make_tree(tmp_path / "work", {"a.py": "good\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    point = service.capture(work)

    with open(os.path.join(work, "a.py"), "w", encoding="utf-8") as handle:
        handle.write("broken\n")

    plan = service.plan(point.id, work)

    assert plan["restored_count"] == 1
    # 文件还没被动。
    with open(os.path.join(work, "a.py"), encoding="utf-8") as handle:
        assert handle.read() == "broken\n"


def test_baseline_is_the_rollback_target(tmp_path):
    """基线安全点是默认的回滚目标。"""
    work = _make_tree(tmp_path / "work", {"a.py": "x\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    service.capture(work, label="基线安全点 · 2026-10-27 10:00:00")
    service.capture(work, label="红线拦截快照 · 10:05:00")

    baseline = service.baseline(workdir=work)

    assert baseline is not None
    assert baseline["label"].startswith("基线")


def test_restore_without_point_returns_error(tmp_path):
    """没有安全点时要说清楚，而不是崩掉。"""
    service = SafePointService(root=str(tmp_path / "points"))

    result = service.restore("", str(tmp_path))

    assert result["ok"] is False
    assert "安全点" in result["error"]


def test_restore_reports_elapsed(tmp_path):
    """回滚要报耗时 —— 用户等着的时候得知道多久。"""
    work = _make_tree(tmp_path / "work", {"a.py": "x\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    point = service.capture(work)

    result = service.restore(point.id, work)

    assert "elapsed" in result
    assert result["elapsed"] >= 0


def test_list_points_newest_first(tmp_path):
    """安全点列表新的在前。"""
    work = _make_tree(tmp_path / "work", {"a.py": "x\n"})
    service = SafePointService(root=str(tmp_path / "points"))
    first = service.capture(work, label="第一个")
    second = service.capture(work, label="第二个")

    points = service.list_points(workdir=work)

    assert len(points) == 2
    assert points[0]["id"] == second.id
    assert points[1]["id"] == first.id
