"""What every registrar client speaks: the data it exchanges and the way it fails.

A client is a thin, stateless-looking wrapper over one registrar's HTTP API. It never
touches the database: the workflow in ``core.services.registrations`` decides what to
ask and records what comes back. Status words are normalized here so the workflow
reads the same whichever registrar answers.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import requests

# (connect, read). A registrar call runs in the middle of a web request or of the
# cron task: it must give up well before the router's 30 seconds.
TIMEOUT = (3, 15)


class RegistrarError(Exception):
    """A registrar call did not go through."""


class RegistrarConfigError(RegistrarError):
    """The operator's registrar account is missing, incomplete or refused."""


class RegistrarUnavailable(RegistrarError):
    """The registrar could not be reached or failed on its side: try again later."""


class RegistrarRejected(RegistrarError):
    """The registrar understood the request and refused it. ``message`` says why."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Contact:  # pylint: disable=too-many-instance-attributes
    """A party named on a domain: the holder, or the contact for the operator."""

    email: str
    phone: str
    address1: str
    zip_code: str
    city: str
    country: str = "FR"
    organization: str = ""
    identifier: str = ""
    first_name: str = ""
    last_name: str = ""
    language: str = "FR"


@dataclass(frozen=True)
class Availability:
    """Whether a domain can be registered, as the registrar sees it."""

    domain: str
    available: bool
    # Registrable, but the registry examines the name first: it stays pending for
    # days instead of being granted at once.
    needs_review: bool = False
    # Why it is not available, or the registrar's own word for the review.
    reason: str = ""


@dataclass(frozen=True)
class DomainState:
    """A domain as the registrar reports it.

    ``status`` is one of ``DomainRegistration.Status`` (as a string), or ``None``
    when the registrar used a word we do not know: the caller then keeps the status
    it has rather than guessing.
    """

    status: Optional[str]
    registrar_status: str
    nameservers: list = field(default_factory=list)
    expires_at: Optional[date] = None
    registered_at: Optional[date] = None
    message: str = ""


# What an operation (a registration, a nameserver change) came to.
OPERATION_PENDING = "pending"
OPERATION_DONE = "done"
OPERATION_FAILED = "failed"


@dataclass(frozen=True)
class Operation:
    """The outcome of a request to the registrar.

    ``reference`` identifies it for later, empty when the registrar gives none.
    """

    state: str
    reference: str = ""
    message: str = ""


# French overseas calling codes have three digits; everything else we meet has two.
_THREE_DIGIT_CODES = ("262", "508", "590", "594", "596", "681", "687", "689")


def international_phone(value: str) -> str:
    """Write a phone number as ``+33 123456789``, the form registrars ask for.

    A French national number (``01 23 45 67 89``) gets ``+33``; an international one
    keeps its calling code. Raises ``ValueError`` for anything else.
    """
    digits = re.sub(r"[^\d+]", "", value or "")
    if re.fullmatch(r"0\d{9}", digits):
        return f"+33 {digits[1:]}"
    match = re.fullmatch(r"\+(\d{8,15})", digits)
    if not match:
        raise ValueError(f"{value!r} is not a phone number")
    number = match.group(1)
    length = 3 if number.startswith(_THREE_DIGIT_CODES) else 2
    return f"+{number[:length]} {number[length:]}"


def error_message(response: requests.Response) -> str:
    """What a registrar said was wrong, from an error response."""
    try:
        body = response.json()
    except ValueError:
        return response.text.strip()[:300] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            return "; ".join(
                str(item.get("message", item)) if isinstance(item, dict) else str(item)
                for item in errors
            )[:300]
        for key in ("message", "error", "detail", "MESSAGE"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300]


class RegistrarClient:
    """The calls the workflow makes, whichever registrar answers.

    Use it as a context manager: it holds a session at the registrar when the API
    has one, and gives it back on exit.
    """

    def __init__(self, config):
        self.config = config
        self.http = requests.Session()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def close(self):
        """Give back what was opened at the registrar."""
        self.http.close()

    def check(self, domain: str) -> Availability:
        """Whether the domain can be registered."""
        raise NotImplementedError

    def get_domain(self, domain: str) -> Optional[DomainState]:
        """The domain in the operator's account, or ``None`` when it is not there."""
        raise NotImplementedError

    def create_contacts(self, domain: str, holder: Contact, operator: Contact) -> dict:
        """Create what the registration needs to name, and return their references.

        The references go back to :meth:`create_domain` unchanged.
        """
        raise NotImplementedError

    def create_domain(
        self, domain: str, years: int, contacts: dict, nameservers: list[str]
    ) -> Operation:
        """Ask for the registration."""
        raise NotImplementedError

    def set_nameservers(self, domain: str, nameservers: list[str]) -> Operation:
        """Ask for the domain to be delegated to these nameservers."""
        raise NotImplementedError

    def get_operation(self, reference: str) -> Operation:
        """Where an earlier request stands. Only for registrars that give references."""
        raise NotImplementedError
