"""An in-memory capture producer capability for trusted durable intake."""

from ai_task_manager.domains.work.errors import AccessDenied


class CompletedCapture:
    __slots__ = ("_owner", "_token", "_original")

    def __init_subclass__(cls, **kwargs):
        raise TypeError("Capture completion capabilities cannot be subclassed")

    def __init__(self, owner, token):
        from .capture import ConformanceCapture

        if (
            type(owner) is not ConformanceCapture
            or token is not owner._completion_token
            or owner._complete_hash is None
            or owner._native_owner is None
        ):
            raise AccessDenied("Only the actual completed capture can supply this capability")
        self._owner, self._token = owner, token
        self._original = (owner, token)

    def export(self):
        if self._owner is not self._original[0] or self._token is not self._original[1]:
            raise AccessDenied("The original capture producer changed")
        return self._owner._export_completion(self, self._token)
