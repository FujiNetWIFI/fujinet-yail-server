"""Shared helpers for the generation backends."""


def short_error(e: Exception, limit: int = 120) -> str:
    """One short line describing an API error, for client-facing messages."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"]).split("\n")[0][:limit]
    return str(e).split("\n")[0][:limit]
