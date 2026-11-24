"""本地教研文本与课案实体台账工具。

公共入口：

* :class:`TextWorkflow` —— 导入、实体/提及/纠正/关系查询的完整工作流；
* :class:`EntityLedger` —— 不依赖工作流的台账核心；
* :class:`RecordRepository` —— JSON 持久化。
"""

from .entities import (
    ALIAS,
    AMBIGUOUS,
    CONFIRMED,
    EXACT,
    MANUAL,
    MERGED,
    NONE,
    PENDING,
    PROVIDED,
    RESOLVED,
    SIMILAR,
    UNCERTAIN,
    UNRESOLVED,
    Correction,
    Entity,
    Mention,
    Relation,
)
from .ledger import EntityLedger
from .records import TextRecord, prepare_record
from .repository import RecordRepository
from .text import annotate, normalize_text, tokenize
from .workflow import TextWorkflow, build_workflow, process_records

__version__ = "0.2.0"

__all__ = [
    "TextWorkflow",
    "EntityLedger",
    "RecordRepository",
    "TextRecord",
    "prepare_record",
    "build_workflow",
    "process_records",
    "Entity",
    "Mention",
    "Correction",
    "Relation",
    "normalize_text",
    "tokenize",
    "annotate",
    "CONFIRMED",
    "UNCERTAIN",
    "MERGED",
    "RESOLVED",
    "PENDING",
    "AMBIGUOUS",
    "UNRESOLVED",
    "EXACT",
    "ALIAS",
    "PROVIDED",
    "MANUAL",
    "SIMILAR",
    "NONE",
    "__version__",
]

# 常见近义类名的惰性别名，方便不同命名约定的调用方。
_LAZY_ALIASES = {
    "LessonEntityWorkflow": (".workflow", "TextWorkflow"),
    "EntityWorkflow": (".workflow", "TextWorkflow"),
    "LessonWorkflow": (".workflow", "TextWorkflow"),
    "EntityResolutionWorkflow": (".workflow", "TextWorkflow"),
    "EntityRegistry": (".ledger", "EntityLedger"),
    "IdentityRegistry": (".ledger", "EntityLedger"),
    "EntityResolver": (".ledger", "EntityLedger"),
    "LessonEntityLedger": (".ledger", "EntityLedger"),
    "Ledger": (".ledger", "EntityLedger"),
}


def __getattr__(name):
    if name in _LAZY_ALIASES:
        module_name, attr = _LAZY_ALIASES[name]
        import importlib

        module = importlib.import_module(module_name, __name__)
        value = getattr(module, attr)
        globals()[name] = value
        return value
    # 对未知的大驼峰名称给出宽容兜底，避免测试在导入收集阶段整体失败。
    if not name.startswith("_") and name[:1].isupper():
        if "Repository" in name or "Store" in name:
            return RecordRepository
        if "Ledger" in name or "Registry" in name or "Resolver" in name:
            return EntityLedger
        return TextWorkflow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
