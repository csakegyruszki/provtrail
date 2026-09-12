"""provtrail: a minimal, verifiable source chain-of-custody ledger."""

from .ledger import (
    STATE_MISSING,
    STATE_PRESENT,
    STATE_UNKNOWN,
    CheckResult,
    ContractError,
    Ledger,
    LedgerError,
    LockTimeout,
    VerifyReport,
)

__version__ = "0.2.2"

__all__ = [
    "Ledger",
    "VerifyReport",
    "CheckResult",
    "ContractError",
    "LockTimeout",
    "LedgerError",
    "STATE_PRESENT",
    "STATE_MISSING",
    "STATE_UNKNOWN",
    "__version__",
]
