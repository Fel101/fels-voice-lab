"""Errors that carry the status the HTTP API answers for them (serve.error_status), beside their sentence.

Standard library only: the service and the model workers both import the modules that raise them."""


class NotInstalled(ValueError):
    """A request needs a model whose files are not in place. It is a ValueError, so code that refuses a request it cannot
    take still refuses this one; the HTTP API answers 503, with the code model_not_installed, instead of 400."""

    status_code = 503
