"""Agent 扩展的**数据层**。

这个包把「怎么启动一个 Agent CLI」这件事彻底数据化 ——
新增一个 Agent 只需要往 :data:`cloud_agents.json` 里加一条，
**不需要改任何 Python 代码**。

设计来源
========

参照开源项目 ``agent_bridge`` 的 ``bridge/registry.py``（``AgentSpec``
数据类）与 ``bridge/provider_manifest.py``（清单校验协议），
在哨兵侧重新实现了一份**零依赖**的等价物。

与上游的差异（都是刻意为之）
----------------------------

1. **别名内联到每条 Agent**：上游把别名放在全局 ``ALIASES`` 字典里，
   拆成两份数据容易漂移；这里每条 Agent 自带 ``aliases``，
   归一表在加载时合成。
2. **展示名保留哨兵本地译名**：``label`` 用哨兵自己的中文名，
   ``source_label`` 保留上游原文，便于回溯。
3. **未知字段不报错**：上游的清单解析器同样忽略未知键，
   这里保持一致 —— 前向兼容比严格校验更重要。

模块入口
========

* :func:`load_agents` —— 读文件 + 校验，返回 ``id → AgentProfile``。
* :func:`canonical_agent` —— 把任意写法（id / 别名 / 可执行名）
  归一到规范 id。
* :func:`build_argv` —— **统一的命令行构造器**。
* :func:`alias_table` / :func:`binary_table` / :func:`label_table`
  —— 供雷达面板直接消费的三张表。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_DATA_PATH",
    "EXTRA_BIN_DIRS",
    "JSON_DOCUMENT_SHAPES",
    "SCHEMA_VERSION",
    "VALID_JSON_SHAPES",
    "VALID_MODES",
    "AgentConfigError",
    "AgentProfile",
    "alias_table",
    "binary_table",
    "build_argv",
    "canonical_agent",
    "label_table",
    "load_agents",
    "log_dir_table",
    "resolve_executable",
]
#: 清单结构版本。字段语义发生不兼容变更时递增。
SCHEMA_VERSION = 1

#: 默认清单路径 —— 与本模块同目录。
#:
#: ⚠️ 打包成 exe 后 ``__file__`` 落在 PyInstaller 的解包目录里，
#: 而 ``cloud_agents.json`` 是通过 ``--add-data`` 放进同一个目录的，
#: 所以这个相对定位在**源码运行**与**打包运行**下都成立。
DEFAULT_DATA_PATH = Path(__file__).with_name("cloud_agents.json")

#: 允许的启动形态。
#:
#: * ``flag``      —— 正文开关**吃值**：``cli -p <正文>``
#: * ``switch``    —— 正文开关是**布尔**的：``cli -p <正文>``
#:   （argv 形态与 ``flag`` 相同；分开登记是为了避免
#:   把不吃值的开关误写成取值开关）
#: * ``positional`` —— 正文作**位置参数**：``cli <正文>``
VALID_MODES: Tuple[str, ...] = ("flag", "switch", "positional")

#: 机器可读输出的形态。
#:
#: * ``none``     —— 没有机器可读输出，只有纯文本；
#: * ``document`` —— stdout **整体**是一个 JSON 文档；
#: * ``jsonl``    —— 每行一个 JSON 对象；
#: * ``stream``   —— **事件流**（每行一个事件，但不是完整文档）。
#:
#: ⚠️ 只有 ``document`` 与 ``jsonl`` 值得打开 ``json_args`` ——
#: ``stream`` 按文档解析必然失败，作者也建议「默认别开」。
VALID_JSON_SHAPES: Tuple[str, ...] = (
    "none",
    "document",
    "jsonl",
    "stream",
)

#: 值得打开 ``json_args`` 的形态。
JSON_DOCUMENT_SHAPES: Tuple[str, ...] = ("document", "jsonl")

#: 规范 id 的合法写法。
_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

#: PATH 之外还要兜底搜索的目录。
#:
#: 这些是 ``bun`` / ``npm`` / 发行包常见的安装位置 ——
#: Windows 上 ``shutil.which`` 找不到它们，但 CLI 明明装在那儿。
EXTRA_BIN_DIRS: Tuple[str, ...] = (
    "~/.bun/bin",
    "~/AppData/Roaming/npm",
    "~/.local/bin",
    "~/.stepcode/bin",
    "~/AppData/Local/hermes/bin",
    "~/.qwenpaw/bin",
    "~/AppData/Local/AtomCode",
    "~/AppData/Local/evox/bin",
    "/usr/local/bin",
)

#: 扩展名候选（按优先级）。
#:
#: ⚠️ ``.exe`` 必须排在 ``.bunx`` 前面 —— ``CreateProcess`` 认不了
#: 扩展名脚本，硬塞给它只会报「不是有效的 Win32 应用程序」。
_EXE_SUFFIXES: Tuple[str, ...] = (
    ".exe",
    ".cmd",
    ".bat",
    ".ps1",
    "",
)


class AgentConfigError(ValueError):
    """Agent 清单本身有问题（格式非法、字段缺失、id 冲突等）。"""


@dataclass(frozen=True)
class AgentProfile:
    """一个 Agent CLI 的启动档案。

    :param id: 规范 id（``^[a-z][a-z0-9-]{1,31}$``）。
    :param label: 展示名（哨兵本地译名）。
    :param source_label: 上游原文展示名，仅供回溯。
    :param binary: PATH 上查找的可执行名。
    :param mode: 启动形态，见 :data:`VALID_MODES`。
    :param fixed_args: 固定前缀参数（拼在可执行名之后）。
    :param prompt_flag: 正文开关；``positional`` 形态为空。
    :param model_flag: 模型参数名；为空表示该 CLI 不支持选模型。
    :param json_args: 机器可读输出开关。
    :param json_shape: 机器可读输出的**形态** ——
        ``none`` / ``document`` / ``jsonl`` / ``stream``。
        决定「要不要开这个开关」：``stream`` 是事件流，
        按文档解析会失败，作者建议默认别开。
    :param json_shape_note: 形态说明（纯文档）。
    :param version_args: 版本探测参数。
    :param aliases: 别名（归一到本 Agent）。
    :param auth_patterns: 本 Agent 专属的**鉴权失败**措辞（正则）。
    :param fail_patterns: 本 Agent 专属的**运行期失败**措辞（正则）——
        与鉴权失败分开，因为处置动作不同（去起服务，不是去登录）。
    :param login_hint: 登录指引。
    :param install_cmd: 一键安装命令。
    :param install_hint: 安装指引。
    :param notes: 备注。
    :param hints: 运维提示。
    :param log_dirs: **已知会话日志目录**（相对家目录或绝对路径）。

        为什么需要它（v4.3.0）：Agent CLI 常常**不往自己的 cwd 写日志** ——
        Claude Code 写 ``~/.claude/projects/<项目slug>/``，
        Codex 写 ``~/.codex/sessions/<年>/<月>/<日>/``。
        雷达原先只在 cwd 下找 ``.jsonl`` / ``.log``，
        找不到就把进程标成「疑似挂起」—— **假阳性**。
        登记了 ``log_dirs`` 之后，cwd 下没有就再去这些目录找。

        ⚠️ 只登记**有依据**的目录 —— 凭印象填一个不存在的路径，
        只会让扫描白跑一圈。
    """

    id: str
    label: str
    binary: str
    mode: str
    source_label: str = ""
    fixed_args: Tuple[str, ...] = ()
    prompt_flag: Optional[str] = None
    model_flag: Optional[str] = None
    json_args: Tuple[str, ...] = ()
    json_shape: str = "none"
    json_shape_note: str = ""
    version_args: Tuple[str, ...] = ("--version",)
    aliases: Tuple[str, ...] = ()
    auth_patterns: Tuple[str, ...] = ()
    fail_patterns: Tuple[str, ...] = ()
    login_hint: str = ""
    install_cmd: str = ""
    install_hint: str = ""
    notes: str = ""
    hints: Tuple[str, ...] = ()
    log_dirs: Tuple[str, ...] = ()

    @property
    def exec_names(self) -> Tuple[str, ...]:
        """``shutil.which`` 的候选名（按优先级）。

        Windows 上这些 CLI 多是 npm / bun 装的 ``.cmd`` 垫片，
        ``Popen(["mcode", ...])`` 解析不到（它只认真可执行文件），
        所以要按 PATHEXT 逐个试。

        :return: 候选可执行名列表。
        """
        return (
            self.binary,
            self.binary + ".cmd",
            self.binary + ".exe",
            self.binary + ".bat",
        )

    @property
    def supports_json_document(self) -> bool:
        """这个 Agent 的机器可读输出**能不能按文档解析**。

        ``stream`` 形态（事件流）返回 ``False`` ——
        按文档解析必然失败，作者也建议「默认别开」。

        :return: 可以按文档解析时返回 ``True``。
        """
        return self.json_shape in JSON_DOCUMENT_SHAPES


def _require_str(
    raw: Dict[str, object],
    key: str,
    *,
    where: str,
    default: str = "",
) -> str:
    """取一个字符串字段。

    :param raw: 原始字典。
    :param key: 字段名。
    :param where: 出错时的定位串。
    :param default: 缺省值。
    :return: 字段值（缺省时返回 ``default``）。
    :raises AgentConfigError: 字段存在但不是字符串。
    """
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise AgentConfigError(f"{where} 的 {key} 必须是字符串")
    return value.strip()


def _require_str_list(
    raw: Dict[str, object],
    key: str,
    *,
    where: str,
) -> Tuple[str, ...]:
    """取一个字符串数组字段。

    :param raw: 原始字典。
    :param key: 字段名。
    :param where: 出错时的定位串。
    :return: 元组形式的字符串列表。
    :raises AgentConfigError: 字段类型不对。
    """
    value = raw.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        out: List[str] = []
        for item in value:
            if not isinstance(item, str):
                raise AgentConfigError(
                    f"{where} 的 {key} 只允许字符串数组"
                )
            out.append(item.strip())
        return tuple(out)
    raise AgentConfigError(f"{where} 的 {key} 必须是字符串或字符串数组")


def _parse_agent(
    raw: object,
    *,
    where: str,
) -> AgentProfile:
    """把一条原始记录解析成 :class:`AgentProfile`。

    :param raw: 原始记录（字典）。
    :param where: 出错时的定位串。
    :return: 解析好的档案。
    :raises AgentConfigError: 字段缺失或取值非法。
    """
    if not isinstance(raw, dict):
        raise AgentConfigError(f"{where} 必须是对象")

    agent_id = _require_str(raw, "id", where=where)
    if not _ID_RE.match(agent_id):
        raise AgentConfigError(
            f"{where} 的 id={agent_id!r} 非法，"
            "须匹配 ^[a-z][a-z0-9-]{1,31}$"
        )

    binary = _require_str(raw, "binary", where=where)
    if not binary:
        raise AgentConfigError(f"{where} 缺少 binary")

    mode = _require_str(raw, "mode", where=where)
    if mode not in VALID_MODES:
        raise AgentConfigError(
            f"{where} 的 mode={mode!r} 非法，可选：{'/'.join(VALID_MODES)}"
        )

    prompt_flag = _require_str(raw, "prompt_flag", where=where) or None
    if mode in ("flag", "switch") and not prompt_flag:
        raise AgentConfigError(
            f"{where} 是 mode={mode}，必须声明 prompt_flag"
        )
    if mode == "positional" and prompt_flag:
        raise AgentConfigError(
            f"{where} 是 mode=positional，不能声明 prompt_flag"
        )

    label = _require_str(raw, "label", where=where) or agent_id

    json_args = _require_str_list(raw, "json_args", where=where)
    json_shape = _require_str(raw, "json_shape", where=where) or "none"
    if json_shape not in VALID_JSON_SHAPES:
        raise AgentConfigError(
            f"{where} 的 json_shape={json_shape!r} 非法，可选："
            f"{'/'.join(VALID_JSON_SHAPES)}"
        )
    if json_shape != "none" and not json_args:
        raise AgentConfigError(
            f"{where} 声明了 json_shape={json_shape}，却没给 json_args"
        )
    if json_shape == "none" and json_args:
        raise AgentConfigError(
            f"{where} 给了 json_args，就必须声明 json_shape"
        )

    return AgentProfile(
        id=agent_id,
        label=label,
        source_label=_require_str(raw, "source_label", where=where),
        binary=binary,
        mode=mode,
        fixed_args=_require_str_list(raw, "fixed_args", where=where),
        prompt_flag=prompt_flag,
        model_flag=_require_str(raw, "model_flag", where=where) or None,
        json_args=json_args,
        json_shape=json_shape,
        json_shape_note=_require_str(raw, "json_shape_note", where=where),
        version_args=_require_str_list(raw, "version_args", where=where),
        aliases=_require_str_list(raw, "aliases", where=where),
        auth_patterns=_require_str_list(raw, "auth_patterns", where=where),
        fail_patterns=_require_str_list(raw, "fail_patterns", where=where),
        login_hint=_require_str(raw, "login_hint", where=where),
        install_cmd=_require_str(raw, "install_cmd", where=where),
        install_hint=_require_str(raw, "install_hint", where=where),
        notes=_require_str(raw, "notes", where=where),
        hints=_require_str_list(raw, "hints", where=where),
        log_dirs=_require_str_list(raw, "log_dirs", where=where),
    )


def load_agents(
    path: Optional[os.PathLike] = None,
    *,
    strict: bool = True,
) -> Dict[str, AgentProfile]:
    """读清单并校验。

    :param path: 清单路径；缺省用 :data:`DEFAULT_DATA_PATH`。
    :param strict: 清单缺失或损坏时是否抛异常。
        ``False`` 时返回空字典并记一条 ``warning`` ——
        打包成 exe 后清单万一没被带上，哨兵要能**降级**而不是起不来。
    :return: ``id → AgentProfile`` 的映射。
    :raises AgentConfigError: ``strict`` 为真且清单有问题。
    """
    target = Path(path) if path is not None else DEFAULT_DATA_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if strict:
            raise AgentConfigError(f"找不到 Agent 清单：{target}") from None
        logger.warning("找不到 Agent 清单：%s（已降级为空表）", target)
        return {}
    except (OSError, ValueError) as exc:
        if strict:
            raise AgentConfigError(
                f"Agent 清单读取失败：{target}（{exc}）"
            ) from exc

    if not isinstance(raw, dict):
        raise AgentConfigError(f"{target} 顶层必须是对象")

    schema = int(raw.get("schema") or 0)
    if schema != SCHEMA_VERSION:
        raise AgentConfigError(
            f"{target} 的 schema={schema}，本模块只认 {SCHEMA_VERSION}"
        )

    entries = raw.get("agents")
    if not isinstance(entries, list):
        raise AgentConfigError(f"{target} 缺少 agents 数组")

    agents: Dict[str, AgentProfile] = {}
    alias_owner: Dict[str, str] = {}
    for index, item in enumerate(entries):
        profile = _parse_agent(item, where=f"{target} 第 {index + 1} 条")
        if profile.id in agents:
            raise AgentConfigError(
                f"{target} 里 id={profile.id!r} 重复"
            )
        agents[profile.id] = profile
        for alias in profile.aliases:
            owner = alias_owner.get(alias)
            if owner is not None and owner != profile.id:
                raise AgentConfigError(
                    f"{target} 里别名 {alias!r} 同时属于 "
                    f"{owner} 与 {profile.id}"
                )
            alias_owner[alias] = profile.id

    logger.info(
        "已加载 Agent 清单 %s：%d 个 Agent，别名 %d 个",
        target.name,
        len(agents),
        len(alias_owner),
    )
    return agents


def canonical_agent(
    name: str,
    agents: Optional[Dict[str, AgentProfile]] = None,
) -> Optional[str]:
    """把任意写法归一到规范 id。

    判定顺序：规范 id → 别名 → 可执行名。

    :param name: 用户 / 面板给的名字。
    :param agents: Agent 表；缺省用 :func:`load_agents` 的结果。
    :return: 规范 id；认不出时返回 ``None``。
    """
    key = str(name or "").strip().lower()
    if not key:
        return None
    table = agents if agents is not None else load_agents(strict=False)
    if key in table:
        return key
    for agent_id, profile in table.items():
        if key in profile.aliases:
            return agent_id
        if key == profile.binary.lower():
            return agent_id
    return None


def alias_table(
    agents: Dict[str, AgentProfile],
) -> Dict[str, str]:
    """别名 → 规范 id 的归一表。

    :param agents: Agent 表。
    :return: 归一表。
    """
    table: Dict[str, str] = {}
    for agent_id, profile in agents.items():
        for alias in profile.aliases:
            table[alias.lower()] = agent_id
    return table


def binary_table(
    agents: Dict[str, AgentProfile],
) -> Tuple[str, ...]:
    """可执行名元组（去重、保持清单顺序）。

    :param agents: Agent 表。
    :return: 可执行名元组。
    """
    seen: List[str] = []
    for profile in agents.values():
        if profile.binary not in seen:
            seen.append(profile.binary)
    return tuple(seen)


def label_table(
    agents: Dict[str, AgentProfile],
) -> Dict[str, str]:
    """规范 id → 展示名。

    :param agents: Agent 表。
    :return: 展示名表。
    """
    return {agent_id: profile.label for agent_id, profile in agents.items()}


def log_dir_table(
    agents: Dict[str, AgentProfile],
) -> Dict[str, Tuple[str, ...]]:
    """规范 id → 已知会话日志目录。

    雷达在 cwd 下找不到日志时，会去这里登记的目录里再找一遍
    （见 :func:`src.proc_tree.find_log_for_process`）。

    :param agents: Agent 表。
    :return: ``id → 目录清单`` 的映射；没有登记目录的 Agent 不出现。
    """
    table: Dict[str, Tuple[str, ...]] = {}
    for agent_id, profile in agents.items():
        if profile.log_dirs:
            table[agent_id] = profile.log_dirs
    return table


def build_argv(
    profile: AgentProfile,
    task: str,
    *,
    exe: Optional[str] = None,
    model: Optional[str] = None,
    json_output: bool = False,
    extra_args: Sequence[str] = (),
    include_exe: bool = True,
) -> List[str]:
    """拼出 Agent 的命令行。

    拼装顺序（与上游 ``bridge/registry.py`` 的 ``build_argv`` 一致）::

        [可执行名] + fixed_args
                    + ([prompt_flag, 正文] | [正文])
                    + json_args（仅当 json_output）
                    + ([model_flag, 模型]（仅当给了 model）)
                    + extra_args

    ⚠️ **正文的位置不固定** —— ``positional`` 形态下正文紧跟
    ``fixed_args``，而 ``flag`` / ``switch`` 形态下正文跟在开关后面。
    哨兵自己的 ``mcode`` / ``claude`` / ``codex`` 约定把正文放**最后**，
    两者不能混用。

    :param profile: Agent 档案。
    :param task: 任务正文。
    :param exe: 可执行文件路径；缺省用 ``profile.binary``。
    :param model: 模型名；为空表示不传。
    :param json_output: 是否要求机器可读输出。
    :param extra_args: 额外参数（追加在最后）。
    :param include_exe: 是否把可执行名放进结果。
    :return: 命令行参数列表。
    """
    argv: List[str] = []
    if include_exe:
        argv.append(exe or profile.binary)
    argv.extend(profile.fixed_args)

    if profile.mode in ("flag", "switch"):
        argv.append(profile.prompt_flag or "")
        argv.append(task)
    else:
        argv.append(task)

    if json_output and profile.json_args:
        argv.extend(profile.json_args)
    if model and profile.model_flag:
        argv.extend([profile.model_flag, model])

    argv.extend(str(item) for item in extra_args)
    return argv


def resolve_executable(
    binary: str,
    *,
    extra_dirs: Sequence[str] = EXTRA_BIN_DIRS,
) -> Optional[str]:
    """在 PATH 与兜底目录里找可执行文件。

    ⚠️ **``.exe`` 必须优先于无扩展名脚本** —— Windows 的
    ``CreateProcess`` 认不了 ``#!`` 脚本，把 ``.bunx`` 交给它只会报
    「不是有效的 Win32 应用程序」。

    :param binary: 可执行名。
    :param extra_dirs: PATH 之外的兜底目录。
    :return: 可执行文件路径；找不到时返回 ``None``。
    """
    found = shutil.which(binary)
    if found:
        return found

    for raw_dir in extra_dirs:
        directory = Path(os.path.expanduser(str(raw_dir)))
        if not directory.is_dir():
            continue
        for suffix in _EXE_SUFFIXES:
            candidate = directory / (binary + suffix)
            if candidate.is_file():
                return str(candidate)
    return None
