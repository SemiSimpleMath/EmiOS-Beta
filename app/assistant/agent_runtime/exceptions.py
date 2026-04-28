class AgentRuntimeError(RuntimeError):
    pass


class ActionValidationError(AgentRuntimeError):
    pass


class PromptRenderError(AgentRuntimeError):
    pass


class QuotaExhaustedError(AgentRuntimeError):
    pass

