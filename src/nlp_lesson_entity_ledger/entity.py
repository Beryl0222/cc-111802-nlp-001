"""单数模块名兼容入口，实际定义见 :mod:`entities` 与 :mod:`ledger`。"""
from .entities import Entity, EntityIdentity, EntityRecord
from .ledger import EntityLedger

EntityRegistry = EntityLedger

__all__ = ["Entity", "EntityIdentity", "EntityRecord", "EntityLedger", "EntityRegistry"]
