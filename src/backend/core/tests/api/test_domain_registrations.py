"""Tests for the domain registration API (core/api/viewsets/registrations.py)."""

# pylint: disable=redefined-outer-name

from datetime import datetime, timezone
from unittest import mock

import pytest
from rest_framework.test import APIClient

from core import factories, models
from core.services import dns as dns_service
from core.services import registrations
from core.services.registrars import Availability

from ..fake_registrar import Boom, FakeRegistrar, configure_account, state

pytestmark = pytest.mark.django_db

Status = models.DomainRegistration.Status
NAMESERVERS = ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
ADDRESS = {"address1": "1 place de la Mairie", "zip_code": "29000", "city": "Quimper"}


@pytest.fixture
def setup():
    """A user of an operator that manages an organization subscribed to the service."""
    user = factories.UserFactory()
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory(name="Commune de Test")
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    factories.ServiceSubscriptionFactory(
        organization=organization, operator=operator, service=service, is_active=True
    )
    configure_account(operator, service)
    client = APIClient()
    client.force_login(user)
    return client, user, operator, organization, service


@pytest.fixture
def registrar():
    """The registrar every client in the code under test is replaced by."""
    fake = FakeRegistrar()
    with (
        mock.patch.object(registrations, "get_client", return_value=fake),
        mock.patch("core.api.viewsets.registrations.get_client", return_value=fake),
    ):
        yield fake


@pytest.fixture(autouse=True)
def free_dns():
    """The DNS knows none of the names, unless a test says otherwise."""
    with mock.patch.object(
        dns_service,
        "nameservers_batch",
        side_effect=lambda domains: {
            domain: ([], dns_service.ERROR_NXDOMAIN) for domain in domains
        },
    ) as patched:
        yield patched


def url(operator, organization, suffix=""):
    """The registrations endpoint of an organization."""
    return (
        f"/api/v1.0/operators/{operator.id}/organizations/{organization.id}"
        f"/domain-registrations/{suffix}"
    )


# ---------------------------------------------------------------------------
# Access


def test_anonymous_users_are_turned_away(setup):
    """Nothing is shown without logging in."""
    _, _, operator, organization, _ = setup
    response = APIClient().get(url(operator, organization))
    assert response.status_code in (401, 403)


def test_users_of_another_operator_are_turned_away(setup):
    """A user needs a role in an operator that manages the organization."""
    _, _, operator, organization, _ = setup
    client = APIClient()
    client.force_login(factories.UserFactory())
    assert client.get(url(operator, organization)).status_code == 403


def test_the_service_must_be_subscribed(setup):
    """An organization that did not subscribe has no registrations to show."""
    client, _, operator, organization, service = setup
    models.ServiceSubscription.objects.filter(service=service).update(is_active=False)
    assert client.get(url(operator, organization)).status_code == 404


# ---------------------------------------------------------------------------
# List


def test_list_shows_the_organizations_domains(setup):
    """The domains of the organization, with where they stand, and nothing else."""
    client, _, operator, organization, service = setup
    factories.DomainRegistrationFactory(
        domain="mienne.fr",
        organization=organization,
        operator=operator,
        service=service,
        registrar_status="ACTIVE, DELEGATED",
    )
    factories.DomainRegistrationFactory(domain="autre.fr")

    response = client.get(url(operator, organization))

    assert response.status_code == 200
    assert [item["domain"] for item in response.json()] == ["mienne.fr"]
    item = response.json()[0]
    assert item["status"] == "active"
    assert item["registrar_status"] == "ACTIVE, DELEGATED"
    assert "registrant" not in item and "registrar_data" not in item


# ---------------------------------------------------------------------------
# Create


def test_create_only_records_the_request(setup, registrar):
    """The registrar is not asked while the user waits: the task will do it."""
    client, user, operator, organization, _ = setup

    response = client.post(
        url(operator, organization), {"domain": "Libre.FR", **ADDRESS}, format="json"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["domain"] == "libre.fr"
    assert body["status"] == "requested"
    assert not registrar.calls
    registration = models.DomainRegistration.objects.get(domain="libre.fr")
    assert registration.organization == organization
    assert registration.operator == operator
    assert registration.requested_by == user
    assert registration.next_check_at is not None


def test_a_created_request_is_registered_by_the_task(setup, registrar):
    """The task submits the request, and looks at it again a minute later."""
    client, _, operator, organization, _ = setup
    client.post(
        url(operator, organization), {"domain": "libre.fr", **ADDRESS}, format="json"
    )

    before = datetime.now(timezone.utc)
    summary = registrations.process_due()

    assert summary == {"processed": 1, "failed": 0}
    assert registrar.names() == [
        "get_domain",
        "check",
        "create_contacts",
        "create_domain",
    ]
    registration = models.DomainRegistration.objects.get(domain="libre.fr")
    assert registration.status == Status.PENDING
    assert registration.registrar_data["operation"] == "42"
    assert registration.next_check_at >= before + registrations.FIRST_LOOK
    assert (
        registration.next_check_at
        <= datetime.now(timezone.utc) + registrations.FIRST_LOOK
    )


@pytest.mark.parametrize("domain", ["commune.gouv.fr", "commune.com", "pas un domaine"])
def test_create_refuses_a_domain_that_cannot_be_registered(setup, registrar, domain):
    """The registrar is not asked about a name that could not be registered."""
    client, _, operator, organization, _ = setup

    response = client.post(
        url(operator, organization), {"domain": domain, **ADDRESS}, format="json"
    )

    assert response.status_code == 400
    assert "domain" in response.json()
    assert not registrar.calls
    assert not models.DomainRegistration.objects.exists()


def test_create_refuses_a_domain_the_dns_knows(setup, registrar, free_dns):
    """A name with nameservers is taken."""
    client, _, operator, organization, _ = setup
    free_dns.side_effect = lambda domains: {
        d: (["ns1.autre.fr"], None) for d in domains
    }

    response = client.post(
        url(operator, organization), {"domain": "pris.fr", **ADDRESS}, format="json"
    )

    assert response.status_code == 400
    assert not registrar.calls


@pytest.mark.usefixtures("registrar")
def test_create_needs_the_address(setup):
    """The street address of the organization is asked for."""
    client, _, operator, organization, _ = setup
    response = client.post(
        url(operator, organization), {"domain": "libre.fr"}, format="json"
    )
    assert response.status_code == 400
    assert set(response.json()) == {"address1", "zip_code", "city"}


def test_create_needs_a_configured_operator(setup, registrar):
    """An operator without an account at a registrar is told so."""
    client, _, operator, organization, service = setup
    models.OperatorServiceConfig.objects.filter(
        operator=operator, service=service
    ).delete()

    response = client.post(
        url(operator, organization), {"domain": "libre.fr", **ADDRESS}, format="json"
    )

    assert response.status_code == 409
    assert not registrar.calls


# ---------------------------------------------------------------------------
# Check


def test_check_says_a_free_domain_is_registrable(setup, registrar):
    """A name that passes our rules and the registrar's check."""
    client, _, operator, organization, _ = setup

    response = client.post(
        url(operator, organization, "check/"), {"domain": "libre.fr"}, format="json"
    )

    assert response.status_code == 200
    assert response.json() == {
        "domain": "libre.fr",
        "registrable": True,
        "reasons": [],
        "message": "",
        "needs_review": False,
        "nameservers": NAMESERVERS,
    }
    assert registrar.names() == ["check"]


def test_check_says_when_the_registry_examines_the_name(setup, registrar):
    """A name the registry reviews first can be asked for, with a warning."""
    client, _, operator, organization, _ = setup
    registrar.availability = Availability("libre.fr", True, needs_review=True)

    response = client.post(
        url(operator, organization, "check/"), {"domain": "libre.fr"}, format="json"
    )

    assert response.json()["registrable"] is True
    assert response.json()["needs_review"] is True


def test_check_refuses_without_asking_the_registrar(setup, registrar):
    """The registrar is only asked about names that got through our rules."""
    client, _, operator, organization, _ = setup

    response = client.post(
        url(operator, organization, "check/"),
        {"domain": "commune.gouv.fr"},
        format="json",
    )

    assert response.json()["registrable"] is False
    assert response.json()["reasons"] == ["level"]
    assert response.json()["message"]
    assert not registrar.calls


def test_check_reports_a_domain_the_registrar_cannot_sell(setup, registrar):
    """The registrar's own no."""
    client, _, operator, organization, _ = setup
    registrar.availability = Availability("libre.fr", False, reason="premium")

    response = client.post(
        url(operator, organization, "check/"), {"domain": "libre.fr"}, format="json"
    )

    assert response.json()["registrable"] is False
    assert response.json()["reasons"] == ["unavailable"]


def test_check_reports_a_registrar_that_does_not_answer(setup, registrar):
    """The user is told to try again."""
    client, _, operator, organization, _ = setup
    registrar.availability = Boom("down")

    response = client.post(
        url(operator, organization, "check/"), {"domain": "libre.fr"}, format="json"
    )

    assert response.status_code == 502


# ---------------------------------------------------------------------------
# Suggestions


def test_suggestions_propose_free_candidates(setup):
    """Names the organization could register, that look free."""
    client, _, operator, _, service = setup
    organization = factories.OrganizationFactory(
        name="Saint-Malo", type="commune", departement_code_insee="35"
    )
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    factories.ServiceSubscriptionFactory(
        organization=organization, operator=operator, service=service, is_active=True
    )

    response = client.get(url(operator, organization, "suggestions/"))

    assert response.status_code == 200
    domains = response.json()["domains"]
    assert "saint-malo.fr" in domains
    assert "saint-malo.bzh" in domains
    assert response.json()["address"] == {
        "address1": "",
        "zip_code": organization.code_postal,
        "city": "",
    }


# ---------------------------------------------------------------------------
# Nameservers and refresh


def test_nameservers_are_recorded_then_pushed_by_the_task(setup, registrar):
    """A new wish is recorded; the registrar is asked by the next run."""
    client, _, operator, organization, service = setup
    registration = factories.DomainRegistrationFactory(
        organization=organization,
        operator=operator,
        service=service,
        status=Status.ACTIVE,
        next_check_at=None,
    )
    registrar.state = state(
        "active", "ACTIVE, DELEGATED", nameservers=["ns1.ancien.fr", "ns2.ancien.fr"]
    )

    response = client.patch(
        url(operator, organization, f"{registration.id}/"),
        {"nameservers": ["NS1.Nouveau.fr", "ns2.nouveau.fr"]},
        format="json",
    )

    assert response.status_code == 200
    assert response.json()["desired_nameservers"] == [
        "ns1.nouveau.fr",
        "ns2.nouveau.fr",
    ]
    assert not registrar.calls

    registrations.process_due()

    assert (
        "set_nameservers",
        registration.domain,
        ["ns1.nouveau.fr", "ns2.nouveau.fr"],
    ) in registrar.calls


@pytest.mark.parametrize(
    "nameservers", [["ns1.seul.fr"], ["pas valide", "ns.a.fr"], "ns.a.fr"]
)
def test_nameservers_must_be_a_valid_list(setup, registrar, nameservers):
    """Two to five hostnames."""
    client, _, operator, organization, service = setup
    registration = factories.DomainRegistrationFactory(
        organization=organization, operator=operator, service=service
    )
    response = client.patch(
        url(operator, organization, f"{registration.id}/"),
        {"nameservers": nameservers},
        format="json",
    )
    assert response.status_code == 400
    assert not registrar.calls


def test_refresh_has_the_registrar_asked_at_the_next_run(setup, registrar):
    """The check is brought forward to the next run, not made while the user waits."""
    client, _, operator, organization, service = setup
    registration = factories.DomainRegistrationFactory(
        organization=organization,
        operator=operator,
        service=service,
        status=Status.PENDING,
        next_check_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    registrar.state = state("active", "ACTIVE, DELEGATED", nameservers=NAMESERVERS)

    response = client.post(url(operator, organization, f"{registration.id}/refresh/"))

    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    assert not registrar.calls

    registrations.process_due()

    registration.refresh_from_db()
    assert registrar.names() == ["get_domain"]
    assert registration.status == Status.ACTIVE


def test_another_operators_domain_cannot_be_changed(setup, registrar):
    """A domain in another operator's registrar account is not ours to touch."""
    client, _, operator, organization, service = setup
    registration = factories.DomainRegistrationFactory(
        organization=organization,
        service=service,
        operator=factories.OperatorFactory(),
    )

    changed = client.patch(
        url(operator, organization, f"{registration.id}/"),
        {"nameservers": NAMESERVERS},
        format="json",
    )
    refreshed = client.post(url(operator, organization, f"{registration.id}/refresh/"))

    assert changed.status_code == 403
    assert refreshed.status_code == 403
    assert not registrar.calls
