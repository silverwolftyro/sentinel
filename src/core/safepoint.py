"""安全点（Safe Point）—— 危险操作前的「后悔药」。

v2.0.0 之前，哨兵拦下一条红线命令时只能**挂起进程**：
危险命令不会执行，但 Agent **之前**已经把代码改乱了，哨兵管不着。

半夜挂机时这是最难受的场景：人不在电脑前，
Agent 跑偏了、把好好的代码改废了，等用户早上醒来，
看到的是一堆改坏的代码和一条「已拦截」的告警 ——
拦截拦住了最后那一刀，却救不回前面已经砍掉的树。

本模块提供两个能力：

====================== =================================================
能力                    说明
====================== =================================================
**快照（capture）**     在挂起进程**之前**，对工作目录做一次极速快照
**回滚（restore）**     把工作目录恢复成某个安全点的样子
====================== =================================================

「极速」是怎么做到的
==================

大目录直接全量复制要几十秒 —— 而守护者正在**等**着挂起进程，
用户正在**等**着弹窗。所以这里有三重降级：

1. **体积阈值** —— 目录总量超过 ``max_bytes``（默认 64 MB）
   就只备份「最近改动过」的文件（``recent_minutes``，默认 30 分钟），
   其余只记进清单；
2. **文件数阈值** —— 文件数超过 ``max_files``（默认 20000）同上；
3. **时间预算** —— 拷贝过程中一旦超过 ``max_seconds``（默认 3 秒）
   就**立刻停手**，剩下的文件只记清单。

无论怎么降级，**清单都是完整的** ——
「这个目录当时有哪些文件」这件事必须准确，
否则回滚时没法判断哪些文件是 Agent 新建的。

回滚的安全阀
============

* **先看计划，再动手** —— :meth:`SafePointService.plan` 只算不算，
  看板先弹「将恢复 N 个文件、删除 M 个新增文件」给用户看，
  用户点了确认才真的执行（权限小于用户）；
* **绝不越界** —— 所有路径都必须落在工作目录内，
  清单里出现 ``..`` 一律拒绝（防路径穿越）；
* **绝不动排除目录** —— ``.git`` / ``node_modules`` 这些不碰。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "DEFAULT_EXCLUDED_DIRS",
    "SafePoint",
    "SafePointService",
]

logger = logging.getLogger(__name__)

#: 默认**不**进快照的目录名。
#:
#: 这些目录要么是版本库（``.git``），要么是依赖缓存（``node_modules``）——
#: 它们要么太大，要么回滚了也没意义（重新装一次就有了）。
DEFAULT_EXCLUDED_DIRS: Tuple[str, ...] = (
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".sentinel",
)

#: 单个文件超过这个大小就不备份内容（只记清单）。
DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024


@dataclass
class SafePoint:
    """一个安全点。

    :param id: 安全点 ID（目录名）。
    :param label: 人话标签（界面上显示）。
    :param created_at: 创建时刻。
    :param workdir: 快照对应的**原始**工作目录。
    :param mode: ``full``（全量备份）或 ``incremental``（只备份近期改动）。
    :param files_count: 清单里记录的文件数。
    :param backed_up_count: 真正备份了内容的文件数。
    :param bytes: 备份占用的字节数。
    :param elapsed: 快照耗时（秒）。
    :param backup_dir: 备份目录。
    :param manifest_path: 清单文件路径。
    :param truncated: 是否因为时间预算**提前收工**。
    """

    id: str = ""
    label: str = ""
    created_at: float = 0.0
    workdir: str = ""
    mode: str = "full"
    files_count: int = 0
    backed_up_count: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    backup_dir: str = ""
    manifest_path: str = ""
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """转成普通字典（给 API / JSON 用）。"""
        return {
            "id": self.id,
            "label": self.label,
            "created_at": float(self.created_at),
            "workdir": self.workdir,
            "mode": self.mode,
            "files_count": int(self.files_count),
            "backed_up_count": int(self.backed_up_count),
            "bytes": int(self.bytes),
            "elapsed": round(float(self.elapsed), 3),
            "backup_dir": self.backup_dir,
            "manifest_path": self.manifest_path,
            "truncated": bool(self.truncated),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SafePoint":
        """从清单头还原。"""
        raw = dict(data or {})
        return cls(
            id=str(raw.get("id") or ""),
            label=str(raw.get("label") or ""),
            created_at=float(raw.get("created_at") or 0.0),
            workdir=str(raw.get("workdir") or ""),
            mode=str(raw.get("mode") or "full"),
            files_count=int(raw.get("files_count") or 0),
            backed_up_count=int(raw.get("backed_up_count") or 0),
            bytes=int(raw.get("bytes") or 0),
            elapsed=float(raw.get("elapsed") or 0.0),
            backup_dir=str(raw.get("backup_dir") or ""),
            manifest_path=str(raw.get("manifest_path") or ""),
            truncated=bool(raw.get("truncated")),
        )


class SafePointService:
    """安全点服务 —— 快照与回滚。

    :param root: 安全点存放目录（默认 ``logs/safepoints``）。
    :param max_bytes: 目录总量超过这个值就降级成增量模式。
    :param max_files: 文件数超过这个值就降级成增量模式。
    :param max_seconds: 单次快照的时间预算（秒）。
    :param recent_minutes: 增量模式下「最近改动过」的判定窗口（分钟）。
    :param excluded_dirs: 不进快照的目录名。
    """

    def __init__(
        self,
        *,
        root: str = "",
        max_bytes: int = 64 * 1024 * 1024,
        max_files: int = 20000,
        max_seconds: float = 3.0,
        recent_minutes: int = 30,
        excluded_dirs: Optional[Tuple[str, ...]] = None,
    ) -> None:
        self.root = os.path.abspath(
            str(root or os.path.join("logs", "safepoints"))
        )
        self.max_bytes = max(int(max_bytes or 0), 0)
        self.max_files = max(int(max_files or 0), 0)
        self.max_seconds = max(float(max_seconds or 0.0), 0.0)
        self.recent_minutes = max(int(recent_minutes or 0), 0)
        self.excluded_dirs = tuple(
            excluded_dirs if excluded_dirs is not None else DEFAULT_EXCLUDED_DIRS
        )

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def capture(self, workdir: str, *, label: str = "") -> SafePoint:
        """对工作目录做一次极速快照。

        **绝不抛异常** —— 快照失败绝不能挡住守护者挂起进程
        （审查侧异常一律降级，架构红线 R5）。

        :param workdir: 要快照的目录。
        :param label: 人话标签。
        :return: 安全点；失败时返回 ``id`` 为空的空对象。
        """
        started = time.monotonic()
        target = os.path.abspath(str(workdir or ""))
        point = SafePoint(created_at=time.time(), workdir=target)
        if not target or not os.path.isdir(target):
            point.label = str(label or "（目录不存在）")
            point.elapsed = time.monotonic() - started
            return point

        point.id = _make_point_id(point.created_at)
        point.label = str(label or f"安全点 {point.id}")
        backup_dir = os.path.join(self.root, point.id, "files")
        point.backup_dir = backup_dir
        point.manifest_path = os.path.join(self.root, point.id, "manifest.json")
        os.makedirs(backup_dir, exist_ok=True)

        try:
            entries = self._scan(target)
        except OSError as exc:
            logger.warning("扫描工作目录失败：%s: %s", type(exc).__name__, exc)
            point.elapsed = time.monotonic() - started
            return point

        total_bytes = sum(item["size"] for item in entries)
        recent_cutoff = time.time() - self.recent_minutes * 60
        big_dir = (
            (self.max_bytes and total_bytes > self.max_bytes)
            or (self.max_files and len(entries) > self.max_files)
        )
        point.mode = "incremental" if big_dir else "full"

        budget_hit = False
        backed_up = 0
        used_bytes = 0
        for item in entries:
            rel = item["rel"]
            record = {
                "size": item["size"],
                "mtime": item["mtime"],
                "backed_up": False,
                "sha256": "",
            }
            need_copy = True
            if point.mode == "incremental":
                need_copy = item["mtime"] >= recent_cutoff
            if need_copy and item["size"] > DEFAULT_MAX_FILE_BYTES:
                need_copy = False
            if need_copy and (
                self.max_seconds
                and (time.monotonic() - started) > self.max_seconds
            ):
                budget_hit = True
                need_copy = False
            if need_copy:
                try:
                    source = os.path.join(target, rel)
                    destination = os.path.join(backup_dir, rel)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    shutil.copy2(source, destination)
                    record["backed_up"] = True
                    record["sha256"] = _sha256(destination)
                    backed_up += 1
                    used_bytes += item["size"]
                except OSError as exc:
                    logger.debug(
                        "备份文件失败（已跳过）：%s: %s", rel, exc
                    )
            item["record"] = record

        point.files_count = len(entries)
        point.backed_up_count = backed_up
        point.bytes = used_bytes
        point.truncated = budget_hit
        point.elapsed = time.monotonic() - started

        manifest = {
            "version": 1,
            "point": point.to_dict(),
            "files": {
                item["rel"]: item["record"] for item in entries
            },
        }
        # v2.1.0：顺便算一下「相对上一次快照变了什么」。
        # ⚠️ 只比**清单**（路径 + 大小 + sha256），不读文件内容 ——
        # 内容级的 diff 要到用户点【查看改动】时才算，
        # 否则会把这个「极速」快照拖慢。
        try:
            manifest["changes"] = self._changes_vs_previous(point.id, entries)
        except Exception as exc:  # noqa: BLE001 —— 变动清单失败只降级
            logger.debug("算变动清单失败（已降级）：%s: %s", type(exc).__name__, exc)
            manifest["changes"] = {"modified": [], "added": [], "removed": []}
        try:
            with open(point.manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.warning("写安全点清单失败：%s: %s", type(exc).__name__, exc)
            point.id = ""
        return point

    def _changes_vs_previous(
        self, point_id: str, entries: List[Dict[str, Any]]
    ) -> Dict[str, List[str]]:
        """算「相对上一次快照，这个目录变了什么」。

        ⚠️ 只比**清单**，不读文件内容 —— 所以它很便宜，
        不会把「极速」快照拖慢。内容级 diff 由 :meth:`diff` 按需计算。

        :param point_id: 当前快照 ID（用来排除它自己）。
        :param entries: 本次扫描到的文件条目。
        :return: ``{"modified": [...], "added": [...], "removed": [...]}``。
        """
        previous = self.latest(workdir="", exclude_id=point_id)
        if not previous:
            return {"modified": [], "added": [], "removed": []}
        old_files = self._load_manifest(previous["id"]).get("files", {})
        new_files = {
            item["rel"]: item["record"] for item in entries
        }
        modified: List[str] = []
        added: List[str] = []
        removed: List[str] = []
        for rel, record in new_files.items():
            old = old_files.get(rel)
            if old is None:
                added.append(rel)
                continue
            old_sha = str(old.get("sha256") or "")
            new_sha = str(record.get("sha256") or "")
            if old_sha and new_sha and old_sha != new_sha:
                modified.append(rel)
            elif (
                not old_sha
                or not new_sha
            ) and int(old.get("size") or 0) != int(record.get("size") or 0):
                # 有一边没备下内容（增量模式 / 超大文件）——
                # 只能拿大小当粗略依据。
                modified.append(rel)
        removed.extend(rel for rel in old_files if rel not in new_files)
        return {
            "modified": sorted(modified),
            "added": sorted(added),
            "removed": sorted(removed),
        }

    def _scan(self, root: str) -> List[Dict[str, Any]]:
        """遍历目录，列出所有该进快照的文件。

        :param root: 工作目录。
        :return: 文件条目列表（``rel`` / ``size`` / ``mtime``）。
        """
        entries: List[Dict[str, Any]] = []
        excluded = set(self.excluded_dirs)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name for name in dirnames if name not in excluded
            ]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                try:
                    stat = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                entries.append(
                    {"rel": rel, "size": int(stat.st_size), "mtime": stat.st_mtime}
                )
        return entries

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_points(self, *, workdir: str = "") -> List[Dict[str, Any]]:
        """列出全部安全点（新的在前）。

        :param workdir: 只看这个工作目录的安全点；留空看全部。
        :return: 安全点字典列表。
        """
        points: List[Dict[str, Any]] = []
        if not os.path.isdir(self.root):
            return points
        target = os.path.abspath(str(workdir)) if workdir else ""
        for name in sorted(os.listdir(self.root), reverse=True):
            manifest_path = os.path.join(self.root, name, "manifest.json")
            if not os.path.exists(manifest_path):
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
            except (OSError, ValueError):
                continue
            head = loaded.get("point")
            if not isinstance(head, dict):
                continue
            if target and os.path.abspath(
                str(head.get("workdir") or "")
            ) != target:
                continue
            points.append(head)
        return points

    def get(self, point_id: str) -> Optional[Dict[str, Any]]:
        """按 ID 取一个安全点。

        :param point_id: 安全点 ID。
        :return: 安全点字典；找不到返回 ``None``。
        """
        wanted = str(point_id or "").strip()
        if not wanted:
            return None
        for item in self.list_points():
            if str(item.get("id") or "") == wanted:
                return item
        return None

    def latest(
        self, *, workdir: str = "", exclude_id: str = ""
    ) -> Optional[Dict[str, Any]]:
        """最近一个安全点。

        :param workdir: 只看这个工作目录的（留空 = 全部）。
        :param exclude_id: 排除掉某个 ID —— 打快照时用它排除「自己」，
            这样 ``changes`` 比的就是**上一次**快照。
        :return: 安全点字典；没有就返回 ``None``。
        """
        skip = str(exclude_id or "").strip()
        points = [
            item
            for item in self.list_points(workdir=workdir)
            if str(item.get("id") or "") != skip
        ]
        return points[0] if points else None

    def baseline(self, *, workdir: str = "") -> Optional[Dict[str, Any]]:
        """**基线安全点** —— 工作目录的「起点」快照。

        基线是**回滚的目标**：拦截发生时生效的安全点就是它
        （除非用户手动换过）。Agent 在这次任务里改乱的东西，
        回滚到基线就都没了。

        :param workdir: 工作目录。
        :return: 安全点字典；没有就返回 ``None``。
        """
        points = self.list_points(workdir=workdir)
        for item in points:
            if str(item.get("label") or "").startswith("基线"):
                return item
        return points[-1] if points else None

    # ------------------------------------------------------------------
    # 差异
    # ------------------------------------------------------------------
    def diff(
        self,
        point_id: str = "",
        workdir: str = "",
        *,
        max_files: int = 20,
        max_bytes: int = 256 * 1024,
    ) -> Dict[str, Any]:
        """算「这个安全点 vs 现在的目录」的文件级差异。

        给看板的【查看改动】用 —— 让用户**直观看到** Agent 到底改了哪些
        文件、加了什么、删了什么，再决定放行还是回滚。

        ⚠️ 内容级 diff 是**按需**算的（用户点按钮才算）——
        快照本身只比清单，不读内容，否则「极速」就没了。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录；留空表示用安全点自己记录的。
        :param max_files: 最多返回几个文件的 diff。
        :param max_bytes: 单个文件参与 diff 的最大字节数。
        :return: 差异字典。
        """
        point = self.get(point_id) if point_id else self.baseline(
            workdir=workdir
        )
        if not point:
            return {
                "ok": False,
                "error": "没有可对比的安全点。",
                "files": [],
            }
        target = os.path.abspath(str(workdir or point.get("workdir") or ""))
        manifest = self._load_manifest(str(point.get("id") or ""))
        files = manifest.get("files") if isinstance(manifest, dict) else {}
        if not isinstance(files, dict):
            files = {}
        backup_dir = str(point.get("backup_dir") or "")

        changed: List[Dict[str, Any]] = []
        added: List[str] = []
        removed: List[str] = []
        skipped: List[str] = []

        for rel, record in files.items():
            if not isinstance(record, dict):
                continue
            current_path = os.path.join(target, rel)
            exists_now = os.path.exists(current_path)
            if not exists_now:
                removed.append(rel)
                continue
            old_sha = str(record.get("sha256") or "")
            new_sha = ""
            try:
                new_sha = _sha256(current_path)
            except OSError:
                new_sha = ""
            if old_sha and new_sha and old_sha == new_sha:
                continue
            changed.append(rel)

        current_files = set(self._current_files(target))
        added.extend(rel for rel in current_files if rel not in files)

        # 内容级 diff —— 只对「两边都有内容可比」的文件做。
        details: List[Dict[str, Any]] = []
        for rel in sorted(changed)[: max(0, int(max_files))]:
            old_path = os.path.join(backup_dir, rel.replace("/", os.sep))
            current_path = os.path.join(target, rel)
            detail = self._file_diff(rel, old_path, current_path, max_bytes)
            if detail is not None:
                details.append(detail)
            else:
                skipped.append(rel)

        return {
            "ok": True,
            "point": point,
            "workdir": target,
            "modified": sorted(changed),
            "added": sorted(added),
            "removed": sorted(removed),
            "files": details,
            "skipped": skipped,
            "modified_count": len(changed),
            "added_count": len(added),
            "removed_count": len(removed),
            "truncated": len(changed) > int(max_files),
        }

    def _file_diff(
        self, rel: str, old_path: str, current_path: str, max_bytes: int
    ) -> Optional[Dict[str, Any]]:
        """算单个文件的 unified diff。

        :param rel: 相对路径（显示用）。
        :param old_path: 旧内容路径（快照备份）。
        :param current_path: 新内容路径（当前工作目录）。
        :param max_bytes: 单个文件参与 diff 的最大字节数。
        :return: 差异字典；没法比时返回 ``None``。
        """
        old_text = _read_text(old_path, max_bytes)
        new_text = _read_text(current_path, max_bytes)
        if old_text is None or new_text is None:
            return None
        if old_text == new_text:
            return None
        diff_lines = list(
            difflib.unified_diff(
                old_text.splitlines(),
                new_text.splitlines(),
                fromfile=f"安全点/{rel}",
                tofile=f"现在/{rel}",
                lineterm="",
                n=3,
            )
        )
        if not diff_lines:
            return None
        added = sum(
            1
            for line in diff_lines
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in diff_lines
            if line.startswith("-") and not line.startswith("---")
        )
        return {
            "path": rel,
            "added": added,
            "removed": removed,
            "lines": diff_lines,
        }

    # ------------------------------------------------------------------
    # 回滚
    # ------------------------------------------------------------------
    def plan(self, point_id: str, workdir: str = "") -> Dict[str, Any]:
        """算出回滚**将要**做什么（只算不动）。

        看板先把这个计划给用户看，用户点了确认才执行 ——
        权限小于用户。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录；留空表示用安全点自己记录的。
        :return: 计划字典。
        """
        point = self.get(point_id) if point_id else self.baseline(
            workdir=workdir
        )
        if not point:
            return {
                "ok": False,
                "error": "没有可回滚的安全点。",
                "restored": [],
                "deleted": [],
            }
        target = os.path.abspath(str(workdir or point.get("workdir") or ""))
        manifest = self._load_manifest(str(point.get("id") or ""))
        files = manifest.get("files") if isinstance(manifest, dict) else {}
        if not isinstance(files, dict):
            files = {}

        restored: List[str] = []
        deleted: List[str] = []
        for rel, record in files.items():
            if not isinstance(record, dict) or not record.get("backed_up"):
                continue
            current = os.path.join(target, rel)
            if not os.path.exists(current):
                restored.append(rel)
                continue
            try:
                stat = os.stat(current)
                if int(stat.st_size) != int(record.get("size") or 0):
                    restored.append(rel)
            except OSError:
                restored.append(rel)

        deleted.extend(
            rel for rel in self._current_files(target) if rel not in files
        )

        return {
            "ok": True,
            "point": point,
            "workdir": target,
            "restored": sorted(restored),
            "deleted": sorted(deleted),
            "restored_count": len(restored),
            "deleted_count": len(deleted),
        }

    def restore(self, point_id: str = "", workdir: str = "") -> Dict[str, Any]:
        """把工作目录恢复成某个安全点的样子。

        **绝不抛异常** —— 回滚失败只降级（返回 ``ok=False``）。

        :param point_id: 安全点 ID；留空表示用基线。
        :param workdir: 工作目录；留空表示用安全点自己记录的。
        :return: 结果字典（含耗时与恢复/删除的文件数）。
        """
        started = time.monotonic()
        plan = self.plan(point_id, workdir)
        if not plan.get("ok"):
            return plan
        point = plan.get("point") or {}
        target = str(plan.get("workdir") or "")
        backup_dir = str(point.get("backup_dir") or "")
        manifest = self._load_manifest(str(point.get("id") or ""))
        files = manifest.get("files") if isinstance(manifest, dict) else {}
        if not isinstance(files, dict):
            files = {}

        restored: List[str] = []
        failed: List[str] = []
        for rel in plan.get("restored") or []:
            record = files.get(rel)
            if not isinstance(record, dict):
                continue
            source = os.path.join(backup_dir, rel.replace("/", os.sep))
            destination = os.path.join(target, rel.replace("/", os.sep))
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
                restored.append(rel)
            except OSError as exc:
                logger.warning("回滚文件失败：%s: %s", rel, exc)
                failed.append(rel)

        deleted: List[str] = []
        for rel in plan.get("deleted") or []:
            full = os.path.join(target, rel.replace("/", os.sep))
            if not _inside(target, full):
                continue
            try:
                os.remove(full)
                deleted.append(rel)
            except OSError as exc:
                logger.warning("删除新增文件失败：%s: %s", rel, exc)

        elapsed = time.monotonic() - started
        return {
            "ok": not failed,
            "point": point,
            "workdir": target,
            "restored": sorted(restored),
            "deleted": sorted(deleted),
            "failed": sorted(failed),
            "restored_count": len(restored),
            "deleted_count": len(deleted),
            "elapsed": round(elapsed, 3),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _current_files(self, root: str) -> List[str]:
        """列出当前工作目录里的文件（相对路径，斜杠分隔）。"""
        if not root or not os.path.isdir(root):
            return []
        excluded = set(self.excluded_dirs)
        names: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in excluded]
            for name in filenames:
                full = os.path.join(dirpath, name)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                names.append(rel)
        return names

    def _load_manifest(self, point_id: str) -> Dict[str, Any]:
        """读安全点清单。"""
        manifest_path = os.path.join(self.root, str(point_id or ""), "manifest.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            return {}


def _inside(root: str, path: str) -> bool:
    """判断 ``path`` 是否落在 ``root`` 之内（防路径穿越）。

    :param root: 根目录。
    :param path: 待检查路径。
    :return: 在里面返回 ``True``。
    """
    try:
        root_abs = os.path.abspath(root)
        path_abs = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    if path_abs == root_abs:
        return True
    return path_abs.startswith(root_abs + os.sep)


def _sha256(path: str) -> str:
    """算文件的 SHA-256（用来校验备份完整性）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 256), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _read_text(path: str, max_bytes: int) -> Optional[str]:
    """读一个文件当文本（给 diff 用）。

    ⚠️ 二进制文件（读出来有 ``\\x00``）返回 ``None`` ——
    给二进制做行级 diff 只会得到一堆乱码，用户看了也白看。

    :param path: 文件路径。
    :param max_bytes: 最多读多少字节。
    :return: 文本；读不了 / 是二进制时返回 ``None``。
    """
    try:
        if not os.path.isfile(path):
            return None
        size = os.path.getsize(path)
        if max_bytes and size > max_bytes:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _make_point_id(stamp: float) -> str:
    """生成安全点 ID。

    :param stamp: 时间戳。
    :return: 形如 ``20261027-174512-123456`` 的 ID。
    """
    return "{}-{:06d}".format(
        time.strftime("%Y%m%d-%H%M%S", time.localtime(stamp)),
        int((stamp % 1.0) * 1_000_000),
    )
