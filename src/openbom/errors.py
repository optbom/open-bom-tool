"""Exception types raised by the core library.

The CLI catches OpenBomError, prints the message, and exits non-zero. Messages
must be specific: name the owning org, the missing part ID, or the cycle.
"""


class OpenBomError(Exception):
    """Base class for all expected, user-facing errors."""


class SchemaError(OpenBomError):
    """An object or field value violates the object model (§4)."""


class NotFoundError(OpenBomError):
    """A referenced org, part, workspace, object, or revision does not exist."""


class ConflictError(OpenBomError):
    """Something already exists or the request contradicts current state."""


class OwnershipError(OpenBomError):
    """The acting org tried to change something owned by another org (§5)."""


class IdentityError(OpenBomError):
    """No acting org is set, or it does not exist."""


class CycleError(OpenBomError):
    """The draft configuration graph contains a cycle."""


class NothingToCommit(OpenBomError):
    """The draft's root configuration equals HEAD's."""
