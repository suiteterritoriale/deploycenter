"""Tests that the Django admin is reserved to superusers."""

from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse

import pytest

from core import factories, models
from core.admin_site import SuperuserAdminSite

pytestmark = pytest.mark.django_db

CODE = "99999999900017"


def _client(**user_kwargs):
    client = Client()
    client.force_login(factories.UserFactory(**user_kwargs))
    return client


def _staff_with_every_permission():
    """A staff user who is not a superuser, but holds every model permission."""
    user = factories.UserFactory(is_staff=True, is_superuser=False)
    user.user_permissions.set(Permission.objects.all())
    client = Client()
    client.force_login(user)
    return client


def test_the_admin_site_is_the_superuser_one():
    """INSTALLED_APPS swaps the admin app, or the rule below would not apply."""
    assert isinstance(site, SuperuserAdminSite)


def test_a_staff_superuser_reaches_the_admin():
    """Superusers keep the admin."""
    client = _client(is_staff=True, is_superuser=True)

    assert client.get(reverse("admin:index")).status_code == 200
    assert client.get(reverse("admin:core_account_bulk_import")).status_code == 200


def test_a_superuser_who_is_not_staff_is_refused():
    """Like any admin user, it also takes the staff flag."""
    client = _client(is_staff=False, is_superuser=True)

    assert client.get(reverse("admin:index")).status_code == 302


@pytest.mark.parametrize(
    "url_name",
    [
        "admin:index",
        "admin:core_account_changelist",
        "admin:core_account_add",
        "admin:core_account_bulk_import",
        "admin:core_operatororganizationrole_bulk_import",
        "admin:core_servicesubscription_bulk_subscribe",
        "admin:core_servicesubscription_bulk_operator_subscribe",
        "admin:core_user_add_passwordless",
    ],
)
def test_a_staff_user_with_every_permission_cannot_open_an_admin_page(url_name):
    """Model permissions open nothing: the user is sent to the login page."""
    client = _staff_with_every_permission()

    response = client.get(reverse(url_name))

    assert response.status_code == 302
    assert "login" in response["Location"]


def test_a_staff_user_with_every_permission_cannot_write_through_the_admin():
    """The custom views that skip the model permission checks write nothing."""
    client = _staff_with_every_permission()
    operator = factories.OperatorFactory()
    organization = factories.OrganizationFactory(siret=CODE)
    service = factories.ServiceFactory(type="drive")
    role = factories.OperatorOrganizationRoleFactory(
        operator=operator,
        organization=organization,
        operator_admins_have_admin_role=False,
    )

    posts = [
        (
            "admin:core_account_bulk_import",
            {
                "global_roles": '["admin"]',
                "service": "",
                "service_roles": "[]",
                "account_data": f"{CODE},limited@example.org",
            },
        ),
        (
            "admin:core_operatororganizationrole_bulk_import",
            {
                "operator": operator.id,
                "role": "admin",
                "operator_admins_have_admin_role": "on",
                "siret_list": CODE,
            },
        ),
        (
            "admin:core_servicesubscription_bulk_subscribe",
            {
                "operator": operator.id,
                "service": service.id,
                "siret_list": CODE,
                "metadata": "{}",
                "is_active": "on",
            },
        ),
        ("admin:core_user_add_passwordless", {"email": "ghost@example.org"}),
        (
            "admin:core_operatororganizationrole_changelist",
            {
                "action": "enable_operator_admins_have_admin_role",
                "_selected_action": [str(role.pk)],
            },
        ),
        (
            "admin:core_service_changelist",
            {"action": "duplicate_service", "_selected_action": [str(service.pk)]},
        ),
    ]
    for url_name, data in posts:
        assert client.post(reverse(url_name), data).status_code == 302

    role.refresh_from_db()
    assert not role.operator_admins_have_admin_role
    assert not models.Account.objects.filter(email="limited@example.org").exists()
    assert not models.ServiceSubscription.objects.filter(
        organization=organization
    ).exists()
    assert not models.User.objects.filter(email="ghost@example.org").exists()
    assert models.Service.objects.count() == 1
