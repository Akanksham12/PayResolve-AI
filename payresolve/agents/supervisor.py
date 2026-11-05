from payresolve.schemas import InvestigationOutput, ResolutionOutput, TransactionOutput


def select_route(transaction: TransactionOutput, investigation: InvestigationOutput,
                 resolution: ResolutionOutput, errors: list[str]) -> str:
    if errors or resolution.requires_human_review or not transaction.found:
        return "human_review"
    if investigation.confidence < 0.6:
        return "human_review"
    return "complete"
