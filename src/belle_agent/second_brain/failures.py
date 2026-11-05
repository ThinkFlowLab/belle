"""Name a failure for the log without its message."""


def describe(exc: BaseException) -> str:
    """The exception type and the HTTP status of its chain, if any.

    The message is left out: a library or a provider can echo the document,
    the prompt or the query in it.
    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        status = getattr(link, "status_code", None) or getattr(getattr(link, "response", None), "status_code", None)
        if isinstance(status, int):
            return f"{type(exc).__name__} (HTTP {status})"
        link = link.__cause__ or link.__context__
    return type(exc).__name__
