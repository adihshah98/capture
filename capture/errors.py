"""The API's error type.

Its own module because the things that raise it — the WorkOS client, request
validation, the route handlers — should not have to import the web app to do so.
"""


class ApiError(Exception):
    """A failure with an HTTP status the client should see.

    server.py registers the handler that turns this into a JSON body; anything
    raising it needs only this module.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)
