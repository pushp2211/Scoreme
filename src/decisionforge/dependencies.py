"""A deterministic external-service simulation; never performs network I/O."""

from pydantic import Field

from decisionforge.config import StrictModel


class DependencyFailure(Exception):
    def __init__(self, code: str, *, transient: bool):
        self.code, self.transient = code, transient
        super().__init__(code)


class Simulation(StrictModel):
    fail_until: int = Field(default=0, ge=0, le=10)
    permanent_failure: bool = False
    verified: bool = True
    risk_score: int = Field(default=10, ge=0, le=100)


def verify(payload: dict, attempt: int, operation_key: str) -> dict:
    simulation = Simulation.model_validate(payload.get("simulation", {}))
    if simulation.permanent_failure:
        raise DependencyFailure("provider_permanent_error", transient=False)
    if attempt <= simulation.fail_until:
        raise DependencyFailure("provider_unavailable", transient=True)
    return {
        "verified": simulation.verified,
        "risk_score": simulation.risk_score,
        "provider": "simulated_verification",
        "operation_key": operation_key,
    }
