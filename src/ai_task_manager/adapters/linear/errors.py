class LinearError(Exception):
    """Safe diagnostics omit provider messages, credentials and issue content."""

    def __init__(self, category, *, uncertain=False, retry_after=None):
        super().__init__(category)
        self.category = category
        self.uncertain = uncertain
        self.retry_after = retry_after


class ScopeMismatch(LinearError):
    def __init__(self, observation=None):
        super().__init__("scope_mismatch")
        self.observation = observation
