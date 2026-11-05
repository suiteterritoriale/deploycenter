"""Tests for the Nameshield client (core/services/registrars/nameshield.py)."""

import json
from dataclasses import replace
from datetime import date

import pytest
import requests
import responses

from core.services.registrars import (
    Contact,
    RegistrarConfigError,
    RegistrarRejected,
    RegistrarUnavailable,
    get_client,
    parse_config,
)
from core.services.registrars.nameshield import map_domain_status

from .test_registrars import nameshield_config

BASE = "https://ote-api.nameshield.net/registrar/v2"

HOLDER = Contact(
    organization="Commune de Test",
    identifier="123456789",
    address1="1 place de la Mairie",
    zip_code="29000",
    city="Quimper",
    email="domaines@operateur.example",
    phone="+33 123456789",
    first_name="Ada",
    last_name="Lovelace",
)


def make_client():
    """A client for a test account."""
    return get_client(parse_config(nameshield_config(), "token"))


def body_of(call):
    """The JSON a call sent."""
    return json.loads(call.request.body)


@pytest.mark.parametrize(
    "raw,nameservers,statuses,expected",
    [
        ("ORDERED", [], [], "pending"),
        ("INVOICED", [], [], "pending"),
        ("PAID", [], [], "pending"),
        ("PENDING_CREATION", [], [], "pending"),
        ("CREATED", ["ns1.a.fr"], ["ok"], "active"),
        ("CREATED", [], ["inactive"], "registered"),
        ("CREATED", ["ns1.a.fr"], ["serverHold"], "blocked"),
        ("PENDING_MODIFY", ["ns1.a.fr"], [], "active"),
        ("BLOCKED", [], [], "blocked"),
        ("PENDING_DELETION", [], [], "expired"),
        ("DELETED", [], [], "released"),
        ("KILLED", [], [], "released"),
        ("SOMETHING NEW", [], [], None),
    ],
)
def test_map_domain_status(raw, nameservers, statuses, expected):
    """Nameshield's statuses become ours; one we do not know becomes none."""
    assert map_domain_status(raw, nameservers, statuses) == expected


@responses.activate
def test_calls_carry_the_token():
    """There is no session: the token is sent every time."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr/check",
        json={"message": "Ok", "data": [{"available": True}]},
    )
    make_client().check("exemple.fr")
    assert responses.calls[0].request.headers["Authorization"] == "Bearer token"


@responses.activate
def test_refused_token_is_a_configuration_error():
    """A token Nameshield refuses has to be fixed, not retried."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr/check",
        status=401,
        json={"errors": [{"code": "UNAUTHORIZED", "message": "This token is invalid"}]},
    )
    with pytest.raises(RegistrarConfigError):
        make_client().check("exemple.fr")


@responses.activate
def test_rate_limit_server_errors_and_network_failures_are_unavailable():
    """Something to try again later."""
    responses.add(responses.GET, f"{BASE}/domains/a.fr/check", status=429)
    responses.add(responses.GET, f"{BASE}/domains/b.fr/check", status=500)
    responses.add(
        responses.GET,
        f"{BASE}/domains/c.fr/check",
        body=requests.ConnectionError("down"),
    )
    client = make_client()
    for domain in ("a.fr", "b.fr", "c.fr"):
        with pytest.raises(RegistrarUnavailable):
            client.check(domain)


@responses.activate
def test_refusal_carries_the_registrars_message_and_code():
    """What Nameshield says is wrong is what the user is told."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr/check",
        status=400,
        json={
            "errors": [{"code": "INVALID_INPUT", "message": "The field is not valid"}]
        },
    )
    with pytest.raises(RegistrarRejected) as error:
        make_client().check("exemple.fr")
    assert error.value.message == "The field is not valid"
    assert error.value.code == "INVALID_INPUT"


@responses.activate
@pytest.mark.parametrize(
    "entry,available,review",
    [
        ({"available": True}, True, False),
        ({"available": True, "reserved": True}, True, True),
        ({"available": False}, False, False),
        ({"available": True, "premium": True}, False, False),
        ({"available": True, "dpmlExists": True}, False, False),
        ({"available": True, "collision": True}, False, False),
    ],
)
def test_check(entry, available, review):
    """What can be registered through the API, and what the registry examines."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr/check",
        json={"message": "Ok", "data": [entry]},
    )
    result = make_client().check("exemple.fr")
    assert result.available is available
    assert result.needs_review is review


@responses.activate
def test_get_domain_reads_the_domain():
    """Status, nameservers and dates come from the domain."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr",
        json={
            "message": "Ok",
            "data": [
                {
                    "domain": "exemple.fr",
                    "status": "CREATED",
                    "statusList": ["ok"],
                    "nameservers": {
                        "0": "NS1.lst-domaines.fr",
                        "1": "ns2.lst-domaines.fr.",
                    },
                    "nicCreationDate": "2026-11-05 10:00:00",
                    "expirationDate": "2036-11-05",
                }
            ],
        },
    )
    state = make_client().get_domain("exemple.fr")
    assert state.status == "active"
    assert state.nameservers == ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
    assert state.registered_at == date(2026, 11, 5)
    assert state.expires_at == date(2036, 11, 5)


@responses.activate
def test_get_domain_not_found_is_none():
    """A domain that is not in the account is not an error."""
    responses.add(
        responses.GET,
        f"{BASE}/domains/exemple.fr",
        status=404,
        json={"errors": [{"code": "NOT_FOUND", "message": "Object not found"}]},
    )
    assert make_client().get_domain("exemple.fr") is None


@responses.activate
def test_create_contacts_makes_an_owner_an_admin_and_a_tech():
    """Three contacts are created for the extension: the holder, then the operator twice."""
    handles = iter(["NS-1", "NS-2", "NS-3"])
    responses.add_callback(
        responses.POST,
        f"{BASE}/contacts",
        callback=lambda request: (
            201,
            {},
            json.dumps({"data": [{"handle": next(handles)}]}),
        ),
    )
    operator = replace(HOLDER, organization="Opérateur", identifier="987654321")

    contacts = make_client().create_contacts("exemple.fr", HOLDER, operator)

    assert contacts == {"owner": "NS-1", "admin": "NS-2", "tech": "NS-3"}
    owner, admin, tech = (body_of(call) for call in responses.calls)
    assert owner["type"] == "owner"
    assert owner["extension"] == "fr"
    assert owner["juridic"] == "m"
    assert owner["organization"] == "Commune de Test"
    assert owner["postalCode"] == "29000"
    assert owner["addons"] == {"fr": [{"ident": "123456789"}]}
    assert (admin["type"], tech["type"]) == ("admin", "tech")
    assert admin["organization"] == "Opérateur"
    assert admin["addons"] == {"fr": [{"ident": "987654321"}]}


@responses.activate
def test_contacts_for_other_extensions_carry_no_french_addons():
    """The identification the AFNIC asks for is sent for .fr only."""
    responses.add(
        responses.POST,
        f"{BASE}/contacts",
        json={"data": [{"handle": "NS-1"}]},
        status=201,
    )
    make_client().create_contacts("exemple.bzh", HOLDER, HOLDER)
    assert all("addons" not in body_of(call) for call in responses.calls)
    assert body_of(responses.calls[0])["extension"] == "bzh"


@responses.activate
def test_create_domain_is_accepted_with_nothing_to_follow():
    """The answer is a bare acceptance: the domain itself is followed."""
    responses.add(
        responses.POST,
        f"{BASE}/domains",
        status=202,
        json={"message": "Domain name under registration"},
    )
    contacts = {"owner": "NS-1", "admin": "NS-2", "tech": "NS-3"}

    operation = make_client().create_domain(
        "exemple.fr", 10, contacts, ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
    )

    assert body_of(responses.calls[0]) == {
        "domain": "exemple.fr",
        "period": 10,
        "owner": "NS-1",
        "admin": "NS-2",
        "tech": "NS-3",
        "nameservers": {"0": "ns1.lst-domaines.fr", "1": "ns2.lst-domaines.fr"},
    }
    assert (operation.state, operation.reference) == ("pending", "")


@responses.activate
@pytest.mark.parametrize("code,state", [(200, "done"), (202, "pending")])
def test_set_nameservers(code, state):
    """A change applied at once is done; one accepted is pending."""
    responses.add(responses.PUT, f"{BASE}/domains/exemple.fr", status=code, json={})
    operation = make_client().set_nameservers("exemple.fr", ["ns1.a.fr", "ns2.a.fr"])
    assert body_of(responses.calls[0]) == {
        "nameservers": {"0": "ns1.a.fr", "1": "ns2.a.fr"}
    }
    assert operation.state == state
