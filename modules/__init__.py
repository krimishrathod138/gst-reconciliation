"""GST reconciliation engine modules: loader, cleaner, matcher, reports, database."""


class ReconError(ValueError):
    """Error whose message is safe and meaningful to show to an end user."""
