class FlowError(Exception):
    """Only safe, application-owned messages cross the HTTP boundary."""


class NotFound(FlowError):
    pass


class InvalidInput(FlowError):
    pass


class Conflict(FlowError):
    pass
