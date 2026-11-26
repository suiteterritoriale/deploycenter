"""Tests for the migration that normalizes the domains of Messages subscriptions."""

from importlib import import_module

from django.apps import apps

import pytest

from core import factories

pytestmark = pytest.mark.django_db

migration = import_module("core.migrations.0023_normalize_messages_domains")


def _subscription(service_type, metadata):
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory()
    factories.OperatorOrganizationRoleFactory(
        operator=operator, organization=organization
    )
    service = factories.ServiceFactory(type=service_type)
    factories.OperatorServiceConfigFactory(operator=operator, service=service)
    return factories.ServiceSubscriptionFactory(
        organization=organization,
        service=service,
        operator=operator,
        metadata=metadata,
    )


def test_messages_domains_are_stripped_lowercased_and_sorted():
    """Stored forms like "Town.FR " become what the API would have stored."""
    subscription = _subscription(
        "messages",
        {"domains": ["Victim-Town.FR", " b.fr\t", "a.fr", "a.fr", " "], "other": 1},
    )

    migration.normalize_messages_domains(apps, None)

    subscription.refresh_from_db()
    assert subscription.metadata == {
        "domains": ["a.fr", "b.fr", "victim-town.fr"],
        "other": 1,
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"domains": "Town.FR"},
        {"domains": ["Town.FR", 1]},
        {"domains": ["a.fr"]},
        ["Town.FR"],
        "Town.FR",
    ],
)
def test_other_messages_metadata_is_left_alone(metadata):
    """Rows without a clean list of strings, or already normalized, do not change.

    That includes metadata that is not an object at all.
    """
    subscription = _subscription("messages", metadata)

    migration.normalize_messages_domains(apps, None)

    subscription.refresh_from_db()
    assert subscription.metadata == metadata


def test_other_services_are_left_alone():
    """Only Messages grants its domains as stored."""
    subscription = _subscription("domains", {"domains": ["Town.FR"]})

    migration.normalize_messages_domains(apps, None)

    subscription.refresh_from_db()
    assert subscription.metadata == {"domains": ["Town.FR"]}
