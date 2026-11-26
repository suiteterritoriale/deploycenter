"""Tests for the admin box that bypasses the population limits of a subscription."""

from django.test import Client
from django.urls import reverse

import pytest

from core import factories, models

pytestmark = pytest.mark.django_db

BOX = "bypass_population_limits"


def _admin_client():
    client = Client()
    client.force_login(factories.UserFactory(is_staff=True, is_superuser=True))
    return client


def _setup(service_kwargs=None):
    """A commune above the population limit of a service its operator offers."""
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory(
        type="commune", population=5000, epci_population=5000
    )
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(
        config={"population_limits": {"commune": 1000, "epci": 1000}},
        **(service_kwargs or {}),
    )
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    return operator, organization, service


def _data(operator, organization, service, **overrides):
    return {
        "organization": str(organization.pk),
        "operator": str(operator.pk),
        "service": str(service.pk),
        "is_active": "on",
        "metadata": "{}",
        **overrides,
    }


def _add(client, operator, organization, service, **overrides):
    return client.post(
        reverse("admin:core_servicesubscription_add"),
        _data(operator, organization, service, **overrides),
    )


def _subscriptions(organization):
    return models.ServiceSubscription.objects.filter(organization=organization)


def test_the_box_is_on_the_add_form_and_off_by_default():
    """The add form shows an unchecked box."""
    response = _admin_client().get(reverse("admin:core_servicesubscription_add"))

    assert response.status_code == 200
    form = response.context["adminform"].form
    assert form[BOX].value() is False
    assert f'name="{BOX}"' in response.content.decode()
    assert f'name="{BOX}" checked' not in response.content.decode()


def test_an_organization_above_the_limit_is_refused_without_the_box():
    """Without the box, the limit applies as before."""
    operator, organization, service = _setup()

    response = _add(_admin_client(), operator, organization, service)

    assert response.status_code == 200
    assert "population_limit_exceeded" in response.content.decode()
    assert not _subscriptions(organization).exists()


def test_an_organization_above_the_limit_is_created_with_the_box():
    """The box creates the active subscription despite the limit."""
    operator, organization, service = _setup()

    response = _add(_admin_client(), operator, organization, service, **{BOX: "on"})

    assert response.status_code == 302
    assert _subscriptions(organization).get().is_active


def test_the_box_does_not_skip_the_required_services():
    """Only the population limit is bypassed."""
    operator, organization, service = _setup()
    service.required_services.add(factories.ServiceFactory())

    response = _add(_admin_client(), operator, organization, service, **{BOX: "on"})

    assert response.status_code == 200
    assert "missing_required_services" in response.content.decode()
    assert not _subscriptions(organization).exists()


def test_the_box_is_not_on_the_change_form_and_bypasses_nothing_there():
    """Activating an existing subscription still meets the limit."""
    operator, organization, service = _setup()
    subscription = factories.ServiceSubscriptionFactory(
        organization=organization, operator=operator, service=service, is_active=False
    )
    client = _admin_client()
    url = reverse("admin:core_servicesubscription_change", args=[subscription.pk])

    assert BOX not in client.get(url).content.decode()
    response = client.post(
        url,
        _data(operator, organization, service, **{BOX: "on"}),
    )

    assert response.status_code == 200
    subscription.refresh_from_db()
    assert not subscription.is_active
