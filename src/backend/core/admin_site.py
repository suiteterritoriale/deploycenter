"""The Django admin site, reserved to superusers."""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig


class SuperuserAdminSite(AdminSite):
    """An admin site that only staff superusers can use.

    ``has_permission`` is what every admin page and every ``admin_site.admin_view``
    custom route asks first. Model permissions are therefore never the only thing
    between a staff user and the custom views, which do not check them.
    """

    def has_permission(self, request):
        """Allow active staff users who are also superusers."""
        return super().has_permission(request) and request.user.is_superuser


class SuperuserAdminConfig(AdminConfig):
    """Replaces ``django.contrib.admin`` in INSTALLED_APPS to swap the admin site."""

    default_site = "core.admin_site.SuperuserAdminSite"
