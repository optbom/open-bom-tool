"""Orgs and the acting identity.

Identity is ambient, like git's: an installation is configured once with the
org it acts for and the user's name (`config --org … --user …`), and a process
can override it with $OPENBOM_ORG / $OPENBOM_USER. Real users, auth, and keys
(D10, D11) will replace how the Actor is obtained without touching callers.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ORG_ENV
from .errors import ConflictError, IdentityError, NotFoundError, OwnershipError
from .objects import validate_org_id
from .store import Store


@dataclass(frozen=True)
class Actor:
    org: str
    user: str | None = None


def create_org(store: Store, org_id: str, name: str | None = None) -> None:
    validate_org_id(org_id)
    with store.transaction():
        if store.get_org(org_id) is not None:
            raise ConflictError(f"org '{org_id}' already exists")
        store.add_org(org_id, name or org_id)


def list_orgs(store: Store) -> list[tuple[str, str]]:
    return [(r["id"], r["name"]) for r in store.list_orgs()]


def require_org(store: Store, org_id: str) -> None:
    if store.get_org(org_id) is None:
        raise NotFoundError(f"org '{org_id}' does not exist")


def make_actor(store: Store, org: str | None, user: str | None = None) -> Actor:
    if org is None:
        raise IdentityError(
            f"no org configured for this installation; run `config --org <org_id>` (or set ${ORG_ENV})"
        )
    require_org(store, org)
    return Actor(org=org, user=user)


def require_owner(actor: Actor, owner_org: str, what: str) -> None:
    """The only rule enforced in v1: you can only change what your org owns."""
    if actor.org != owner_org:
        raise OwnershipError(f"{what} is owned by org '{owner_org}'; you are acting as '{actor.org}'")
