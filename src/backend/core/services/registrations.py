"""Domain registration workflow: asking registrars, and following what they answer.

The model is :class:`core.models.DomainRegistration`; the registrar clients are in
:mod:`core.services.registrars`. This module is what sits between them, in three
parts:

* **pre-validation**, to turn away what could not be registered before a registrar
  is asked (and, for a refused creation, charged a failed attempt);
* **steps**, each doing one thing to one registration through a client: ``submit``,
  ``refresh``, ``sync_nameservers``. They save what they learn, schedule the next
  check, and can be run and tested one by one;
* **the driver**, ``process_due``, which runs the steps for the registrations whose
  ``next_check_at`` has come. Only the cron task calls it: a request never talks to
  a registrar to change anything, it records what is wanted and the task does it.

A registration is claimed by pushing its ``next_check_at`` a lease ahead, so two
runs never work on the same one and no database lock is held during a registrar call.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from django.core.exceptions import ValidationError
from django.utils import timezone

import sentry_sdk

from core.models import DomainRegistration, OperatorServiceConfig, Organization
from core.services import dns as dns_service
from core.services.domainnames import is_valid_domain
from core.services.domains import (
    DOMAIN_EXTENSIONS_ALLOWED,
    domain_extension,
    expected_nameservers,
    is_internationalized,
)
from core.services.domains_candidates import (
    candidate_domains_for_organization,
    claimed_domains,
)
from core.services.registrars import (
    Contact,
    RegistrarConfigError,
    RegistrarError,
    RegistrarRejected,
    get_client,
    parse_config,
)
from core.services.registrars.base import (
    OPERATION_FAILED,
    RegistrarClient,
)

logger = logging.getLogger(__name__)

Status = DomainRegistration.Status

SERVICE_TYPE = "domain_registration"

# The only period offered: a collectivité's domain is not something to lose track of.
REGISTRATION_YEARS = 10

# How long a claimed registration stays out of the other runs' reach. Longer than any
# run on one registration can take; if the run dies, the registration is picked up
# again after it.
LEASE = timedelta(minutes=10)

# Registrations handled by one run. The cron fires every few minutes, so a backlog
# drains; this bounds a run, not the work.
BATCH_SIZE = 100

# How long to wait after a failed call: doubling, up to the cap.
RETRY_BASE = timedelta(minutes=5)
RETRY_CAP = timedelta(hours=6)

# How long after a registration was submitted the registrar is first asked about it.
FIRST_LOOK = timedelta(minutes=1)

# Nameservers are not pushed again for the same list before this has passed.
NAMESERVERS_RETRY = timedelta(hours=6)
# ... but a pushed change is looked at again soon, to see it took.
NAMESERVERS_RECHECK = timedelta(minutes=15)

MIN_NAMESERVERS = 2
MAX_NAMESERVERS = 5

# Domains suggested for an organization at most.
MAX_SUGGESTIONS = 12

# Why a domain is turned away, and what the user is told.
REASON_INVALID = "invalid"
REASON_INTERNATIONALIZED = "internationalized"
REASON_EXTENSION = "extension"
REASON_LEVEL = "level"
REASON_RESERVED = "reserved"
REASON_KNOWN = "known"
REASON_TAKEN = "taken"
REASON_UNAVAILABLE = "unavailable"

REASON_MESSAGES = {
    REASON_INVALID: "Ce n'est pas un nom de domaine valide.",
    REASON_INTERNATIONALIZED: (
        "Les noms de domaine internationalisés (avec accents) ne sont pas acceptés."
    ),
    REASON_EXTENSION: (
        "Cette extension n'est pas une extension souveraine admise par le RPNT."
    ),
    REASON_LEVEL: (
        "Seul un nom directement sous l'extension peut être enregistré "
        "(exemple.fr, pas www.exemple.fr)."
    ),
    REASON_RESERVED: "Ce nom est réservé.",
    REASON_KNOWN: "Ce nom de domaine est déjà suivi dans Deploy Center.",
    REASON_TAKEN: "Ce nom de domaine est déjà enregistré.",
    REASON_UNAVAILABLE: "Ce nom de domaine n'est pas disponible à l'enregistrement.",
}

# The part of a holder that is the organization's own address.
ADDRESS_KEYS = ("address1", "zip_code", "city")

# First labels nobody registers: gouv.fr is the State's.
RESERVED_LABELS = frozenset({"gouv"})


# ---------------------------------------------------------------------------
# Pre-validation


def validation_reasons(domain: str) -> list[str]:
    """What is wrong with a domain by its name alone, without asking anyone."""
    domain = (domain or "").strip().lower()
    if not is_valid_domain(domain):
        return [REASON_INVALID]
    if is_internationalized(domain):
        return [REASON_INTERNATIONALIZED]
    labels = domain.split(".")
    if len(labels) != 2:
        return [REASON_LEVEL]
    if domain_extension(domain) not in DOMAIN_EXTENSIONS_ALLOWED:
        return [REASON_EXTENSION]
    if labels[0] in RESERVED_LABELS:
        return [REASON_RESERVED]
    return []


def precheck(domains: list[str]) -> dict[str, list[str]]:
    """Reasons to turn each domain away, from what we know without a registrar.

    The name's own rules, then the registrations we already hold, then the DNS: a
    name that has nameservers, or is registered but delegated to none, is taken. The
    DNS has false negatives (a registered name with no zone looks free) which the
    registrar's check catches; it is only here to spare that call when it can be.
    """
    reasons = {domain: validation_reasons(domain) for domain in domains}
    open_domains = [domain for domain, found in reasons.items() if not found]

    known = set(
        DomainRegistration.objects.filter(domain__in=open_domains).values_list(
            "domain", flat=True
        )
    )
    for domain in known:
        reasons[domain] = [REASON_KNOWN]

    to_resolve = [domain for domain in open_domains if domain not in known]
    for domain, (_, error) in dns_service.nameservers_batch(to_resolve).items():
        if error in (None, dns_service.ERROR_NOT_DELEGATED):
            reasons[domain] = [REASON_TAKEN]
    return reasons


def suggestions(organization: Organization) -> list[str]:
    """Domains an organization could register: its candidates that look free.

    The candidates are the ones ProConnect proposes, so they are all on a sovereign
    extension. A candidate the DNS knows, or that we already hold, is dropped.
    """
    candidates = candidate_domains_for_organization(organization, claimed_domains())
    free = [domain for domain, reasons in precheck(candidates).items() if not reasons]
    return sorted(free)[:MAX_SUGGESTIONS]


def address_defaults(organization: Organization) -> dict:
    """The address to propose for an organization: the one it registered with last.

    Failing that, only the postal code we know.
    """
    last = (
        DomainRegistration.objects.filter(organization=organization)
        .exclude(registrant={})
        .order_by("-created_at")
        .first()
    )
    if last is not None:
        return {key: last.registrant.get(key, "") for key in ADDRESS_KEYS}
    return {"address1": "", "zip_code": organization.code_postal or "", "city": ""}


def message_for(reasons: list[str]) -> str:
    """The text telling the user why a domain was turned away."""
    return " ".join(REASON_MESSAGES[reason] for reason in reasons)


# ---------------------------------------------------------------------------
# Requests


def siren_of(organization: Organization) -> str:
    """The organization's SIREN, or ``""`` when we have none."""
    siren = (organization.siren or (organization.siret or "")[:9]).strip()
    return siren if len(siren) == 9 and siren.isdigit() else ""


def build_registrant(
    organization: Organization, operator_contact: Contact, address: dict
) -> dict:
    """The holder of a domain: the organization, reached through the operator.

    The organization gives the name and the SIREN. The operator gives the person and
    the mailbox, so what registrars and registries send lands where it is handled.
    The street address is the organization's own and is asked for, since we do not
    hold it.
    """
    siren = siren_of(organization)
    if not siren:
        raise ValidationError(
            {"organization": "L'organisation n'a pas de numéro SIREN connu."}
        )
    missing = [key for key in ADDRESS_KEYS if not address.get(key)]
    if missing:
        raise ValidationError(dict.fromkeys(missing, "Ce champ est obligatoire."))
    return {
        "organization": organization.name,
        "identifier": siren,
        "address1": address["address1"].strip(),
        "zip_code": address["zip_code"].strip(),
        "city": address["city"].strip(),
        "country": "FR",
        "email": operator_contact.email,
        "phone": operator_contact.phone,
        "first_name": operator_contact.first_name,
        "last_name": operator_contact.last_name,
    }


def operator_account(operator_id, service_id):
    """An operator's validated registrar account for the service."""
    try:
        config = OperatorServiceConfig.objects.get(
            operator_id=operator_id, service_id=service_id
        )
    except OperatorServiceConfig.DoesNotExist as error:
        raise RegistrarConfigError(
            "the operator has no configuration for the service"
        ) from error
    return parse_config(config.get_effective_config(), config.api_secret)


def request_registration(
    *, organization, operator, service, domain, address, user=None, now=None
) -> DomainRegistration:
    """Record the request to register a domain; the registrar is asked next.

    A request for a domain whose earlier attempt failed starts that registration over.
    Raises ``ValidationError`` when the domain cannot be asked for.
    """
    now = now or timezone.now()
    domain = (domain or "").strip().lower()
    account = operator_account(operator.pk, service.pk)

    previous = DomainRegistration.objects.filter(domain=domain).first()
    retry = (
        previous is not None
        and previous.status == Status.FAILED
        and previous.organization_id == organization.pk
    )
    reasons = validation_reasons(domain)
    if not reasons and previous is None:
        reasons = precheck([domain])[domain]
    elif not reasons and not retry:
        reasons = [REASON_KNOWN]
    if reasons:
        raise ValidationError({"domain": message_for(reasons)})

    fields = {
        "organization": organization,
        "operator": operator,
        "service": service,
        "registrar": account.registrar,
        "origin": DomainRegistration.Origin.REGISTERED,
        "status": Status.REQUESTED,
        "status_changed_at": now,
        "years": REGISTRATION_YEARS,
        "registrant": build_registrant(organization, account.contact, address),
        "desired_nameservers": expected_nameservers(),
        "nameservers": [],
        "registrar_status": "",
        "registrar_message": "",
        "registrar_data": {},
        "registered_at": None,
        "expires_at": None,
        "last_checked_at": None,
        "next_check_at": now,
        "failures": 0,
        "last_error": "",
        "requested_by": user,
    }
    if retry:
        for name, value in fields.items():
            setattr(previous, name, value)
        previous.save()
        return previous
    return DomainRegistration.objects.create(domain=domain, **fields)


def prepare_imported(registration: DomainRegistration, now=None) -> None:
    """Make a registration that already existed ready for its first look.

    Takes the registrar from the operator's account and schedules a check; the
    registrar's answer then fills in the rest.
    """
    account = operator_account(registration.operator_id, registration.service_id)
    registration.registrar = account.registrar
    registration.origin = DomainRegistration.Origin.IMPORTED
    registration.status = Status.UNVERIFIED
    registration.next_check_at = now or timezone.now()


def clean_nameservers(nameservers) -> list[str]:
    """A nameserver list as the user gave it, normalized; raises if it cannot be one."""
    if not isinstance(nameservers, list):
        raise ValidationError({"nameservers": "Une liste est attendue."})
    cleaned = []
    for entry in nameservers:
        name = str(entry).strip().rstrip(".").lower()
        if not is_valid_domain(name):
            raise ValidationError(
                {"nameservers": f"{name} n'est pas un nom d'hôte valide."}
            )
        if name not in cleaned:
            cleaned.append(name)
    if not MIN_NAMESERVERS <= len(cleaned) <= MAX_NAMESERVERS:
        raise ValidationError(
            {
                "nameservers": (
                    f"Il faut entre {MIN_NAMESERVERS} et {MAX_NAMESERVERS} serveurs de noms."
                )
            }
        )
    return cleaned


def request_nameservers(
    registration: DomainRegistration, nameservers, now=None
) -> DomainRegistration:
    """Ask for a domain to be delegated elsewhere; the step pushes it when due."""
    if registration.is_final:
        raise ValidationError({"nameservers": "Ce nom de domaine n'est plus géré."})
    registration.desired_nameservers = clean_nameservers(nameservers)
    registration.next_check_at = now or timezone.now()
    registration.save()
    return registration


def check_now(registration: DomainRegistration, now=None) -> DomainRegistration:
    """Have the registrar asked about a domain at the next run."""
    if not registration.is_final:
        registration.next_check_at = now or timezone.now()
        registration.save()
    return registration


# ---------------------------------------------------------------------------
# Scheduling


def check_delay(registration: DomainRegistration, now) -> Optional[timedelta]:
    """How long until the registrar is asked again; ``None`` when never.

    Short while something is going on, long once a domain is in order. The gaps
    follow what is expected next, not a clock: a pending name is polled often
    (Netim asks for no more than one poll every five minutes), then less as it drags
    on; a healthy one weekly, daily while it is new or about to expire.
    """
    status = registration.status
    age = now - registration.status_changed_at
    if registration.is_final:
        return None
    if status in (Status.REQUESTED, Status.UNVERIFIED):
        return timedelta(0)
    if status == Status.PENDING:
        if age < timedelta(days=2):
            return timedelta(minutes=10)
        return timedelta(hours=1) if age < timedelta(days=14) else timedelta(hours=6)
    if status == Status.REGISTERED:
        return timedelta(hours=1)
    if status == Status.BLOCKED:
        return timedelta(hours=6)
    if status == Status.EXPIRED:
        return timedelta(days=1)
    # Active.
    young = registration.registered_at and (
        now.date() - registration.registered_at < timedelta(days=60)
    )
    expiring = registration.expires_at and (
        registration.expires_at - now.date() < timedelta(days=90)
    )
    return timedelta(days=1) if young or expiring else timedelta(days=7)


def _schedule(registration, now, soon: Optional[timedelta] = None) -> None:
    delay = check_delay(registration, now)
    if delay is not None and soon is not None:
        delay = min(delay, soon)
    registration.next_check_at = None if delay is None else now + delay


def _succeed(registration, now) -> None:
    registration.failures = 0
    registration.last_error = ""
    registration.last_checked_at = now


def _fail(registration, error, now) -> None:
    """Record a call that failed, to be retried later."""
    registration.failures += 1
    registration.last_error = str(error)[:1000]
    registration.last_checked_at = now
    # The exponent is bounded: a long outage must not overflow the delay.
    delay = min(RETRY_BASE * 2 ** min(registration.failures - 1, 10), RETRY_CAP)
    registration.next_check_at = now + delay
    registration.save()


# ---------------------------------------------------------------------------
# Steps


def _apply(registration, state, now) -> None:
    """Take what the registrar reports about the domain."""
    registration.registrar_status = state.registrar_status
    registration.registrar_message = state.message
    registration.nameservers = state.nameservers
    if state.expires_at:
        registration.expires_at = state.expires_at
    if state.registered_at:
        registration.registered_at = state.registered_at
    if state.status:
        registration.move_to(state.status, now)
    else:
        logger.warning(
            "Unknown %s status %r for %s",
            registration.registrar,
            state.registrar_status,
            registration.domain,
        )


def submit(registration: DomainRegistration, client: RegistrarClient, now=None) -> None:
    """Ask the registrar to register a requested domain.

    The registrar is asked about the domain first: an earlier attempt may have gone
    through without our hearing back, and registering twice is paid twice. What the
    registrar needs named is created once and kept, so a retry does not repeat it. A
    refusal fails the registration; anything else is retried later.
    """
    now = now or timezone.now()
    # What the registrar says about the domain before we ask for it is never a
    # reason to fail: a refusal here is one we do not understand, so it is retried.
    try:
        state = client.get_domain(registration.domain)
        availability = None if state else client.check(registration.domain)
    except RegistrarError as error:
        _fail(registration, error, now)
        return

    if state is not None:
        _apply(registration, state, now)
        _succeed(registration, now)
        _schedule(registration, now)
        registration.save()
        return

    if not availability.available:
        registration.registrar_message = REASON_MESSAGES[REASON_UNAVAILABLE]
        if availability.reason:
            registration.registrar_message += f" ({availability.reason})"
        registration.move_to(Status.FAILED, now)
        _succeed(registration, now)
        _schedule(registration, now)
        registration.save()
        return

    try:
        data = registration.registrar_data
        if not data.get("contacts"):
            holder = Contact(**registration.registrant)
            data = {
                **data,
                "contacts": client.create_contacts(
                    registration.domain, holder, client.config.contact
                ),
            }
            registration.registrar_data = data
            # Before the call that is paid for, so a failure there keeps them.
            registration.save()

        operation = client.create_domain(
            registration.domain,
            registration.years,
            data["contacts"],
            registration.desired_nameservers,
        )
    except RegistrarRejected as error:
        registration.registrar_message = error.message
        registration.move_to(Status.FAILED, now)
        _succeed(registration, now)
        _schedule(registration, now)
        registration.save()
        return
    except RegistrarError as error:
        _fail(registration, error, now)
        return

    registration.registrar_data = {
        **registration.registrar_data,
        "operation": operation.reference,
    }
    if operation.state == OPERATION_FAILED:
        registration.registrar_message = operation.message
        registration.move_to(Status.FAILED, now)
    else:
        registration.registrar_message = ""
        registration.move_to(Status.PENDING, now)
    _succeed(registration, now)
    # Most registrations are over in seconds: the first look comes soon, the usual
    # polling takes over after it.
    _schedule(registration, now, soon=FIRST_LOOK)
    registration.save()


def refresh(
    registration: DomainRegistration, client: RegistrarClient, now=None
) -> None:
    """Ask the registrar where a domain stands, and take its answer."""
    now = now or timezone.now()
    try:
        state = client.get_domain(registration.domain)
        reference = registration.registrar_data.get("operation")
        operation = None
        if state is None and registration.status == Status.PENDING and reference:
            operation = client.get_operation(reference)
    except RegistrarError as error:
        _fail(registration, error, now)
        return

    if state is not None:
        _apply(registration, state, now)
    elif operation is not None and operation.state == OPERATION_FAILED:
        registration.registrar_message = operation.message
        registration.move_to(Status.FAILED, now)
    elif registration.status == Status.PENDING:
        # Not visible at the registrar yet: still on its way.
        registration.registrar_message = ""
    elif registration.status == Status.UNVERIFIED:
        _fail(registration, "the domain is not in the registrar account", now)
        return
    else:
        registration.registrar_message = "Le nom de domaine n'est plus dans le compte."
        registration.move_to(Status.RELEASED, now)

    _succeed(registration, now)
    _schedule(registration, now)
    registration.save()


def needs_nameservers(registration: DomainRegistration, now) -> bool:
    """Whether the domain should be pointed at its desired nameservers now."""
    if registration.status not in (Status.REGISTERED, Status.ACTIVE):
        return False
    desired = registration.desired_nameservers
    if not desired or set(desired) == set(registration.nameservers):
        return False
    pushed = registration.registrar_data.get("nameservers_push") or {}
    recent = pushed.get("at") and (
        now - datetime.fromisoformat(pushed["at"]) < NAMESERVERS_RETRY
    )
    return not (recent and pushed.get("nameservers") == desired)


def sync_nameservers(
    registration: DomainRegistration, client: RegistrarClient, now=None
) -> None:
    """Point the domain at its desired nameservers, if it is not."""
    now = now or timezone.now()
    if not needs_nameservers(registration, now):
        return
    try:
        operation = client.set_nameservers(
            registration.domain, registration.desired_nameservers
        )
    except RegistrarRejected as error:
        # Kept to show, and not repeated before the retry delay.
        registration.registrar_message = error.message
        operation = None
    except RegistrarError as error:
        _fail(registration, error, now)
        return

    registration.registrar_data = {
        **registration.registrar_data,
        "nameservers_push": {
            "nameservers": registration.desired_nameservers,
            "at": now.isoformat(),
        },
    }
    if operation is not None and operation.state == OPERATION_FAILED:
        registration.registrar_message = operation.message
    _succeed(registration, now)
    _schedule(registration, now, soon=NAMESERVERS_RECHECK)
    registration.save()


def process(
    registration: DomainRegistration, client: RegistrarClient, now=None
) -> None:
    """Do what a registration needs now, according to its status."""
    now = now or timezone.now()
    if registration.status == Status.REQUESTED:
        submit(registration, client, now)
        return
    refresh(registration, client, now)
    if registration.failures == 0:
        sync_nameservers(registration, client, now)


# ---------------------------------------------------------------------------
# Driver


def claim(registration_id, now) -> Optional[DomainRegistration]:
    """Take a due registration for this run, or ``None`` if another run has it."""
    taken = DomainRegistration.objects.filter(
        pk=registration_id, next_check_at__lte=now
    ).update(next_check_at=now + LEASE)
    if not taken:
        return None
    return DomainRegistration.objects.get(pk=registration_id)


def process_due(now=None, limit=BATCH_SIZE) -> dict:
    """Run the steps of the registrations whose check is due.

    Returns the number processed and the number that failed.
    """
    now = now or timezone.now()
    due = DomainRegistration.objects.filter(next_check_at__lte=now)
    groups = defaultdict(list)
    for pk, operator_id, service_id in due.order_by("next_check_at").values_list(
        "pk", "operator_id", "service_id"
    )[:limit]:
        groups[(operator_id, service_id)].append(pk)

    summary = {"processed": 0, "failed": 0}
    for (operator_id, service_id), ids in groups.items():
        try:
            account = operator_account(operator_id, service_id)
        except RegistrarConfigError as error:
            _fail_group(ids, error, now, summary)
            continue
        with get_client(account) as client:
            for pk in ids:
                registration = claim(pk, now)
                if registration is None:
                    continue
                summary["processed"] += 1
                if registration.registrar != account.registrar:
                    _fail(registration, "the operator changed registrar", now)
                    summary["failed"] += 1
                    continue
                try:
                    process(registration, client, now)
                except Exception:  # pylint: disable=broad-except
                    logger.exception(
                        "Domain registration %s crashed", registration.domain
                    )
                    sentry_sdk.capture_exception()
                    summary["failed"] += 1
                    registration.refresh_from_db()
                    _fail(registration, "unexpected error", now)
                else:
                    if registration.failures:
                        summary["failed"] += 1
    return summary


def _fail_group(ids, error, now, summary) -> None:
    """Record a failure, the same for every registration of a group."""
    logger.warning("Registrar account unusable: %s", error)
    for pk in ids:
        registration = claim(pk, now)
        if registration is not None:
            summary["processed"] += 1
            summary["failed"] += 1
            _fail(registration, error, now)
