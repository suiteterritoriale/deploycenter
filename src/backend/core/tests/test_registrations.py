"""Tests for the domain registration workflow (core/services/registrations.py)."""

# pylint: disable=too-many-lines,redefined-outer-name

from datetime import date, datetime, timedelta, timezone
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import connection

import pytest

from core import factories, models
from core.services import dns as dns_service
from core.services import registrations
from core.services.registrars import Availability, Operation, RegistrarRejected

from .fake_registrar import Boom, FakeRegistrar, configure_account, state

pytestmark = pytest.mark.django_db

Status = models.DomainRegistration.Status
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)
NAMESERVERS = ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
REGISTRANT = {
    "organization": "Commune de Test",
    "identifier": "123456789",
    "address1": "1 place de la Mairie",
    "zip_code": "29000",
    "city": "Quimper",
    "country": "FR",
    "email": "domaines@operateur.example",
    "phone": "+33 123456789",
    "first_name": "Ada",
    "last_name": "Lovelace",
}
ADDRESS = {"address1": "1 place de la Mairie", "zip_code": "29000", "city": "Quimper"}


def make(status=Status.ACTIVE, **kwargs):
    """A registration in a given status, due now unless final."""
    final = status in models.DomainRegistration.FINAL_STATUSES
    kwargs.setdefault("next_check_at", None if final else NOW)
    kwargs.setdefault("status_changed_at", NOW)
    kwargs.setdefault("registrant", REGISTRANT)
    kwargs.setdefault("desired_nameservers", NAMESERVERS)
    return factories.DomainRegistrationFactory(status=status, **kwargs)


@pytest.fixture
def account():
    """An operator with a service and a working registrar account."""
    operator = factories.OperatorFactory()
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    configure_account(operator, service)
    return operator, service


# ---------------------------------------------------------------------------
# Model


def test_move_to_records_when_the_status_changed():
    """The status keeps the time it was entered."""
    registration = make(Status.PENDING)
    later = NOW + timedelta(hours=1)
    registration.move_to(Status.ACTIVE, later)
    assert registration.status == Status.ACTIVE
    assert registration.status_changed_at == later


def test_move_to_the_same_status_changes_nothing():
    """Being told the same thing again does not restart the clock."""
    registration = make(Status.ACTIVE)
    registration.move_to(Status.ACTIVE)
    assert registration.status_changed_at == NOW


@pytest.mark.parametrize("final", [Status.FAILED, Status.RELEASED])
def test_a_final_status_is_never_left(final):
    """Nothing happens to a failed or released domain."""
    registration = make(final)
    with pytest.raises(ValueError):
        registration.move_to(Status.ACTIVE)


def test_a_registration_is_never_requested_again_in_place():
    """Requested is where a registration starts, not somewhere it comes back to."""
    registration = make(Status.ACTIVE)
    with pytest.raises(ValueError):
        registration.move_to(Status.REQUESTED)


def test_a_final_registration_cannot_be_scheduled():
    """The database refuses a check scheduled for a domain nothing can happen to."""
    registration = make(Status.FAILED)
    registration.next_check_at = NOW
    with pytest.raises(ValidationError):
        registration.save()


def test_the_domain_is_normalized_and_must_be_valid():
    """Domains are stored lowercase, and only real ones."""
    registration = make(domain="  Exemple.FR ")
    assert registration.domain == "exemple.fr"
    with pytest.raises(ValidationError):
        make(domain="pas un domaine")


def test_a_domain_is_registered_once():
    """One record per domain, whoever holds it."""
    make(domain="exemple.fr")
    with pytest.raises(ValidationError):
        make(domain="exemple.fr")


def test_the_api_secret_is_encrypted_at_rest(account):
    """The database holds the secret encrypted, and the model gives it back."""
    operator, service = account
    config = models.OperatorServiceConfig.objects.get(
        operator=operator, service=service
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT api_secret FROM deploycenter_operator_service_config WHERE id = %s",
            [config.pk],
        )
        stored = cursor.fetchone()[0]
    assert stored and "s3cret" not in stored
    assert config.api_secret == "s3cret"


def test_a_registration_service_config_must_be_complete():
    """The operator's account is validated when it is configured."""
    operator = factories.OperatorFactory()
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    with pytest.raises(ValidationError) as error:
        models.OperatorServiceConfig.objects.create(
            operator=operator, service=service, config_override={"registrar": "netim"}
        )
    assert "config_override" in error.value.message_dict


def test_other_services_config_is_left_alone():
    """Only the registration service has such requirements."""
    config = factories.OperatorServiceConfigFactory(config_override={"anything": 1})
    assert config.api_secret is None


# ---------------------------------------------------------------------------
# Pre-validation


@pytest.mark.parametrize(
    "domain,reasons",
    [
        ("commune.fr", []),
        ("  Commune.FR ", []),
        ("commune.bzh", []),
        ("commune.alsace", []),
        ("pas un domaine", ["invalid"]),
        ("", ["invalid"]),
        ("stmearddegurçon.fr", ["invalid"]),
        ("xn--stmearddeguron-rjb.fr", ["internationalized"]),
        ("www.commune.fr", ["level"]),
        ("commune.gouv.fr", ["level"]),
        ("gouv.fr", ["reserved"]),
        ("commune.com", ["extension"]),
        ("commune.org", ["extension"]),
    ],
)
def test_validation_reasons(domain, reasons):
    """What can be told from the name alone."""
    assert registrations.validation_reasons(domain) == reasons


def resolver_says(known):
    """Patch the DNS: ``known`` maps a domain to its nameservers, the rest is NXDOMAIN."""

    def answer(domains):
        return {
            domain: (known[domain], None)
            if domain in known
            else ([], dns_service.ERROR_NXDOMAIN)
            for domain in domains
        }

    return mock.patch.object(dns_service, "nameservers_batch", side_effect=answer)


def test_precheck_turns_away_what_the_dns_knows():
    """A name with nameservers is taken; one that does not exist is worth asking about."""
    with resolver_says({"pris.fr": ["ns1.autre.fr"]}):
        result = registrations.precheck(["pris.fr", "libre.fr"])
    assert result == {"pris.fr": ["taken"], "libre.fr": []}


def test_precheck_takes_a_registered_name_without_nameservers_as_taken():
    """A name that exists but is delegated to nothing is registered all the same."""
    with mock.patch.object(
        dns_service,
        "nameservers_batch",
        return_value={"vide.fr": ([], dns_service.ERROR_NOT_DELEGATED)},
    ):
        assert registrations.precheck(["vide.fr"]) == {"vide.fr": ["taken"]}


@pytest.mark.parametrize(
    "error", [dns_service.ERROR_TIMEOUT, dns_service.ERROR_UNKNOWN]
)
def test_precheck_does_not_take_a_failed_lookup_for_an_answer(error):
    """When the DNS could not say, the registrar is left to."""
    with mock.patch.object(
        dns_service, "nameservers_batch", return_value={"incertain.fr": ([], error)}
    ):
        assert registrations.precheck(["incertain.fr"]) == {"incertain.fr": []}


def test_precheck_turns_away_what_we_already_hold():
    """A domain we follow is known, and the DNS is not asked about it."""
    make(domain="suivi.fr")
    with resolver_says({}) as patched:
        result = registrations.precheck(["suivi.fr", "invalide"])
    assert result == {"suivi.fr": ["known"], "invalide": ["invalid"]}
    assert patched.call_args.args[0] == []


def test_the_address_proposed_is_the_one_used_last():
    """An organization does not retype its address for each domain."""
    organization = factories.OrganizationFactory(code_postal="29000")
    assert registrations.address_defaults(organization) == {
        "address1": "",
        "zip_code": "29000",
        "city": "",
    }

    make(organization=organization, registrant=REGISTRANT)

    assert registrations.address_defaults(organization) == ADDRESS


def test_suggestions_are_the_candidates_that_look_free():
    """The candidates ProConnect proposes, minus the ones taken or already followed."""
    organization = factories.OrganizationFactory(
        name="Saint-Malo", type="commune", departement_code_insee="35"
    )
    make(domain="mairie-saint-malo.fr")
    with resolver_says({"saint-malo.fr": ["ns1.autre.fr"]}):
        found = registrations.suggestions(organization)
    assert "saint-malo.fr" not in found
    assert "mairie-saint-malo.fr" not in found
    assert "saint-malo.bzh" in found
    assert found == sorted(found)
    assert len(found) <= registrations.MAX_SUGGESTIONS


# ---------------------------------------------------------------------------
# Requests


def request(account, organization=None, **kwargs):
    """Request a registration with the usual defaults."""
    operator, service = account
    kwargs.setdefault("domain", "libre.fr")
    kwargs.setdefault("address", ADDRESS)
    with resolver_says({}):
        return registrations.request_registration(
            organization=organization or factories.OrganizationFactory(),
            operator=operator,
            service=service,
            now=NOW,
            **kwargs,
        )


def test_request_records_a_registration_to_submit(account):
    """The request is recorded, due at once, with the holder and the nameservers."""
    operator, _ = account
    organization = factories.OrganizationFactory(
        name="Commune de Test", siren="123456789"
    )
    user = factories.UserFactory()

    registration = request(account, organization, user=user)

    assert registration.status == Status.REQUESTED
    assert registration.origin == "registered"
    assert registration.registrar == "netim"
    assert registration.operator == operator
    assert registration.years == 10
    assert registration.next_check_at == NOW
    assert registration.requested_by == user
    assert registration.desired_nameservers == registrations.expected_nameservers()
    assert registration.registrant == REGISTRANT


def test_request_takes_the_siren_from_the_siret(account):
    """An organization known only by its SIRET is held under its SIREN."""
    organization = factories.OrganizationFactory(siren="", siret="12345678900012")
    assert request(account, organization).registrant["identifier"] == "123456789"


def test_request_needs_a_siren(account):
    """A collectivité without one cannot be named as a holder."""
    organization = factories.OrganizationFactory(siren="", siret="")
    with pytest.raises(ValidationError) as error:
        request(account, organization)
    assert "organization" in error.value.message_dict


def test_request_needs_an_address(account):
    """The street address is the organization's own, and is asked for."""
    with pytest.raises(ValidationError) as error:
        request(account, address={"address1": "", "zip_code": "29000"})
    assert set(error.value.message_dict) == {"address1", "city"}


def test_request_refuses_a_domain_that_cannot_be_registered(account):
    """The name is checked before anything is recorded."""
    with pytest.raises(ValidationError) as error:
        request(account, domain="commune.gouv.fr")
    assert "domain" in error.value.message_dict
    assert not models.DomainRegistration.objects.exists()


def test_request_refuses_a_domain_taken_in_the_dns(account):
    """A name the DNS knows is not asked of the registrar."""
    operator, service = account
    with resolver_says({"pris.fr": ["ns1.autre.fr"]}):
        with pytest.raises(ValidationError):
            registrations.request_registration(
                organization=factories.OrganizationFactory(),
                operator=operator,
                service=service,
                domain="pris.fr",
                address=ADDRESS,
            )


def test_request_refuses_a_domain_already_followed(account):
    """One record per domain."""
    make(domain="libre.fr", status=Status.ACTIVE)
    with pytest.raises(ValidationError):
        request(account)


def test_request_starts_a_failed_registration_over(account):
    """Asking again for a domain whose registration failed retries that record."""
    organization = factories.OrganizationFactory()
    failed = make(
        Status.FAILED,
        domain="libre.fr",
        organization=organization,
        registrar_message="refusé",
        failures=3,
        registrar_data={"contacts": {"owner": "x"}},
    )

    registration = request(account, organization)

    assert registration.pk == failed.pk
    assert registration.status == Status.REQUESTED
    assert registration.registrar_message == ""
    assert registration.failures == 0
    assert registration.registrar_data == {}
    assert registration.next_check_at == NOW


def test_request_does_not_take_over_another_organizations_failed_domain(account):
    """A failed record of someone else is not ours to reset."""
    make(Status.FAILED, domain="libre.fr")
    with pytest.raises(ValidationError):
        request(account)


def test_request_needs_a_configured_operator():
    """An operator without an account at a registrar cannot register."""
    operator = factories.OperatorFactory()
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    with pytest.raises(registrations.RegistrarConfigError):
        registrations.request_registration(
            organization=factories.OrganizationFactory(),
            operator=operator,
            service=service,
            domain="libre.fr",
            address=ADDRESS,
        )


def test_prepare_imported_schedules_a_first_look(account):
    """A domain that already exists is looked up before anything else."""
    operator, service = account
    registration = models.DomainRegistration(
        domain="ancien.fr",
        organization=factories.OrganizationFactory(),
        operator=operator,
        service=service,
    )
    registrations.prepare_imported(registration, now=NOW)
    registration.save()
    assert registration.origin == "imported"
    assert registration.status == Status.UNVERIFIED
    assert registration.registrar == "netim"
    assert registration.next_check_at == NOW


def test_request_nameservers_cleans_and_schedules():
    """The nameservers asked for are normalized, and pushed at the next run."""
    registration = make(Status.ACTIVE, next_check_at=NOW + timedelta(days=7))
    registrations.request_nameservers(
        registration, ["NS1.Autre.fr.", "ns2.autre.fr", "ns1.autre.fr"], now=NOW
    )
    registration.refresh_from_db()
    assert registration.desired_nameservers == ["ns1.autre.fr", "ns2.autre.fr"]
    assert registration.next_check_at == NOW


@pytest.mark.parametrize(
    "nameservers",
    [
        ["ns1.autre.fr"],
        [f"ns{i}.a.fr" for i in range(6)],
        ["pas valide", "ns.a.fr"],
        "ns.a.fr",
    ],
)
def test_request_nameservers_refuses_a_bad_list(nameservers):
    """Two to five valid hostnames."""
    with pytest.raises(ValidationError):
        registrations.request_nameservers(make(), nameservers)


def test_request_nameservers_for_a_final_domain_is_refused():
    """There is nothing to point at the nameservers any more."""
    with pytest.raises(ValidationError):
        registrations.request_nameservers(make(Status.RELEASED), NAMESERVERS)


def test_check_now_schedules_and_leaves_final_domains_alone():
    """Asking for a look brings the check forward, unless nothing can happen."""
    later = make(Status.ACTIVE, next_check_at=NOW + timedelta(days=7))
    registrations.check_now(later, now=NOW)
    later.refresh_from_db()
    assert later.next_check_at == NOW

    final = make(Status.RELEASED)
    registrations.check_now(final, now=NOW)
    final.refresh_from_db()
    assert final.next_check_at is None


# ---------------------------------------------------------------------------
# Scheduling


@pytest.mark.parametrize(
    "status,age,expected",
    [
        (Status.REQUESTED, timedelta(0), timedelta(0)),
        (Status.UNVERIFIED, timedelta(0), timedelta(0)),
        (Status.PENDING, timedelta(hours=1), timedelta(minutes=10)),
        (Status.PENDING, timedelta(days=3), timedelta(hours=1)),
        (Status.PENDING, timedelta(days=20), timedelta(hours=6)),
        (Status.REGISTERED, timedelta(days=1), timedelta(hours=1)),
        (Status.BLOCKED, timedelta(days=1), timedelta(hours=6)),
        (Status.EXPIRED, timedelta(days=1), timedelta(days=1)),
        (Status.FAILED, timedelta(days=1), None),
        (Status.RELEASED, timedelta(days=1), None),
    ],
)
def test_check_delay_follows_what_is_expected_next(status, age, expected):
    """Often while something is going on, never once nothing can."""
    registration = make(status, status_changed_at=NOW - age)
    assert registrations.check_delay(registration, NOW) == expected


@pytest.mark.parametrize(
    "registered,expires,expected",
    [
        (date(2026, 10, 20), date(2036, 10, 20), timedelta(days=1)),
        (date(2025, 1, 1), date(2036, 1, 1), timedelta(days=7)),
        (date(2025, 1, 1), date(2027, 1, 20), timedelta(days=1)),
        (None, None, timedelta(days=7)),
    ],
)
def test_check_delay_of_an_active_domain(registered, expires, expected):
    """Daily while new or about to expire, weekly once in order."""
    registration = make(Status.ACTIVE, registered_at=registered, expires_at=expires)
    assert registrations.check_delay(registration, NOW) == expected


# ---------------------------------------------------------------------------
# Steps: submit


def test_submit_registers_the_domain():
    """Look it up, check it, create the contacts, ask for the registration."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registrar.names() == [
        "get_domain",
        "check",
        "create_contacts",
        "create_domain",
    ]
    assert registrar.calls[-1] == (
        "create_domain",
        registration.domain,
        10,
        NAMESERVERS,
    )
    assert registration.status == Status.PENDING
    assert registration.registrar_data == {
        "contacts": registrar.contacts,
        "operation": "42",
    }
    assert registration.next_check_at == NOW + registrations.FIRST_LOOK
    assert registration.failures == 0
    assert registration.last_checked_at == NOW


@pytest.mark.parametrize("state_", ["pending", "done"])
def test_submit_has_the_domain_looked_at_a_minute_later(state_):
    """Whether or not the registrar finished, the first look comes a minute on."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.create = Operation(state_, "7")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.PENDING
    assert registration.next_check_at == NOW + timedelta(minutes=1)


def test_submit_fails_when_the_registrar_cancels_the_registration():
    """A registration that failed at the registrar fails here, with its reason."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.create = Operation("failed", "7", "balance too low")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.FAILED
    assert registration.registrar_message == "balance too low"
    assert registration.next_check_at is None


def test_submit_fails_when_the_registrar_refuses():
    """A refusal is final: the user is told why."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.create = RegistrarRejected("holder not eligible")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.FAILED
    assert registration.registrar_message == "holder not eligible"
    assert registration.next_check_at is None


def test_submit_does_not_ask_for_a_domain_that_is_not_available():
    """The registrar's own check spares a registration that would be refused."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.availability = Availability(registration.domain, False, reason="reserved")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registrar.names() == ["get_domain", "check"]
    assert registration.status == Status.FAILED
    assert "reserved" in registration.registrar_message


def test_submit_adopts_a_domain_the_registrar_already_has():
    """An earlier attempt that went through is found, not paid for twice."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.state = state("pending", "PENDING")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registrar.names() == ["get_domain"]
    assert registration.status == Status.PENDING
    assert registration.registrar_status == "PENDING"


@pytest.mark.parametrize("step", ["state", "availability"])
def test_submit_retries_when_it_cannot_tell_if_the_domain_exists(step):
    """Whatever the registrar says before a registration, a failure to say is retried."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    setattr(registrar, step, Boom("down"))

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert "create_domain" not in registrar.names()
    assert registration.status == Status.REQUESTED
    assert registration.failures == 1
    assert registration.last_error == "down"
    assert registration.next_check_at == NOW + registrations.RETRY_BASE


def test_submit_does_not_fail_on_a_refusal_it_does_not_understand():
    """A refusal to a lookup is not a refusal of the registration."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.state = RegistrarRejected("bad request")

    registrations.submit(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.REQUESTED
    assert registration.failures == 1


def test_submit_keeps_the_contacts_it_created_when_the_registration_is_cut_short():
    """A retry does not create the contacts again."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.create = Boom("timeout")

    registrations.submit(registration, registrar, NOW)
    registration.refresh_from_db()
    assert registration.registrar_data["contacts"] == registrar.contacts
    assert registration.failures == 1

    registrar.create = Operation("pending", "42")
    registrar.calls.clear()
    registrations.submit(registration, registrar, NOW + timedelta(minutes=5))

    assert "create_contacts" not in registrar.names()
    registration.refresh_from_db()
    assert registration.status == Status.PENDING
    assert registration.failures == 0


def test_submit_keeps_trying_when_it_cannot_create_the_contacts():
    """A registrar that is down is waited for, with a longer wait each time."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()
    registrar.contacts = Boom("down")

    for _ in range(3):
        registrations.submit(registration, registrar, NOW)
    registration.refresh_from_db()

    assert registration.status == Status.REQUESTED
    assert registration.failures == 3
    assert registration.next_check_at == NOW + 4 * registrations.RETRY_BASE


def test_retry_delay_is_capped():
    """The wait stops growing."""
    registration = make(Status.REQUESTED, failures=40)
    registrations.submit(registration, _failing(), NOW)
    registration.refresh_from_db()
    assert registration.next_check_at == NOW + registrations.RETRY_CAP


def _failing():
    registrar = FakeRegistrar()
    registrar.state = Boom("down")
    return registrar


# ---------------------------------------------------------------------------
# Steps: refresh


def test_refresh_takes_what_the_registrar_reports():
    """Status, raw status, nameservers and dates."""
    registration = make(Status.PENDING)
    registrar = FakeRegistrar()
    registrar.state = state(
        "active",
        "ACTIVE, DELEGATED",
        nameservers=NAMESERVERS,
        registered_at=date(2026, 11, 5),
        expires_at=date(2036, 11, 5),
        message="ok",
    )

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.ACTIVE
    assert registration.registrar_status == "ACTIVE, DELEGATED"
    assert registration.registrar_message == "ok"
    assert registration.nameservers == NAMESERVERS
    assert registration.registered_at == date(2026, 11, 5)
    assert registration.expires_at == date(2036, 11, 5)
    assert registration.next_check_at == NOW + timedelta(days=1)
    assert registration.last_checked_at == NOW


def test_refresh_keeps_the_status_for_a_word_it_does_not_know():
    """An unknown status is recorded as is and does not move the domain."""
    registration = make(Status.ACTIVE)
    registrar = FakeRegistrar()
    registrar.state = state(None, "SOMETHING NEW")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.ACTIVE
    assert registration.registrar_status == "SOMETHING NEW"
    assert registration.next_check_at is not None


def test_refresh_moves_a_domain_the_registry_blocked():
    """A domain on hold is flagged, and looked at more often."""
    registration = make(Status.ACTIVE)
    registrar = FakeRegistrar()
    registrar.state = state("blocked", "HOLD, NOT DELEGATED")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.BLOCKED
    assert registration.status_changed_at == NOW
    assert registration.next_check_at == NOW + timedelta(hours=6)


def test_refresh_ends_a_registration_the_registrar_reports_failed():
    """A registration the registry gave up on is final."""
    registration = make(Status.PENDING)
    registrar = FakeRegistrar()
    registrar.state = state("failed", "ERROR")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.FAILED
    assert registration.next_check_at is None


def test_refresh_follows_the_operation_while_the_domain_is_not_there_yet():
    """A pending registration the registrar does not show yet is followed by its operation."""
    registration = make(Status.PENDING, registrar_data={"operation": "42"})
    registrar = FakeRegistrar()
    registrar.operation = Operation("failed", "42", "registry refused")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registrar.names() == ["get_domain", "get_operation"]
    assert registration.status == Status.FAILED
    assert registration.registrar_message == "registry refused"


def test_refresh_waits_for_a_pending_domain_that_is_not_visible_yet():
    """Not shown yet, and nothing to follow: still on its way."""
    registration = make(Status.PENDING)
    registrar = FakeRegistrar()

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.PENDING
    assert registration.failures == 0
    assert registration.next_check_at == NOW + timedelta(minutes=10)


def test_refresh_does_not_conclude_that_an_unverified_domain_is_not_ours():
    """An imported domain the account does not show is reported, not dropped."""
    registration = make(Status.UNVERIFIED)

    registrations.refresh(registration, FakeRegistrar(), NOW)

    registration.refresh_from_db()
    assert registration.status == Status.UNVERIFIED
    assert registration.failures == 1
    assert "not in the registrar account" in registration.last_error


def test_refresh_releases_a_domain_that_left_the_account():
    """A domain we knew and the registrar no longer has is gone."""
    registration = make(Status.ACTIVE)

    registrations.refresh(registration, FakeRegistrar(), NOW)

    registration.refresh_from_db()
    assert registration.status == Status.RELEASED
    assert registration.next_check_at is None
    assert registration.registrar_message


def test_refresh_backs_off_when_the_registrar_fails():
    """Nothing is concluded from a failed call."""
    registration = make(Status.ACTIVE)
    registrar = FakeRegistrar()
    registrar.state = Boom("down")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.ACTIVE
    assert registration.failures == 1
    assert registration.next_check_at == NOW + registrations.RETRY_BASE


def test_a_success_clears_the_failures():
    """The count is of failures in a row."""
    registration = make(Status.ACTIVE, failures=4, last_error="down")
    registrar = FakeRegistrar()
    registrar.state = state("active", "ACTIVE, DELEGATED")

    registrations.refresh(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.failures == 0
    assert registration.last_error == ""


# ---------------------------------------------------------------------------
# Steps: nameservers


def test_sync_nameservers_points_the_domain_at_the_desired_ones():
    """A registered domain delegated elsewhere is pointed at ours."""
    registration = make(Status.ACTIVE, nameservers=["ns1.autre.fr", "ns2.autre.fr"])
    registrar = FakeRegistrar()

    registrations.sync_nameservers(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registrar.calls == [("set_nameservers", registration.domain, NAMESERVERS)]
    assert registration.registrar_data["nameservers_push"]["nameservers"] == NAMESERVERS
    assert registration.next_check_at == NOW + registrations.NAMESERVERS_RECHECK


def test_sync_nameservers_leaves_a_domain_that_is_in_order():
    """Same nameservers, in any order: nothing to do."""
    registration = make(Status.ACTIVE, nameservers=list(reversed(NAMESERVERS)))
    registrar = FakeRegistrar()

    registrations.sync_nameservers(registration, registrar, NOW)

    assert not registrar.calls


@pytest.mark.parametrize(
    "status", [Status.PENDING, Status.BLOCKED, Status.EXPIRED, Status.UNVERIFIED]
)
def test_sync_nameservers_only_touches_a_registered_domain(status):
    """Nameservers are changed on a domain that is there to be changed."""
    registration = make(status, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()

    registrations.sync_nameservers(registration, registrar, NOW)

    assert not registrar.calls


def test_sync_nameservers_does_not_push_the_same_list_twice_in_a_row():
    """A change that takes time is not asked for again at every look."""
    registration = make(Status.REGISTERED, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()

    registrations.sync_nameservers(registration, registrar, NOW)
    registrations.sync_nameservers(registration, registrar, NOW + timedelta(hours=1))
    assert registrar.names() == ["set_nameservers"]

    registrations.sync_nameservers(
        registration,
        registrar,
        NOW + registrations.NAMESERVERS_RETRY + timedelta(minutes=1),
    )
    assert registrar.names() == ["set_nameservers", "set_nameservers"]


def test_sync_nameservers_pushes_a_new_wish_at_once():
    """A different list is not held back by the one pushed before."""
    registration = make(Status.ACTIVE, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()
    registrations.sync_nameservers(registration, registrar, NOW)

    registration.desired_nameservers = ["ns1.encore.fr", "ns2.encore.fr"]
    registrations.sync_nameservers(registration, registrar, NOW + timedelta(minutes=1))

    assert registrar.names() == ["set_nameservers", "set_nameservers"]


def test_sync_nameservers_reports_a_refusal_without_failing_the_domain():
    """The registrar's reason is kept to be shown; the domain stays as it is."""
    registration = make(Status.ACTIVE, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()
    registrar.nameservers = RegistrarRejected("zone check failed")

    registrations.sync_nameservers(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.status == Status.ACTIVE
    assert registration.registrar_message == "zone check failed"
    assert registration.failures == 0


def test_sync_nameservers_backs_off_when_the_registrar_fails():
    """An unavailable registrar is asked again later."""
    registration = make(Status.ACTIVE, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()
    registrar.nameservers = Boom("down")

    registrations.sync_nameservers(registration, registrar, NOW)

    registration.refresh_from_db()
    assert registration.failures == 1
    assert "nameservers_push" not in registration.registrar_data


# ---------------------------------------------------------------------------
# Steps: process


def test_process_submits_a_requested_domain_and_nothing_else():
    """A requested domain has nothing to refresh yet."""
    registration = make(Status.REQUESTED)
    registrar = FakeRegistrar()

    registrations.process(registration, registrar, NOW)

    assert registrar.names() == [
        "get_domain",
        "check",
        "create_contacts",
        "create_domain",
    ]


def test_process_refreshes_then_aligns_the_nameservers():
    """A registered domain is read, then pointed at the right nameservers."""
    registration = make(Status.ACTIVE)
    registrar = FakeRegistrar()
    registrar.state = state("active", "ACTIVE, DELEGATED", nameservers=["ns1.autre.fr"])

    registrations.process(registration, registrar, NOW)

    assert registrar.names() == ["get_domain", "set_nameservers"]


def test_process_does_not_touch_the_nameservers_when_the_refresh_failed():
    """Nothing is pushed on the strength of a read that failed."""
    registration = make(Status.ACTIVE, nameservers=["ns1.autre.fr"])
    registrar = FakeRegistrar()
    registrar.state = Boom("down")

    registrations.process(registration, registrar, NOW)

    assert registrar.names() == ["get_domain"]


# ---------------------------------------------------------------------------
# Driver


def run(**kwargs):
    """Run the due registrations against one fake registrar, returned with the summary."""
    registrar = FakeRegistrar()
    registrar.state = state("active", "ACTIVE, DELEGATED", nameservers=NAMESERVERS)
    for name, value in kwargs.pop("script", {}).items():
        setattr(registrar, name, value)
    with mock.patch.object(registrations, "get_client", return_value=registrar):
        summary = registrations.process_due(now=NOW, **kwargs)
    return registrar, summary


def test_process_due_handles_the_due_registrations_of_an_operator(account):
    """One client serves all the registrations of an operator's account, then closes."""
    operator, service = account
    due = [make(operator=operator, service=service) for _ in range(2)]
    not_due = make(
        operator=operator, service=service, next_check_at=NOW + timedelta(days=1)
    )

    registrar, summary = run()

    assert summary == {"processed": 2, "failed": 0}
    assert registrar.closed
    assert registrar.names().count("get_domain") == 2
    for registration in due:
        registration.refresh_from_db()
        assert registration.next_check_at > NOW + timedelta(hours=1)
    not_due.refresh_from_db()
    assert not_due.next_check_at == NOW + timedelta(days=1)


def test_process_due_ignores_final_registrations(account):
    """Nothing is scheduled for them, so nothing is run."""
    operator, service = account
    make(Status.FAILED, operator=operator, service=service)

    registrar, summary = run()

    assert summary == {"processed": 0, "failed": 0}
    assert not registrar.calls


def test_process_due_claims_registrations_so_that_runs_do_not_overlap(account):
    """A registration being worked on is out of reach of another run."""
    operator, service = account
    registration = make(operator=operator, service=service)

    assert registrations.claim(registration.pk, NOW) is not None
    registration.refresh_from_db()
    assert registration.next_check_at == NOW + registrations.LEASE
    assert registrations.claim(registration.pk, NOW) is None


def test_process_due_leaves_a_registration_whose_run_died_for_the_lease(account):
    """If a run dies, the registration comes back once the lease has passed."""
    operator, service = account
    registration = make(operator=operator, service=service)
    registrations.claim(registration.pk, NOW)

    _, early = run()
    assert early["processed"] == 0

    with mock.patch.object(registrations, "get_client", return_value=FakeRegistrar()):
        late = registrations.process_due(now=NOW + registrations.LEASE)
    assert late["processed"] == 1


def test_process_due_limits_a_run(account):
    """A run takes no more than its batch."""
    operator, service = account
    for _ in range(3):
        make(operator=operator, service=service)

    _, summary = run(limit=2)

    assert summary["processed"] == 2


def test_process_due_records_an_unusable_account_on_each_registration():
    """Without a working account the registrations back off instead of being lost."""
    operator = factories.OperatorFactory()
    service = factories.ServiceFactory(type=registrations.SERVICE_TYPE)
    registration = make(operator=operator, service=service)

    with mock.patch.object(registrations, "get_client") as get_client:
        summary = registrations.process_due(now=NOW)

    get_client.assert_not_called()
    assert summary == {"processed": 1, "failed": 1}
    registration.refresh_from_db()
    assert registration.failures == 1
    assert "no configuration" in registration.last_error
    assert registration.next_check_at == NOW + registrations.RETRY_BASE


def test_process_due_does_not_use_another_registrars_account(account):
    """Registrations made at one registrar are not sent to another."""
    operator, service = account
    registration = make(operator=operator, service=service, registrar="nameshield")

    registrar, summary = run()

    assert not registrar.calls
    assert summary["failed"] == 1
    registration.refresh_from_db()
    assert "changed registrar" in registration.last_error


def test_a_crash_after_the_contacts_were_created_keeps_them(account):
    """The contacts are saved before the paid call, so a crash there does not lose them."""
    operator, service = account
    registration = make(Status.REQUESTED, operator=operator, service=service)
    registrar = FakeRegistrar()
    registrar.create = ZeroDivisionError("bug")

    with mock.patch.object(registrations, "get_client", return_value=registrar):
        registrations.process_due(now=NOW)

    registration.refresh_from_db()
    assert registration.registrar_data["contacts"] == registrar.contacts
    assert registration.failures == 1


def test_process_due_goes_on_when_one_registration_crashes(account):
    """One bad registration does not stop the others."""
    operator, service = account
    first = make(operator=operator, service=service, domain="a.fr")
    second = make(operator=operator, service=service, domain="b.fr")
    registrar = FakeRegistrar()
    registrar.state = state("active", "ACTIVE, DELEGATED", nameservers=NAMESERVERS)
    real = registrar.get_domain

    def crash_on_first(domain):
        if domain == "a.fr":
            raise ZeroDivisionError("bug")
        return real(domain)

    registrar.get_domain = crash_on_first
    with mock.patch.object(registrations, "get_client", return_value=registrar):
        summary = registrations.process_due(now=NOW)

    assert summary == {"processed": 2, "failed": 1}
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.failures == 1
    assert second.failures == 0
    assert second.next_check_at > NOW
