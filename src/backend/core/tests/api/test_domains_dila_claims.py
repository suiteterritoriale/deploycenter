"""Tests that a domains subscription cannot take a domain DILA gives to another org."""

import pytest
from rest_framework.test import APIClient

from core import factories, models
from core.services import domains as domains_service

pytestmark = pytest.mark.django_db


def _setup(is_superuser=False):
    """An operator member, an organization and a domains service."""
    user = factories.UserFactory(is_superuser=is_superuser)
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory()
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(type="domains")
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    client = APIClient()
    client.force_login(user)
    return client, operator, organization, service


def _dila_organization(*domains):
    """An organization that declared these domains at DILA."""
    return factories.OrganizationFactory(
        proconnect_domains={
            "dpnt": list(domains),
            "candidates": [],
            "manual": [],
            "requested": [],
            "discarded": [],
        }
    )


def _patch(client, operator, organization, service, data):
    return client.patch(
        f"/api/v1.0/operators/{operator.id}/organizations/{organization.id}"
        f"/services/{service.id}/subscription/",
        data,
        format="json",
    )


def test_a_domain_declared_at_dila_by_another_organization_is_refused():
    """An operator member cannot take the DILA domain of an organization."""
    client, operator, organization, service = _setup()
    _dila_organization("ville-b.fr")

    response = _patch(
        client,
        operator,
        organization,
        service,
        {"is_active": True, "metadata": {"domains": ["Ville-B.fr"]}},
    )

    assert response.status_code == 400
    assert "ville-b.fr" in str(response.json())
    assert not models.ServiceSubscription.objects.filter(
        organization=organization
    ).exists()


def test_the_dila_parking_page_of_the_owner_survives_a_refused_claim():
    """The reported takeover: the export keeps the owner's page, not the claimant's."""
    client, operator, organization, service = _setup()
    owner = _dila_organization("ville-b.fr")
    factories.OperatorOrganizationRoleFactory(operator=operator, organization=owner)
    factories.ServiceSubscriptionFactory(
        organization=owner,
        service=service,
        operator=operator,
        is_active=True,
        metadata={"domains": []},
    )

    _patch(
        client,
        operator,
        organization,
        service,
        {
            "is_active": True,
            "metadata": {
                "domains": ["ville-b.fr"],
                "website": {"ville-b.fr": {"mode": "dns_a", "target": "203.0.113.7"}},
            },
        },
    )

    entries = domains_service.export_domains()
    assert [(e["domain"], e["organization"]["id"], e["source"]) for e in entries] == [
        ("ville-b.fr", str(owner.id), "dpnt")
    ]


def test_a_superuser_can_declare_a_domain_of_another_organization():
    """Superusers keep the override."""
    client, operator, organization, service = _setup(is_superuser=True)
    _dila_organization("ville-b.fr")

    response = _patch(
        client,
        operator,
        organization,
        service,
        {"is_active": True, "metadata": {"domains": ["ville-b.fr"]}},
    )

    assert response.status_code == 201


def test_an_organization_can_declare_its_own_dila_domain():
    """The domains an organization declared at DILA are its to declare."""
    client, operator, organization, service = _setup()
    organization.proconnect_domains = {"dpnt": ["ville-a.fr"]}
    organization.save()

    response = _patch(
        client,
        operator,
        organization,
        service,
        {"is_active": True, "metadata": {"domains": ["ville-a.fr"]}},
    )

    assert response.status_code == 201


def test_a_domain_several_organizations_declare_at_dila_is_allowed_to_each():
    """A shared domain belongs to none of them alone, but each may declare it."""
    client, operator, organization, service = _setup()
    organization.proconnect_domains = {"dpnt": ["shared.fr"]}
    organization.save()
    _dila_organization("shared.fr")

    response = _patch(
        client,
        operator,
        organization,
        service,
        {"is_active": True, "metadata": {"domains": ["shared.fr"]}},
    )

    assert response.status_code == 201


def test_a_domain_nobody_declares_at_dila_is_allowed():
    """Only DILA's attribution is checked: any other domain is free."""
    client, operator, organization, service = _setup()
    _dila_organization("ville-b.fr")

    response = _patch(
        client,
        operator,
        organization,
        service,
        {"is_active": True, "metadata": {"domains": ["autre.fr"]}},
    )

    assert response.status_code == 201


def test_a_stored_domain_of_another_organization_does_not_block_other_edits():
    """Only the domains a request adds are checked."""
    client, operator, organization, service = _setup()
    _dila_organization("ville-b.fr")
    subscription = factories.ServiceSubscriptionFactory(
        organization=organization,
        service=service,
        operator=operator,
        is_active=True,
        metadata={"domains": ["ville-b.fr"]},
    )

    # Deactivating, resending the same domains, and adding a free one.
    assert (
        _patch(
            client, operator, organization, service, {"is_active": False}
        ).status_code
        == 200
    )
    response = _patch(
        client,
        operator,
        organization,
        service,
        {"metadata": {"domains": ["ville-b.fr", "autre.fr"]}},
    )

    assert response.status_code == 200
    subscription.refresh_from_db()
    assert subscription.metadata["domains"] == ["autre.fr", "ville-b.fr"]
