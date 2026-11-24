"""relation 的单数模块名兼容入口。"""
from .entities import Relation, RelationTriple, Triple, ConceptRelation
from .relations import query_relations, relation_provenance, provenance

__all__ = [
    "Relation",
    "RelationTriple",
    "Triple",
    "ConceptRelation",
    "query_relations",
    "relation_provenance",
    "provenance",
]
