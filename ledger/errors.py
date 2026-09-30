"""领域错误与 HTTP 状态码的映射。"""


class LedgerError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400

    def __init__(self, message, status=None, details=None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status
        self.details = details or {}

    def to_dict(self):
        payload = {"error": self.__class__.__name__, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class NotFound(LedgerError):
    status = 404


class Conflict(LedgerError):
    status = 409


class ValidationError(LedgerError):
    status = 422


class AuthorizationError(LedgerError):
    status = 403


class IdentityMismatch(LedgerError):
    """上传方与系统登记身份不一致，必须先人工确认。"""

    status = 409
