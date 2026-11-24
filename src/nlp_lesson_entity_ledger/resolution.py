"""实体解析器入口与纯函数候选工具。"""
from .ledger import EntityLedger
from .text import name_key, name_similarity


class EntityResolver(EntityLedger):
    """身份解析器：与 EntityLedger 同一实现，使用近义命名。"""

    def resolve(self, *args, **kwargs):
        self.rebuild()
        return self.list_mentions(*args, **kwargs)

    def resolve_identities(self):
        self.rebuild()
        return self.list_entities()


def suggest_matches(name, entities, limit=5):
    """对给定名称，在实体集合中给出保守相似候选（不自动合并）。"""
    scored = []
    for entity in entities:
        names = [entity.get("canonical_name", ""), *entity.get("aliases", [])]
        score = max(name_similarity(name, candidate) for candidate in names if candidate)
        if score >= 0.62:
            scored.append((entity, score))
    scored.sort(key=lambda pair: -pair[1])
    return [entity for entity, _ in scored[:limit]]


__all__ = ["EntityResolver", "EntityLedger", "suggest_matches", "name_key", "name_similarity"]
