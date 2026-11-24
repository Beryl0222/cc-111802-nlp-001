"""人工纠正相关的函数式入口（核心逻辑在 :class:`EntityLedger`）。"""
from .entities import Correction, normalize_action

__all__ = ["Correction", "normalize_action", "apply_correction", "merge_identities"]


def apply_correction(ledger, action=None, payload=None, **kwargs):
    """在任意带 ``add_correction`` 的台账/工作流上应用一条人工纠正。"""
    return ledger.add_correction(action, payload, **kwargs)


def merge_identities(ledger, source, target, **kwargs):
    return ledger.add_correction("merge", {"source": source, "target": target, **kwargs})
