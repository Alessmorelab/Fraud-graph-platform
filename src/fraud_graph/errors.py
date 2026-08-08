class FraudGraphError(Exception):
    """Base exception for expected platform failures."""


class DataContractError(FraudGraphError):
    """Raised when source data violates a required contract."""


class ProductionSafetyError(FraudGraphError):
    """Raised when an unsafe training or serving mode is requested."""


class ArtifactNotReadyError(FraudGraphError):
    """Raised when required versioned artifacts are unavailable."""
