"""GUI 持久化工具测试（从旧项目移植的两个模块）。

覆盖：原子写、读失败回落默认值、夹取、清除。
"""

from __future__ import annotations

from src.gui.density import DENSITY_MAX, DENSITY_MIN, clamp_level
from src.gui.density_store import (
    clear_level,
    has_level,
    load_level,
    save_level,
)
from src.gui.layout_store import (
    DEFAULT_RATIO,
    MAX_RATIO,
    MIN_RATIO,
    clamp_ratio,
    load_ratio,
    reset_layout,
    save_ratio,
)


# ---------------------------------------------------------------------------
# layout_store
# ---------------------------------------------------------------------------
def test_save_and_load_ratio(tmp_path):
    path = tmp_path / "layout.json"

    assert save_ratio(42.0, path) is True
    assert load_ratio(path) == 42.0


def test_load_ratio_falls_back_on_missing_file(tmp_path):
    assert load_ratio(tmp_path / "nope.json") == DEFAULT_RATIO


def test_load_ratio_falls_back_on_broken_file(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    assert load_ratio(path) == DEFAULT_RATIO


def test_load_ratio_falls_back_on_wrong_shape(tmp_path):
    path = tmp_path / "layout.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    assert load_ratio(path) == DEFAULT_RATIO


def test_clamp_ratio_bounds():
    assert clamp_ratio(0) == MIN_RATIO
    assert clamp_ratio(100) == MAX_RATIO
    assert clamp_ratio("abc") == DEFAULT_RATIO
    assert clamp_ratio(float("nan")) == DEFAULT_RATIO
    assert clamp_ratio(None) == DEFAULT_RATIO


def test_reset_layout(tmp_path):
    path = tmp_path / "layout.json"
    save_ratio(42.0, path)

    assert reset_layout(path) is True
    assert load_ratio(path) == DEFAULT_RATIO


def test_save_leaves_no_temp_files(tmp_path):
    """原子写不该留下临时文件残骸。"""
    path = tmp_path / "layout.json"

    save_ratio(30.0, path)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "layout.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# density_store / density
# ---------------------------------------------------------------------------
def test_save_and_load_level(tmp_path):
    path = tmp_path / "density.json"

    assert save_level(3, path) is True
    assert load_level(path) == 3


def test_has_level_reflects_file_presence(tmp_path):
    path = tmp_path / "density.json"

    assert has_level(path) is False
    save_level(2, path)
    assert has_level(path) is True
    clear_level(path)
    assert has_level(path) is False


def test_load_level_falls_back_on_broken_file(tmp_path):
    path = tmp_path / "density.json"
    path.write_text("not json", encoding="utf-8")

    assert load_level(path) == DENSITY_MIN


def test_clamp_level_bounds():
    assert clamp_level(-5) == DENSITY_MIN
    assert clamp_level(99) == DENSITY_MAX
    assert clamp_level("abc") == DENSITY_MIN
    assert clamp_level(None) == DENSITY_MIN
