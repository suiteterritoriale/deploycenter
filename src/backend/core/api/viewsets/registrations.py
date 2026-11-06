"""Domain registration API viewsets."""

import logging

from django.shortcuts import get_object_or_404

from drf_spectacular.utils import extend_schema
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response
from rest_framework.settings import api_settings

from core import models
from core.api import permissions
from core.authentication import OperatorExternalManagementApiKeyAuthentication
from core.services import registrations
from core.services.registrars import RegistrarConfigError, RegistrarError, get_client

logger = logging.getLogger(__name__)


class DomainRegistrationSerializer(serializers.ModelSerializer):
    """A domain registration, as the registrar last reported it.

    Meant to be built by the viewset (``get_serializer``): ``editable`` reads the
    operator of the request from it.
    """

    editable = serializers.SerializerMethodField()

    class Meta:
        """Every field is read only."""

        model = models.DomainRegistration
        fields = [
            "id",
            "domain",
            "status",
            "origin",
            "registrar",
            "registrar_status",
            "registrar_message",
            "nameservers",
            "desired_nameservers",
            "registered_at",
            "expires_at",
            "status_changed_at",
            "last_checked_at",
            "last_error",
            "created_at",
            "editable",
        ]
        read_only_fields = fields

    def get_editable(self, obj) -> bool:
        """Whether the registration is in the account of the request's operator."""
        return self.context["view"].owns(obj)


# pylint: disable=abstract-method
class DomainRegistrationCreateSerializer(serializers.Serializer):
    """The domain to register, and the address of the organization holding it."""

    domain = serializers.CharField()
    address1 = serializers.CharField(max_length=80)
    zip_code = serializers.CharField(max_length=10)
    city = serializers.CharField(max_length=30)


# pylint: disable=abstract-method
class DomainRegistrationCheckSerializer(serializers.Serializer):
    """The domain to check."""

    domain = serializers.CharField()


# pylint: disable=abstract-method
class DomainRegistrationNameserversSerializer(serializers.Serializer):
    """The nameservers a domain should be delegated to."""

    nameservers = serializers.ListField(child=serializers.CharField())


# pylint: disable=abstract-method
class DomainRegistrationCheckResultSerializer(serializers.Serializer):
    """The outcome of a check. Documentation/schema only."""

    domain = serializers.CharField()
    registrable = serializers.BooleanField()
    reasons = serializers.ListField(child=serializers.CharField())
    message = serializers.CharField()
    needs_review = serializers.BooleanField()
    nameservers = serializers.ListField(child=serializers.CharField())


# pylint: disable=abstract-method
class DomainRegistrationSuggestionsSerializer(serializers.Serializer):
    """Domains worth trying. Documentation/schema only."""

    domains = serializers.ListField(child=serializers.CharField())
    address = serializers.DictField(child=serializers.CharField(allow_blank=True))


class OrganizationDomainRegistrationViewSet(viewsets.GenericViewSet):
    """Domain registrations of an organization.

    GET    .../domain-registrations/                  the organization's domains
    POST   .../domain-registrations/                  register a domain
    PATCH  .../domain-registrations/<id>/             change its nameservers
    POST   .../domain-registrations/<id>/refresh/     have the registrar asked about it
    POST   .../domain-registrations/check/            can a domain be registered
    GET    .../domain-registrations/suggestions/      domains worth trying

    The organization needs an active subscription to the domain registration service,
    managed by the operator in the URL, and that operator an account at a registrar.

    Only ``check`` asks the registrar while the request waits. Everything that changes
    something at the registrar (registering, nameservers) and the refresh just record
    the request: the registrations task, which runs every minute, does the work.
    """

    queryset = models.DomainRegistration.objects.all()
    serializer_class = DomainRegistrationSerializer
    # An organization holds a handful of domains: the list is a plain array.
    pagination_class = None
    # Resolved from the URL for every request, see initial().
    operator = None
    organization = None
    service = None
    authentication_classes = [
        OperatorExternalManagementApiKeyAuthentication,
        *api_settings.DEFAULT_AUTHENTICATION_CLASSES,
    ]
    permission_classes = [
        permissions.IsAuthenticatedWithAnyMethod,
        permissions.OperatorAndOrganizationAccessPermission,
    ]

    def initial(self, request, *args, **kwargs):
        """Resolve the operator, the organization and the service of the request."""
        super().initial(request, *args, **kwargs)
        self.operator = get_object_or_404(
            models.Operator, pk=kwargs["operator_id"], is_active=True
        )
        self.organization = get_object_or_404(
            models.Organization, pk=kwargs["organization_id"]
        )
        subscription = (
            models.ServiceSubscription.objects.filter(
                organization=self.organization,
                operator=self.operator,
                service__type=registrations.SERVICE_TYPE,
                is_active=True,
            )
            .select_related("service")
            .first()
        )
        if subscription is None:
            raise NotFound(
                "Le service d'enregistrement de noms de domaine n'est pas activé."
            )
        self.service = subscription.service

    def get_queryset(self):
        """The registrations of the organization in the URL."""
        return super().get_queryset().filter(organization=self.organization)

    def _account(self):
        """The operator's registrar account, or a response saying it is not usable."""
        try:
            return registrations.operator_account(
                self.operator.pk, self.service.pk
            ), None
        except RegistrarConfigError as error:
            logger.warning("Registrar account of %s unusable: %s", self.operator, error)
            return None, Response(
                {
                    "detail": "Le compte de l'opérateur chez le registrar n'est pas configuré."
                },
                status=status.HTTP_409_CONFLICT,
            )

    def owns(self, registration) -> bool:
        """Whether the registration sits in the account of the request's operator."""
        return registration.operator_id == self.operator.pk

    def _own_registration(self):
        """The registration of the URL, if it sits in this operator's account."""
        registration = self.get_object()
        if not self.owns(registration):
            raise PermissionDenied("Ce nom de domaine est géré par un autre opérateur.")
        return registration

    @extend_schema(
        operation_id="domain_registrations_list",
        tags=["domain-registrations"],
        responses={200: DomainRegistrationSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        """GET the organization's domain registrations."""
        serializer = self.get_serializer(self.get_queryset(), many=True)
        return Response(serializer.data)

    @extend_schema(
        operation_id="domain_registrations_create",
        tags=["domain-registrations"],
        request=DomainRegistrationCreateSerializer,
        responses={201: DomainRegistrationSerializer},
    )
    def create(self, request, *args, **kwargs):
        """POST a request to register a domain."""
        payload = DomainRegistrationCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        _, refusal = self._account()
        if refusal:
            return refusal
        registration = registrations.request_registration(
            organization=self.organization,
            operator=self.operator,
            service=self.service,
            domain=data["domain"],
            address={
                "address1": data["address1"],
                "zip_code": data["zip_code"],
                "city": data["city"],
            },
            user=request.user if request.user.is_authenticated else None,
        )
        return Response(
            self.get_serializer(registration).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        operation_id="domain_registrations_nameservers",
        tags=["domain-registrations"],
        request=DomainRegistrationNameserversSerializer,
        responses={200: DomainRegistrationSerializer},
    )
    def partial_update(self, request, *args, **kwargs):
        """PATCH the nameservers a domain should be delegated to."""
        registration = self._own_registration()
        payload = DomainRegistrationNameserversSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        registrations.request_nameservers(
            registration, payload.validated_data["nameservers"]
        )
        return Response(self.get_serializer(registration).data)

    @extend_schema(
        operation_id="domain_registrations_refresh",
        tags=["domain-registrations"],
        request=None,
        responses={200: DomainRegistrationSerializer},
    )
    @action(detail=True, methods=["post"])
    def refresh(self, request, *args, **kwargs):
        """POST to have the registrar asked about a domain at the next run."""
        registration = registrations.check_now(self._own_registration())
        return Response(self.get_serializer(registration).data)

    @extend_schema(
        operation_id="domain_registrations_check",
        tags=["domain-registrations"],
        request=DomainRegistrationCheckSerializer,
        responses={200: DomainRegistrationCheckResultSerializer},
    )
    @action(detail=False, methods=["post"])
    def check(self, request, *args, **kwargs):
        """POST a domain to learn whether it can be registered.

        The name's rules, our own records and the DNS answer first; the registrar is
        only asked about a name that got through them.
        """
        payload = DomainRegistrationCheckSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        domain = payload.validated_data["domain"].strip().lower()
        account, refusal = self._account()
        if refusal:
            return refusal

        reasons = registrations.precheck([domain])[domain]
        needs_review = False
        if not reasons:
            try:
                with get_client(account) as client:
                    availability = client.check(domain)
            except RegistrarError as error:
                logger.warning("Availability check of %s failed: %s", domain, error)
                return Response(
                    {"detail": "Le registrar ne répond pas, veuillez réessayer."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )
            if not availability.available:
                reasons = [registrations.REASON_UNAVAILABLE]
            needs_review = availability.needs_review
        return Response(
            {
                "domain": domain,
                "registrable": not reasons,
                "reasons": reasons,
                "message": registrations.message_for(reasons),
                "needs_review": needs_review,
                "nameservers": registrations.expected_nameservers(),
            }
        )

    @extend_schema(
        operation_id="domain_registrations_suggestions",
        tags=["domain-registrations"],
        responses={200: DomainRegistrationSuggestionsSerializer},
    )
    @action(detail=False, methods=["get"])
    def suggestions(self, request, *args, **kwargs):
        """GET domains the organization could register, that look free.

        With the address to propose for the holder: the organization's own.
        """
        return Response(
            {
                "domains": registrations.suggestions(self.organization),
                "address": registrations.address_defaults(self.organization),
            }
        )
