"""概念关系图导出入口。"""


def concept_graph(workflow_or_ledger, **filters):
    obj = getattr(workflow_or_ledger, "ledger", workflow_or_ledger)
    return obj.concept_graph(**filters)


def build_graph(workflow_or_ledger, **filters):
    return concept_graph(workflow_or_ledger, **filters)


__all__ = ["concept_graph", "build_graph"]
