"""An in-memory capture producer capability for trusted durable intake."""


class CompletedCapture:
    def __init__(self, owner, token):
        if token is not owner._completion_token:
            from ai_task_manager.domains.work.errors import AccessDenied

            raise AccessDenied("Only the actual completed capture can supply this capability")
        self._owner, self._token = owner, token

    def export(self):
        return self._owner._export_completion(self, self._token)
