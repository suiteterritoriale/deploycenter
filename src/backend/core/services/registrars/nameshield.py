"""Nameshield registrar API, v2.

https://docs.nameshield.net/docs/openapi/domains/api-registrar/

A Bearer token (obtained from the account manager) authenticates every call, there is
no session. Contacts are created first and scoped to one extension; registering a
domain answers ``202`` with no operation to follow, so progress is read from the
domain itself. Nameshield rate limits to 600 requests per five minutes per token.
"""

from datetime import date
from typing import Optional

import requests

from .base import (
    OPERATION_DONE,
    OPERATION_PENDING,
    TIMEOUT,
    Availability,
    Contact,
    DomainState,
    Operation,
    RegistrarClient,
    RegistrarConfigError,
    RegistrarRejected,
    RegistrarUnavailable,
    error_message,
)

BASE_URLS = {
    "production": "https://api.nameshield.net/registrar/v2",
    "ote": "https://ote-api.nameshield.net/registrar/v2",
}

_PENDING = {"ORDERED", "INVOICED", "PAID", "PENDING_CREATION"}
_RELEASED = {"DELETED", "KILLED"}
_HOLDS = {"clienthold", "serverhold"}


def _parse_date(value) -> Optional[date]:
    """A ``YYYY-MM-DD`` date (a time after it is ignored), or ``None``."""
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def map_domain_status(
    raw: str, nameservers: list, registry_statuses: list
) -> Optional[str]:
    """A Nameshield domain status as one of ours, or ``None`` when we do not know it."""
    word = (raw or "").upper()
    if word in _RELEASED:
        return "released"
    if word == "PENDING_DELETION":
        return "expired"
    if word == "BLOCKED":
        return "blocked"
    if word in _PENDING:
        return "pending"
    if word in ("CREATED", "PENDING_MODIFY"):
        if any(str(item).lower() in _HOLDS for item in registry_statuses):
            return "blocked"
        # Nameshield does not say whether the zone is published: a domain with
        # nameservers is taken to be.
        return "active" if nameservers else "registered"
    return None


def _positions(nameservers: list[str]) -> dict:
    """Nameservers as Nameshield wants them: keyed from 0, primary first."""
    return {str(index): name for index, name in enumerate(nameservers)}


class NameshieldClient(RegistrarClient):
    """A Nameshield account."""

    def __init__(self, config):
        """Prepare the client for the account's environment."""
        super().__init__(config)
        self.base = BASE_URLS[config.environment]

    def _call(self, method, path, body=None):
        """Call the API and return ``(status code, decoded body)``."""
        try:
            response = self.http.request(
                method,
                self.base + path,
                json=body,
                headers={"Authorization": f"Bearer {self.config.secret}"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise RegistrarUnavailable(
                f"Nameshield unreachable: {type(error).__name__}"
            ) from error
        if response.status_code == 401:
            raise RegistrarConfigError("Nameshield refused the token")
        if response.status_code >= 500 or response.status_code == 429:
            raise RegistrarUnavailable(f"Nameshield answered {response.status_code}")
        if response.status_code >= 400:
            code = ""
            try:
                code = str(response.json()["errors"][0]["code"])
            except (ValueError, KeyError, IndexError, TypeError):
                code = str(response.status_code)
            raise RegistrarRejected(error_message(response), code)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, {}

    @staticmethod
    def _first(body) -> dict:
        """The first item of a response's ``data`` list, or an empty one."""
        data = body.get("data") if isinstance(body, dict) else None
        return data[0] if isinstance(data, list) and data else {}

    def check(self, domain):
        """Whether the domain can be registered through the API."""
        _, body = self._call("GET", f"/domains/{domain}/check")
        entry = self._first(body)
        if not entry:
            raise RegistrarUnavailable("Nameshield gave no answer for the check")
        for flag, reason in (
            ("premium", "premium"),
            ("dpmlExists", "dpml"),
            ("collision", "collision"),
        ):
            if entry.get(flag):
                return Availability(domain, False, reason=reason)
        if not entry.get("available"):
            return Availability(
                domain, False, reason="reserved" if entry.get("reserved") else ""
            )
        return Availability(
            domain,
            True,
            needs_review=bool(entry.get("reserved")),
            reason="reserved" if entry.get("reserved") else "",
        )

    def get_domain(self, domain) -> Optional[DomainState]:
        """The domain in the account, or ``None`` when Nameshield does not know it."""
        try:
            _, body = self._call("GET", f"/domains/{domain}")
        except RegistrarRejected as error:
            if error.code in ("NOT_FOUND", "404"):
                return None
            raise
        info = self._first(body)
        if not info:
            return None
        servers = info.get("nameservers") or []
        if isinstance(servers, dict):
            servers = list(servers.values())
        nameservers = sorted(
            {str(ns).strip().rstrip(".").lower() for ns in servers if ns}
        )
        raw = str(info.get("status", ""))
        return DomainState(
            status=map_domain_status(raw, nameservers, info.get("statusList") or []),
            registrar_status=raw,
            nameservers=nameservers,
            expires_at=_parse_date(info.get("expirationDate")),
            registered_at=_parse_date(
                info.get("nicCreationDate") or info.get("creationDate")
            ),
        )

    def _create_contact(self, extension, kind, contact: Contact) -> str:
        """Create one contact of a kind for an extension, and return its handle."""
        body = {
            "extension": extension,
            "type": kind,
            "juridic": "m",
            "organization": contact.organization,
            "address": contact.address1,
            "postalCode": contact.zip_code,
            "city": contact.city,
            "country": contact.country,
            "email": contact.email,
            "phone": contact.phone,
        }
        if extension == "fr":
            body["addons"] = {"fr": [{"ident": contact.identifier}]}
        _, response = self._call("POST", "/contacts", body)
        handle = self._first(response).get("handle")
        if not handle:
            raise RegistrarUnavailable("Nameshield created a contact without naming it")
        return handle

    def create_contacts(self, domain, holder: Contact, operator: Contact) -> dict:
        """Create the owner from the holder, and the administrative and technical
        contacts from the operator."""
        extension = domain.split(".", 1)[1]
        return {
            "owner": self._create_contact(extension, "owner", holder),
            "admin": self._create_contact(extension, "admin", operator),
            "tech": self._create_contact(extension, "tech", operator),
        }

    def create_domain(self, domain, years, contacts, nameservers) -> Operation:
        """Ask Nameshield to register the domain."""
        body = {
            "domain": domain,
            "period": years,
            "owner": contacts["owner"],
            "admin": contacts["admin"],
            "tech": contacts["tech"],
        }
        if nameservers:
            body["nameservers"] = _positions(nameservers)
        self._call("POST", "/domains", body)
        # Accepted. There is nothing to follow but the domain itself.
        return Operation(OPERATION_PENDING)

    def set_nameservers(self, domain, nameservers) -> Operation:
        """Ask Nameshield to delegate the domain to these nameservers."""
        code, _ = self._call(
            "PUT", f"/domains/{domain}", {"nameservers": _positions(nameservers)}
        )
        return Operation(OPERATION_DONE if code == 200 else OPERATION_PENDING)

    def get_operation(self, reference) -> Operation:
        """Nameshield gives no operation to follow."""
        raise NotImplementedError("Nameshield gives no operations to follow")
