"""Registrars: where domain names are registered, behind one interface.

An operator's account at its registrar is configured on its ``OperatorServiceConfig``
for the domain registration service::

    config_override = {
        "registrar": "netim",             # a key of CLIENTS
        "environment": "production",      # or "ote", the registrar's test platform
        "account_id": "AA001",            # reseller ID (Netim only)
        "contact": {                      # the operator, named on every domain
            "first_name": "...", "last_name": "...", "email": "...",
            "phone": "01 23 45 67 89", "address1": "...", "zip_code": "...",
            "city": "...",
            "country": "FR",              # optional
            "organization": "...",        # Nameshield only
            "identifier": "...",          # Nameshield only: SIREN of the operator
        },
    }

and its secret (Netim API password, Nameshield token) in ``api_secret``.
"""

from typing import NamedTuple

from .base import (
    Availability,
    Contact,
    DomainState,
    Operation,
    RegistrarClient,
    RegistrarConfigError,
    RegistrarError,
    RegistrarRejected,
    RegistrarUnavailable,
    international_phone,
)
from .nameshield import NameshieldClient
from .netim import NetimClient

__all__ = [
    "Availability",
    "CLIENTS",
    "Contact",
    "DomainState",
    "Operation",
    "RegistrarClient",
    "RegistrarConfig",
    "RegistrarConfigError",
    "RegistrarError",
    "RegistrarRejected",
    "RegistrarUnavailable",
    "get_client",
    "international_phone",
    "parse_config",
]

CLIENTS = {
    "netim": NetimClient,
    "nameshield": NameshieldClient,
}

ENVIRONMENTS = ("production", "ote")

_CONTACT_KEYS = (
    "first_name",
    "last_name",
    "email",
    "phone",
    "address1",
    "zip_code",
    "city",
)
_NAMESHIELD_CONTACT_KEYS = ("organization", "identifier")


class RegistrarConfig(NamedTuple):
    """An operator's validated account at a registrar."""

    registrar: str
    environment: str
    account_id: str
    secret: str
    contact: Contact


def parse_config(config: dict, secret) -> RegistrarConfig:
    """Validate an operator's registrar configuration.

    Raises :class:`RegistrarConfigError` naming everything that is wrong.
    """
    config = config or {}
    problems = []

    registrar = config.get("registrar")
    if registrar not in CLIENTS:
        problems.append(f"registrar must be one of {', '.join(sorted(CLIENTS))}")
    environment = config.get("environment")
    if environment not in ENVIRONMENTS:
        problems.append(f"environment must be one of {', '.join(ENVIRONMENTS)}")
    account_id = str(config.get("account_id") or "").strip()
    if registrar == "netim" and not account_id:
        problems.append("account_id (the reseller ID) is required for Netim")
    if not secret:
        problems.append("the API secret is not set")

    raw = config.get("contact")
    raw = raw if isinstance(raw, dict) else {}
    required = _CONTACT_KEYS + (
        _NAMESHIELD_CONTACT_KEYS if registrar == "nameshield" else ()
    )
    missing = [key for key in required if not str(raw.get(key) or "").strip()]
    if missing:
        problems.append(f"contact is missing {', '.join(missing)}")

    phone = ""
    if raw.get("phone"):
        try:
            phone = international_phone(str(raw["phone"]))
        except ValueError:
            problems.append("contact phone is not a valid phone number")

    if problems:
        raise RegistrarConfigError("; ".join(problems))

    contact = Contact(
        email=raw["email"].strip(),
        phone=phone,
        address1=raw["address1"].strip(),
        zip_code=raw["zip_code"].strip(),
        city=raw["city"].strip(),
        country=str(raw.get("country") or "FR").strip().upper(),
        organization=str(raw.get("organization") or "").strip(),
        identifier=str(raw.get("identifier") or "").strip(),
        first_name=raw["first_name"].strip(),
        last_name=raw["last_name"].strip(),
    )
    return RegistrarConfig(registrar, environment, account_id, secret, contact)


def get_client(config: RegistrarConfig) -> RegistrarClient:
    """The client for an operator's account."""
    return CLIENTS[config.registrar](config)
