"""Tests that a deactivated user's operator role grants nothing to the partners."""

import pytest
from rest_framework.test import APIClient

from core import factories
from core.services.bal import CHANNELS

pytestmark = pytest.mark.django_db

# Above the default auto-admin population threshold, so that the population
# fallback does not grant anything on its own.
LARGE_POPULATION = 50000
EMAIL = "admin@operator.fr"

# The rights are granted to an active user and withdrawn from a deactivated one.
is_active_cases = pytest.mark.parametrize("is_active", [True, False])


def _setup(service_type, is_active):
    """An operator admin holding rights on a commune, plus an active colleague.

    The colleague proves that the operator having another active admin does not
    keep the rights of the deactivated one.
    """
    operator = factories.OperatorFactory(is_active=True)
    user = factories.UserFactory(email=EMAIL, is_active=is_active)
    factories.UserOperatorRoleFactory(user=user, operator=operator)
    colleague = factories.UserFactory(email="colleague@operator.fr", is_active=True)
    factories.UserOperatorRoleFactory(user=colleague, operator=operator)

    commune = factories.OrganizationFactory(type="commune", population=LARGE_POPULATION)
    factories.OperatorOrganizationRoleFactory(
        operator=operator,
        organization=commune,
        role="admin",
        operator_admins_have_admin_role=True,
    )
    service = factories.ServiceFactory(
        type=service_type, config={"entitlements_api_key": "test_token"}
    )
    factories.ServiceSubscriptionFactory(
        organization=commune,
        service=service,
        operator=operator,
        is_active=True,
        metadata={"domains": ["commune.fr"]} if service_type == "messages" else {},
    )
    return operator, commune, service


def _entitlements(service, commune, **params):
    response = APIClient().get(
        "/api/v1.0/entitlements/",
        query_params={
            "service_id": service.id,
            "account_type": "user",
            "siret": commune.siret,
            **({"account_email": EMAIL} if not params else params),
        },
        headers={"X-Service-Auth": "Bearer test_token"},
    )
    assert response.status_code == 200
    return response.json()["entitlements"]


@is_active_cases
@pytest.mark.parametrize("service_type", ["drive", "esd", "adc"])
def test_base_passthrough_needs_an_active_user(service_type, is_active):
    """The operator admin passthrough of the base resolver, ADC and ESD included."""
    _operator, commune, service = _setup(service_type, is_active)

    assert _entitlements(service, commune)["is_admin"] is is_active


@is_active_cases
def test_can_admin_operators_needs_an_active_user(is_active):
    """A deactivated user is no longer announced as an operator admin."""
    operator, commune, service = _setup("adc", is_active)

    operators = _entitlements(service, commune)["can_admin_operators"]

    assert [o["id"] for o in operators] == ([str(operator.id)] if is_active else [])


@is_active_cases
def test_can_admin_collectivites_needs_an_active_user(is_active):
    """The communes of the operator are no longer granted to a deactivated user."""
    _operator, commune, service = _setup("adc", is_active)

    communes = _entitlements(service, commune)["can_admin_collectivites"]

    assert communes == ([commune.siren] if is_active else [])


@is_active_cases
def test_messages_passthrough_needs_an_active_user(is_active):
    """The mail domains of the operator's organizations need an active user."""
    _operator, commune, service = _setup("messages", is_active)

    domains = _entitlements(service, commune)["can_admin_maildomains"]

    assert domains == (["commune.fr"] if is_active else [])


@is_active_cases
def test_bal_passthrough_needs_an_active_user(is_active):
    """The communes of the operator's organizations need an active user."""
    _operator, commune, service = _setup("bal", is_active)

    communes = _entitlements(service, commune)["can_admin_communes"]

    assert communes == ({commune.code_insee: CHANNELS} if is_active else {})


def test_a_deactivated_user_keeps_the_rights_of_their_own_account():
    """Account roles are not tied to a Deploy Center user: only the operator role goes."""
    _operator, commune, service = _setup("drive", is_active=False)
    factories.AccountFactory(
        organization=commune,
        type="user",
        email=EMAIL,
        external_id="xyz",
        roles=["admin"],
    )

    entitlements = _entitlements(service, commune, account_id="xyz")

    assert entitlements["is_admin"] is True
    assert entitlements["is_admin_resolve_level"] == "organization"
