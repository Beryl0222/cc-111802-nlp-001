"""实体身份模型与注册表入口。"""
from .entities import (
    CONFIRMED,
    UNCERTAIN,
    Entity,
    Mention,
)
from .ledger import EntityLedger


class IdentityRegistry(EntityLedger):
    """EntityLedger 的近义命名。"""


__all__ = ["Entity", "Mention", "EntityLedger", "IdentityRegistry", "CONFIRMED", "UNCERTAIN"]
