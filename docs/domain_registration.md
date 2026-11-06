# Domain registration service

## Overview

The **domain registration** service (`domain_registration`) lets an operator register
domain names for the organizations it manages, through its own account at a registrar,
and follows each domain from the request to a working, delegated name. It shows as a
card on the organization page: the list of domains with where each stands, a button to
register one, and a way to change a domain's nameservers.

Registrars sit behind one interface (`core/services/registrars`). Netim (REST 1.0) and
Nameshield (REST v2) are implemented. Each operator has its own account, and pays for
its registrations.

It is unrelated to the [Domains service](domains.md), which only declares names an
organization already owns, and to the [ProConnect domains](proconnect_domains.md).

## Configuration

An operator is set up on its `OperatorServiceConfig` for the service, in the admin:

```json
{
  "registrar": "netim",
  "environment": "production",
  "account_id": "AA001",
  "contact": {
    "first_name": "...", "last_name": "...", "email": "...", "phone": "01 23 45 67 89",
    "address1": "...", "zip_code": "...", "city": "...",
    "country": "FR",
    "organization": "...",
    "identifier": "..."
  }
}
```

| Key | |
|---|---|
| `registrar` | `netim` or `nameshield` |
| `environment` | `production`, or `ote`, the registrar's test platform. No default: a wrong one costs money |
| `account_id` | Netim only: the reseller ID |
| `contact` | The operator, named on every domain. Its mailbox receives what registrars and registries send |
| `contact.organization`, `contact.identifier` | Nameshield only: the operator as a body corporate, and its SIREN |

The secret (Netim API password, Nameshield token) is the `api_secret` field of the same
object. It is stored encrypted with
[django-fernet-encrypted-fields](https://github.com/jazzband/django-fernet-encrypted-fields),
shown nowhere, and leaving it empty in the admin keeps the stored one. The key derives
from `DJANGO_SECRET_KEY` and `DJANGO_SALT_KEY`: **both must be set in production, and
changing either without keeping the old one makes the stored secrets unreadable.**
`DJANGO_SALT_KEY` is a comma-separated list: the first salt encrypts, all of them
decrypt, which is how it is rotated. An old secret key goes in `SECRET_KEY_FALLBACKS`.

The configuration is validated when saved (`parse_config`).

The nameservers a new domain is delegated to are `DOMAINS_NAMESERVERS`, the same as the
Domains service. The registration period is always 10 years
(`registrations.REGISTRATION_YEARS`).

## The registration

`DomainRegistration` is one domain at a registrar, for one organization and one
operator. It covers a registration we requested (`origin` `registered`) and one that
already existed (`imported`, added in the admin: the registrar is looked up on the first
run). It holds:

* `status`, the workflow state below, and `registrar_status`, the registrar's own word,
  unmodified;
* `desired_nameservers` (what we want) and `nameservers` (what the registrar reports);
* `registrant`, the holder data that was sent; `registrar_data`, references kept for the
  registrar (contacts created, operation to follow);
* `next_check_at`, when the registrar is next asked, null once nothing more can happen;
  `failures` and `last_error` for the calls that failed.

### Statuses

| Status | Meaning | Registrar asked |
|---|---|---|
| `requested` | We hold the request, the registrar has not been told | at once |
| `unverified` | Recorded as existing, not yet confirmed at the registrar | at once |
| `pending` | The registrar or the registry is processing it. Some names are examined by the registry first, which takes days | 1 min after the request was submitted, then every 10 min, hourly after 2 days, every 6 h after 14 |
| `registered` | Registered, not published in the DNS yet (not delegated) | hourly |
| `active` | Registered and delegated | weekly; daily while younger than 60 days or within 90 days of expiry |
| `blocked` | On hold at the registrar or the registry. Someone has to act | every 6 h |
| `expired` | Past its date, possibly restorable | daily |
| `failed` | The registration was refused or abandoned. **Final** | never |
| `released` | No longer in the account (deleted, transferred out). **Final** | never |

Everything but `requested` is read from the registrar: each client maps its own words
to these (`map_domain_status`), and a word it does not know leaves the status where it
is and is logged. A final status is never left (`DomainRegistration.move_to`), and the
database refuses a check scheduled on one.

Asking again for a domain whose registration `failed` starts that record over.

## The steps and the task

`core/services/registrations.py` holds one function per thing that can be done to a
registration, each taking the registration and a registrar client, saving what it learns
and scheduling the next check. They are run and tested one by one:

* `submit` (`requested`): ask the registrar about the domain first (an earlier attempt
  may have gone through without an answer, and a registration is paid), check it is
  available, create the contacts and keep their references, then ask for the
  registration. A refusal fails the registration; any other failure is retried.
* `refresh`: read the domain and take its status, nameservers and dates. A pending
  domain the registrar does not show yet is followed through its operation.
* `sync_nameservers`: when the domain is `registered` or `active` and delegated
  elsewhere than `desired_nameservers`, push the change. The same list is not pushed
  again for 6 hours. Changing nameservers needs nothing more than setting
  `desired_nameservers`; it works on any domain, whatever its origin.

`process_due` runs them for every registration whose `next_check_at` has passed, one
client (one registrar session) per operator account. The cron task
`process_domain_registrations` calls it every minute (`cron.json`); when nothing is due
it costs one query. There is no Celery worker in this deployment: the task runs
synchronously from cron.

A request never changes anything at a registrar. Registering a domain, changing its
nameservers and asking for a refresh only record what is wanted (a `requested`
registration, `desired_nameservers`, `next_check_at` now) and the next run does it, at
most a minute later. The only live call is the availability check, which the user waits
for before sending. After `submit` the registrar is asked again one minute later
(`FIRST_LOOK`), since most registrations are over in seconds, then at the usual pace.
The user is told, once the request is sent, that nothing more is expected of them until
a domain shows `blocked`.

A registration is claimed by moving its `next_check_at` ten minutes ahead, so two runs
never work on the same one and no database lock is held during a registrar call. A run
that dies leaves the registration to be picked up again after the lease.

A failed call (registrar down, credentials refused, an account that does not exist any
more) is retried with a wait that doubles from 5 minutes to 6 hours. The registration
shows `last_error` and `failures`.

## Pre-validation

Before a registrar is asked, `precheck` turns away what cannot be registered:

* the name itself: well formed, not internationalized, directly under its extension
  (`exemple.fr`, not `www.exemple.fr`), on an RPNT 1.2 sovereign extension, not `gouv`;
* a domain we already follow;
* a domain the DNS knows (nameservers, or registered without any), resolved from the
  root with the same resolver as the Domains service. A name with no zone looks free to
  the DNS while being registered, so this spares the registrar call when it can, and the
  registrar's check has the last word.

`suggestions` proposes the candidates ProConnect generates for the organization
(`candidate_domains_for_organization`) that pass the same checks.

A registration refused by the registry costs a failed attempt against the registrar's
quota (AFNIC gives each registrar 50 tokens a day, a failed creation costs one), which is
why a name is checked, and never retried on a refusal.

## API

All under `/api/v1.0/operators/<operator_id>/organizations/<organization_id>/`, for a
user with a role in the operator. The organization needs an active subscription managed
by that operator.

| | |
|---|---|
| `GET domain-registrations/` | The organization's domains |
| `POST domain-registrations/` | `{domain, address1, zip_code, city}`: record the request to register a domain |
| `PATCH domain-registrations/<id>/` | `{nameservers}`: record the nameservers wanted |
| `POST domain-registrations/<id>/refresh/` | Have the registrar asked about the domain at the next run |
| `POST domain-registrations/check/` | `{domain}`: can it be registered? Live: asks the registrar when our checks pass |
| `GET domain-registrations/suggestions/` | Domains worth trying, and the address to propose |

The holder of a domain is the organization: its name and SIREN (the `siren`, or the
first nine digits of the `siret`). The street address is not in our data and is asked
for, then proposed again for the next domain. The person and the mailbox named on the
holder are the operator's `contact`.

## Registrars

### Netim (REST 1.0)

Docs: https://support.netim.com/en/docs/api-rest-1-0. Authentication logs in with the
reseller ID and the API password (the "DAS" password of Netim Direct), then uses a
session token; the session is made asynchronous, so a registration answers at once with
an operation to follow (`Pending`, `Done`, `Failed`, `Cancelled`). Netim allows ten
concurrent sessions per account, so a client opens its session on first use and closes it
on exit. Both the production and the test platform are served over HTTPS, which the
password in the first request needs.

The holder is created as an owner contact (`bodyForm` `ORG`, the SIREN as
`companyNumber`); the reseller account is the administrative, technical and billing
contact. 1.0 is announced as a beta and 3.0 as not yet released: the version-specific
parts (paths, flat `ns1`..`ns5`) are confined to `registrars/netim.py`.

### Nameshield (REST v2)

Docs: https://docs.nameshield.net. A bearer token (from the account manager) and no
session. Contacts are created per extension, one each for the owner, the administrative
and the technical role; the owner and the operator are body corporates, and for `.fr`
their SIREN goes in `addons.fr`. Creating a domain answers `202` and nothing to follow,
so progress is read from the domain itself. The API gives no price and no balance.

### Adding a registrar

Subclass `RegistrarClient` (`registrars/base.py`), map the registrar's statuses to ours,
register the class in `registrars.CLIENTS` and its key in
`DomainRegistration.Registrar`. The workflow does not change.

## To verify on a registrar's test platform

Written from documentation, not from a registrar's answers:

* Netim: that `PeriodCreate` for `.fr` allows 10 years; the minimum nameservers at
  creation and whether a zone check rejects `DOMAINS_NAMESERVERS` before their zone
  exists; the answer for an unknown domain on `info` (`get_domain` takes a 404 or a
  message saying "not found" as "not in the account", and refuses to guess otherwise);
  which check result a name examined by the AFNIC gets (`AVAILABLE` with `RESERVED` or
  `PENDING APPLICATION` is assumed); that a registration debits the reseller balance.
* Nameshield: whether `addons.fr` is an object or a list holding one (the spec says
  list, `registrars/nameshield.py` follows it); whether `period` takes an integer (the
  spec types it integer with string values); the status words of the domain info, which
  the spec leaves untyped.
* Both: how a `.fr` domain frozen by the AFNIC (holder verification) shows. The AFNIC
  freezes the holder's domains, blocks them after 7 days without the documents asked
  (`serverHold`: out of the DNS) and deletes them 30 days later. The registrars' API
  documentation names no such state, so a frozen domain probably looks `active` and only
  the mail to the holder says so, which is why the operator's mailbox is the holder's.
