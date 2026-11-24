"""关系查询与溯源的函数式入口。"""
from .entities import Relation

__all__ = ["Relation", "query_relations", "provenance", "relation_provenance"]


def query_relations(workflow_or_ledger, entity=None, **filters):
    return workflow_or_ledger.query_relations(entity, **filters)


def relation_provenance(workflow_or_ledger, relation_id):
    if hasattr(workflow_or_ledger, "relation_provenance"):
        return workflow_or_ledger.relation_provenance(relation_id)
    return workflow_or_ledger.ledger.relation_provenance(relation_id)


def provenance(workflow_or_ledger, relation_id):
    return relation_provenance(workflow_or_ledger, relation_id)
