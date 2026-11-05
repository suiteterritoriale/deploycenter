"""A scripted registrar client for testing the workflow without any HTTP."""

from core.models import DomainRegistration, OperatorServiceConfig
from core.services.registrars import (
    Availability,
    DomainState,
    Operation,
    RegistrarClient,
    RegistrarError,
    parse_config,
)

from .test_registrars import netim_config

Status = DomainRegistration.Status


class FakeRegistrar(RegistrarClient):  # pylint: disable=too-many-instance-attributes
    """Answers what it is told to, and remembers what it was asked.

    Set ``state`` to what ``get_domain`` returns, ``availability`` to what ``check``
    returns, ``create`` and ``nameservers`` to the operations answered. Any of the
    attributes can be an exception instance, which is then raised instead.
    """

    def __init__(self, config=None):
        super().__init__(config or parse_config(netim_config(), "s3cret"))
        self.state = None
        self.availability = None
        self.contacts = {
            "owner": "QL1",
            "admin": "AA001",
            "tech": "AA001",
            "billing": "AA001",
        }
        self.create = Operation("pending", "42")
        self.nameservers = Operation("pending")
        self.operation = Operation("pending", "42")
        self.calls = []
        self.closed = False

    def _answer(self, name, value, *args):
        self.calls.append((name, *args))
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True

    def check(self, domain):
        default = Availability(domain, True)
        return self._answer("check", self.availability or default, domain)

    def get_domain(self, domain):
        return self._answer("get_domain", self.state, domain)

    def create_contacts(self, domain, holder, operator):
        return self._answer("create_contacts", self.contacts, domain)

    def create_domain(self, domain, years, contacts, nameservers):
        return self._answer("create_domain", self.create, domain, years, nameservers)

    def set_nameservers(self, domain, nameservers):
        return self._answer("set_nameservers", self.nameservers, domain, nameservers)

    def get_operation(self, reference):
        return self._answer("get_operation", self.operation, reference)

    def names(self):
        """The names of the calls made, in order."""
        return [call[0] for call in self.calls]


def state(status, registrar_status="", **kwargs):
    """A domain as a registrar reports it."""
    return DomainState(status=status, registrar_status=registrar_status, **kwargs)


class Boom(RegistrarError):
    """A registrar failure, for scripting."""


def configure_account(operator, service, **overrides):
    """Give an operator a working registrar account for a service."""
    return OperatorServiceConfig.objects.create(
        operator=operator,
        service=service,
        config_override=netim_config(**overrides),
        api_secret="s3cret",
    )
