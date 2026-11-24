"""correction 的单数模块名兼容入口。"""
from .entities import Correction, CorrectionRecord, Decision, normalize_action
from .corrections import apply_correction, merge_identities

__all__ = [
    "Correction",
    "CorrectionRecord",
    "Decision",
    "normalize_action",
    "apply_correction",
    "merge_identities",
]
