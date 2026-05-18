class PlaudError(Exception):
    pass


class PlaudSessionExpiredError(PlaudError):
    code = "PLAUD_SESSION_EXPIRED"


class PlaudApiError(PlaudError):
    pass
