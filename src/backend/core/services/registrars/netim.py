"""Netim reseller API, REST 1.0.

https://support.netim.com/en/docs/api-rest-1-0

Authentication opens a session (the reseller ID and the API password, sent once as
Basic) whose token is then sent as a Bearer. Netim allows 10 concurrent sessions per
account, so a client opens its session on first use and closes it on exit.

Registering and changing nameservers are operations: they answer at once with a
tracking ID and a status, and a pending one is followed with ``get_operation``.
"""

import logging
import re
from datetime import date
from typing import Optional

import requests

from .base import (
    OPERATION_DONE,
    OPERATION_FAILED,
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

logger = logging.getLogger(__name__)

# The test platform is only served over plain HTTP.
BASE_URLS = {
    "production": "https://rest.netim.com/1.0",
    "ote": "http://oterest.netim.com/1.0",
}

# Netim names no more than five nameservers on a domain.
MAX_NAMESERVERS = 5

# Field lengths of a Netim contact: a longer value is refused rather than cut, since
# these are legal names and addresses.
_LIMITS = {
    "firstName": 30,
    "lastName": 30,
    "bodyName": 100,
    "address1": 80,
    "zipCode": 10,
    "city": 30,
}

_UNKNOWN_DOMAIN = re.compile(r"not found|unknown|does not exist|n'existe pas", re.I)

_OPERATION_STATES = {
    "done": OPERATION_DONE,
    "pending": OPERATION_PENDING,
    "failed": OPERATION_FAILED,
    "cancelled": OPERATION_FAILED,
    "error": OPERATION_FAILED,
}


def map_domain_status(raw: str) -> Optional[str]:
    """A Netim domain status (``ACTIVE, DELEGATED``) as one of ours, or ``None``."""
    tokens = {
        token.strip() for token in (raw or "").upper().split(",") if token.strip()
    }
    if tokens & {"DELETED", "TRANSFERRED OUT"}:
        return "released"
    if "ERROR" in tokens:
        return "failed"
    if "PENDING" in tokens:
        return "pending"
    if tokens & {"EXPIRED", "QUARANTINE"}:
        return "expired"
    # RESERVED: registered but waiting for the registrant to activate it.
    if tokens & {"HOLD", "RESERVED"}:
        return "blocked"
    if "ACTIVE" in tokens:
        return "registered" if "NOT DELEGATED" in tokens else "active"
    return None


def _parse_date(value) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class NetimClient(RegistrarClient):
    """A Netim reseller account."""

    def __init__(self, config):
        super().__init__(config)
        self.base = BASE_URLS[config.environment]
        self.token = None

    def _open_session(self):
        response = self._send(
            "POST",
            "/session/",
            auth=(self.config.account_id, self.config.secret),
            headers={"Accept-Language": "FR"},
        )
        if response.status_code in (401, 403):
            raise RegistrarConfigError("Netim refused the account credentials")
        self._check(response)
        token = response.json().get("access_token")
        if not token:
            raise RegistrarUnavailable("Netim opened no session")
        self.token = token
        # Asynchronous: a registration answers at once with an operation to follow,
        # instead of holding the request until the registry replies.
        self._check(
            self._send(
                "PATCH",
                "/session/",
                json={"type": "sync", "value": "0"},
                headers={"Authorization": f"Bearer {token}"},
            )
        )

    def _send(self, method, path, **kwargs):
        headers = {"Content-Type": "application/json", **kwargs.pop("headers", {})}
        try:
            return self.http.request(
                method, self.base + path, headers=headers, timeout=TIMEOUT, **kwargs
            )
        except requests.RequestException as error:
            raise RegistrarUnavailable(
                f"Netim unreachable: {type(error).__name__}"
            ) from error

    @staticmethod
    def _check(response):
        if response.status_code >= 500 or response.status_code == 429:
            raise RegistrarUnavailable(f"Netim answered {response.status_code}")
        if response.status_code >= 400:
            raise RegistrarRejected(error_message(response), str(response.status_code))

    def _call(self, method, path, body=None):
        """Call the API inside the session, reopening it once if Netim dropped it."""
        for attempt in (1, 2):
            if self.token is None:
                self._open_session()
            response = self._send(
                method,
                path,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            if response.status_code == 401 and attempt == 1:
                self.token = None
                continue
            if response.status_code == 401:
                raise RegistrarConfigError("Netim refused the session")
            self._check(response)
            return response.json()
        raise RegistrarUnavailable(
            "Netim kept dropping the session"
        )  # pragma: no cover

    def close(self):
        if self.token:
            try:
                self._send(
                    "DELETE",
                    "/session/",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            except RegistrarUnavailable:
                logger.info("Netim session left to expire")
            self.token = None
        super().close()

    def check(self, domain):
        results = self._call("GET", f"/domain/{domain}/check/")
        entry = results[0] if isinstance(results, list) and results else {}
        result = str(entry.get("result", "")).upper()
        reason = str(entry.get("reason", "")).upper()
        if result == "AVAILABLE" and reason in ("", "RESERVED", "PENDING APPLICATION"):
            return Availability(
                domain, True, needs_review=bool(reason), reason=reason.lower()
            )
        if result == "UNKNOWN":
            raise RegistrarUnavailable(
                f"Netim could not tell: {reason.lower() or 'unknown'}"
            )
        # NOT AVAILABLE, CLOSED, NOT SOLD, and a premium name, whose price we do not
        # ask: all of them are a no for us.
        return Availability(domain, False, reason=(reason or result).lower())

    def get_domain(self, domain) -> Optional[DomainState]:
        try:
            info = self._call("GET", f"/domain/{domain}/info/")
        except RegistrarRejected as error:
            if error.code == "404" or _UNKNOWN_DOMAIN.search(error.message):
                return None
            raise
        raw = str(info.get("status", ""))
        servers = info.get("ns") or []
        if isinstance(servers, dict):
            servers = list(servers.values())
        return DomainState(
            status=map_domain_status(raw),
            registrar_status=raw,
            nameservers=sorted(
                {str(ns).strip().rstrip(".").lower() for ns in servers if ns}
            ),
            expires_at=_parse_date(info.get("dateExpiration")),
            registered_at=_parse_date(info.get("dateCreate")),
        )

    def create_contacts(self, domain, holder: Contact, operator: Contact) -> dict:
        body = {
            "firstName": holder.first_name,
            "lastName": holder.last_name,
            "bodyForm": "ORG",
            "bodyName": holder.organization,
            "address1": holder.address1,
            "address2": "",
            "zipCode": holder.zip_code,
            "area": "",
            "city": holder.city,
            "country": holder.country,
            "phone": holder.phone,
            "fax": "",
            "email": holder.email,
            "language": holder.language,
            "isOwner": 1,
            "companyNumber": holder.identifier,
        }
        for field, limit in _LIMITS.items():
            if len(body[field]) > limit:
                raise RegistrarRejected(
                    f"{field} is longer than the {limit} characters Netim takes"
                )
        owner = self._call("POST", "/contact/", {"contact": body})
        # The reseller account itself is the administrative, technical and billing
        # contact: it is the operator.
        reseller = self.config.account_id
        return {
            "owner": owner,
            "admin": reseller,
            "tech": reseller,
            "billing": reseller,
        }

    @staticmethod
    def _nameservers(nameservers):
        if len(nameservers) > MAX_NAMESERVERS:
            raise RegistrarRejected(
                f"Netim takes at most {MAX_NAMESERVERS} nameservers"
            )
        return {f"ns{index}": name for index, name in enumerate(nameservers, start=1)}

    @staticmethod
    def _operation(data) -> Operation:
        status = str(data.get("STATUS", "")).lower()
        return Operation(
            state=_OPERATION_STATES.get(status, OPERATION_PENDING),
            reference=str(data.get("ID_OPE", "")),
            message=str(data.get("MESSAGE") or ""),
        )

    def create_domain(self, domain, years, contacts, nameservers) -> Operation:
        body = {
            "idOwner": contacts["owner"],
            "idAdmin": contacts["admin"],
            "idTech": contacts["tech"],
            "idBilling": contacts["billing"],
            "duration": years,
            **self._nameservers(nameservers),
        }
        return self._operation(self._call("POST", f"/domain/{domain}/", body))

    def set_nameservers(self, domain, nameservers) -> Operation:
        body = self._nameservers(nameservers)
        return self._operation(self._call("PUT", f"/domain/{domain}/dns/", body))

    def get_operation(self, reference) -> Operation:
        return self._operation(self._call("GET", f"/operation/{reference}"))
