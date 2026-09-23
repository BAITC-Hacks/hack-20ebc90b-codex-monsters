"""Internal data errors translated to the shared DomainError at the boundary."""


class DataError(ValueError):
    def __init__(self, code: str, message: str, affected_sku_ids=None, retryable=False):
        super().__init__(message)
        self.code = code
        self.affected_sku_ids = affected_sku_ids or []
        self.retryable = retryable
