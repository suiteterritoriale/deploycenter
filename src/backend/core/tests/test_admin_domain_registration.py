"""Tests for the admin of registrar accounts and domain registrations."""

import json

from django.test import Client

import pytest

from core import factories, models
from core.services import registrations

from .fake_registrar import configure_account
from .test_registrars import netim_config

pytestmark = pytest.mark.django_db

Status = models.DomainRegistration.Status


def _admin_client():
    user = factories.UserFactory(is_staff=True, is_superuser=True)
    client = Client()
    client.force_login(user)
    return client


def _config_form(config, **overrides):
    return {
        "operator": str(config.operator_id),
        "service": str(config.service_id),
        "display_priority": "1",
        "config_override": json.dumps(config.config_override),
        "api_secret": "",
        **overrides,
    }


def _change(client, config, **overrides):
    return client.post(
        f"/admin/core/operatorserviceconfig/{config.pk}/change/",
        _config_form(config, **overrides),
    )


@pytest.fixture(name="config")
def registrar_account():
    """A registrar account, as configured in the admin."""
    return configure_account(
        factories.OperatorFactory(),
        factories.ServiceFactory(type=registrations.SERVICE_TYPE),
    )


def test_the_secret_is_never_shown_back(config):
    """The edit form does not carry the stored secret."""
    response = _admin_client().get(
        f"/admin/core/operatorserviceconfig/{config.pk}/change/"
    )
    assert response.status_code == 200
    assert "s3cret" not in response.content.decode()


def test_leaving_the_secret_empty_keeps_it(config):
    """Saving the account without retyping the secret does not erase it."""
    response = _change(_admin_client(), config)
    assert response.status_code == 302
    config.refresh_from_db()
    assert config.api_secret == "s3cret"


def test_typing_a_secret_replaces_it(config):
    """A new secret is stored in place of the old one."""
    response = _change(_admin_client(), config, api_secret="n3w")
    assert response.status_code == 302
    config.refresh_from_db()
    assert config.api_secret == "n3w"


def test_an_incomplete_account_is_refused(config):
    """The configuration is validated when it is saved."""
    response = _change(
        _admin_client(), config, config_override=json.dumps({"registrar": "netim"})
    )
    assert response.status_code == 200
    assert "environment must be one of" in response.content.decode()
    config.refresh_from_db()
    assert config.config_override == netim_config()


def test_the_registrations_list_loads(config):
    """The change list and an existing registration open."""
    registration = factories.DomainRegistrationFactory(
        operator=config.operator, service=config.service
    )
    client = _admin_client()
    assert client.get("/admin/core/domainregistration/").status_code == 200
    assert (
        client.get(
            f"/admin/core/domainregistration/{registration.pk}/change/"
        ).status_code
        == 200
    )


def test_adding_a_registration_records_an_existing_domain(config):
    """An existing domain is recorded as unverified, to be looked up at its registrar."""
    organization = factories.OrganizationFactory()

    response = _admin_client().post(
        "/admin/core/domainregistration/add/",
        {
            "domain": "Ancien.FR",
            "organization": str(organization.pk),
            "operator": str(config.operator_id),
            "service": str(config.service_id),
            "desired_nameservers": "[]",
        },
    )

    assert response.status_code == 302
    registration = models.DomainRegistration.objects.get(domain="ancien.fr")
    assert registration.origin == "imported"
    assert registration.status == Status.UNVERIFIED
    assert registration.registrar == "netim"
    assert registration.next_check_at is not None


def test_adding_a_registration_needs_an_account():
    """An operator without an account at a registrar cannot have domains recorded."""
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    operator = factories.OperatorFactory()

    response = _admin_client().post(
        "/admin/core/domainregistration/add/",
        {
            "domain": "ancien.fr",
            "organization": str(factories.OrganizationFactory().pk),
            "operator": str(operator.pk),
            "service": str(service.pk),
            "desired_nameservers": "[]",
        },
    )

    assert response.status_code == 200
    assert "no configuration for the service" in response.content.decode()
    assert not models.DomainRegistration.objects.exists()


def test_the_check_now_action_schedules_the_domains(config):
    """The action brings the next check of the selected domains forward."""
    later = factories.DomainRegistrationFactory(
        operator=config.operator,
        service=config.service,
        next_check_at="2030-01-01T00:00:00Z",
    )

    response = _admin_client().post(
        "/admin/core/domainregistration/",
        {"action": "check_now", "_selected_action": [str(later.pk)]},
    )

    assert response.status_code == 302
    later.refresh_from_db()
    assert later.next_check_at.year < 2030
