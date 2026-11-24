"""Tests for the domains of a Messages subscription: a domain has a single holder."""

import pytest
from rest_framework.test import APIClient

from core import factories, models

pytestmark = pytest.mark.django_db


def _setup(user):
    """Create an operator member, an organization and a Messages service."""
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory()
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(
        type="messages", config={"entitlements_api_key": "test_token"}
    )
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    return operator, organization, service


def _url(operator, organization, service):
    return (
        f"/api/v1.0/operators/{operator.id}/organizations/{organization.id}"
        f"/services/{service.id}/subscription/"
    )


def _holder(operator, service, domains, is_active=True):
    """Create another organization whose Messages subscription holds domains."""
    other_organization = factories.OrganizationFactory()
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=other_organization
    )
    return factories.ServiceSubscriptionFactory(
        organization=other_organization,
        service=service,
        operator=operator,
        is_active=is_active,
        metadata={"domains": domains},
    )


@pytest.mark.parametrize("is_superuser", [False, True])
@pytest.mark.parametrize("is_active", [True, False])
def test_messages_subscription_rejects_domain_held_elsewhere(is_superuser, is_active):
    """A domain held by another Messages subscription, active or not, is refused."""
    user = factories.UserFactory(is_superuser=is_superuser)
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    _holder(operator, service, ["victim-town.fr"], is_active=is_active)

    response = client.patch(
        _url(operator, organization, service),
        {"is_active": True, "metadata": {"domains": ["victim-town.fr"]}},
        format="json",
    )

    assert response.status_code == 400
    assert "victim-town.fr" in str(response.json())
    assert not models.ServiceSubscription.objects.filter(
        organization=organization
    ).exists()


def test_messages_subscription_rejects_domain_held_by_another_operator():
    """The holder may belong to an operator the caller has nothing to do with."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    _holder(factories.OperatorFactory(), service, ["victim-town.fr"])

    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": ["victim-town.fr"]}},
        format="json",
    )

    assert response.status_code == 400


def test_messages_subscription_rejects_domain_held_elsewhere_in_another_case():
    """The comparison is made on the normalized domain, as Messages does."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    _holder(operator, service, ["victim-town.fr"])

    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": [" Victim-Town.FR "]}},
        format="json",
    )

    assert response.status_code == 400


def test_messages_subscription_rejects_domain_added_to_an_existing_subscription():
    """Adding a held domain to an existing subscription is refused, the rest is kept."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    subscription = factories.ServiceSubscriptionFactory(
        organization=organization,
        service=service,
        operator=operator,
        is_active=True,
        metadata={"domains": ["mine.fr"]},
    )
    _holder(operator, service, ["victim-town.fr"])

    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": ["mine.fr", "victim-town.fr"]}},
        format="json",
    )

    assert response.status_code == 400
    subscription.refresh_from_db()
    assert subscription.metadata["domains"] == ["mine.fr"]


def test_messages_subscription_declares_free_domains_normalized():
    """A domain nobody holds is accepted, stored normalized."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)

    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": ["B.fr", "a.fr", "b.fr"]}},
        format="json",
    )

    assert response.status_code == 201
    assert response.json()["metadata"]["domains"] == ["a.fr", "b.fr"]


def test_messages_subscription_keeps_a_stored_duplicate_editable():
    """A duplicate already stored does not block an edit that does not add it."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    _holder(operator, service, ["shared.fr"])
    subscription = factories.ServiceSubscriptionFactory(
        organization=organization,
        service=service,
        operator=operator,
        is_active=True,
        metadata={"domains": ["shared.fr"]},
    )

    # Deactivating, resending the same domains, and adding a free one.
    response = client.patch(
        _url(operator, organization, service), {"is_active": False}, format="json"
    )
    assert response.status_code == 200
    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": ["shared.fr", "mine.fr"]}},
        format="json",
    )
    assert response.status_code == 200
    subscription.refresh_from_db()
    assert subscription.metadata["domains"] == ["mine.fr", "shared.fr"]


@pytest.mark.parametrize(
    "domains", ["victim-town.fr", [1], ["not a domain"], {"a.fr": 1}]
)
def test_messages_subscription_rejects_malformed_domains(domains):
    """The domains must be a list of well-formed domain names."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)

    response = client.patch(
        _url(operator, organization, service),
        {"metadata": {"domains": domains}},
        format="json",
    )

    assert response.status_code == 400


def test_messages_entitlements_do_not_grant_a_held_domain():
    """The reported attack: forging a held domain no longer reaches the entitlements."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    operator, organization, service = _setup(user)
    organization.siret = "12345678900001"
    organization.save()
    _holder(operator, service, ["victim-town.fr"])
    factories.AccountFactory(
        organization=organization,
        type="user",
        external_id="attacker",
        email="attacker@example.org",
        roles=["admin"],
    )

    client.patch(
        _url(operator, organization, service),
        {"is_active": True, "metadata": {"domains": ["victim-town.fr"]}},
        format="json",
    )

    response = APIClient().get(
        "/api/v1.0/entitlements/",
        {
            "service_id": service.id,
            "account_type": "user",
            "account_id": "attacker",
            "siret": organization.siret,
        },
        headers={"X-Service-Auth": "Bearer test_token"},
    )
    assert response.status_code == 200
    assert "victim-town.fr" not in response.json()["entitlements"].get(
        "can_admin_maildomains", []
    )
