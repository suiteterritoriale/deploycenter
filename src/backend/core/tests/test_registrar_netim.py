"""Tests for the Netim client (core/services/registrars/netim.py)."""

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
from core.services.registrars.netim import map_domain_status

from .test_registrars import netim_config

BASE = "http://oterest.netim.com/1.0"

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
    return get_client(parse_config(netim_config(), "s3cret"))


def open_session():
    """Register the two calls that open a session."""
    responses.add(responses.POST, f"{BASE}/session/", json={"access_token": "tok"})
    responses.add(responses.PATCH, f"{BASE}/session/", json={})


def body_of(call):
    """The JSON a call sent."""
    return json.loads(call.request.body)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ACTIVE, DELEGATED", "active"),
        ("ACTIVE, NOT DELEGATED", "registered"),
        ("PENDING", "pending"),
        ("ERROR", "failed"),
        ("RESERVED, NOT DELEGATED", "blocked"),
        ("HOLD, NOT DELEGATED", "blocked"),
        ("EXPIRED", "expired"),
        ("EXPIRED, HOLD, NOT DELEGATED", "expired"),
        ("QUARANTINE, NOT DELEGATED", "expired"),
        ("TRANSFERRED OUT", "released"),
        ("DELETED", "released"),
        ("SOMETHING NEW", None),
        ("", None),
    ],
)
def test_map_domain_status(raw, expected):
    """Netim's statuses become ours; one we do not know becomes none."""
    assert map_domain_status(raw) == expected


@responses.activate
def test_session_is_opened_once_asynchronous_and_closed():
    """The session logs in with Basic, goes asynchronous, and is closed on exit."""
    open_session()
    responses.add(
        responses.GET, f"{BASE}/domain/exemple.fr/info/", json={"status": "PENDING"}
    )
    responses.add(
        responses.GET, f"{BASE}/domain/autre.fr/info/", json={"status": "PENDING"}
    )
    responses.add(responses.DELETE, f"{BASE}/session/", json={})

    with make_client() as client:
        client.get_domain("exemple.fr")
        client.get_domain("autre.fr")

    methods = [call.request.method for call in responses.calls]
    assert methods == ["POST", "PATCH", "GET", "GET", "DELETE"]
    login = responses.calls[0].request
    assert login.headers["Authorization"].startswith("Basic ")
    assert body_of(responses.calls[1]) == {"type": "sync", "value": "0"}
    assert responses.calls[2].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_client_without_calls_opens_no_session():
    """Nothing is opened, so nothing is closed, when no call was made."""
    with make_client():
        pass
    assert len(responses.calls) == 0


@responses.activate
def test_refused_credentials_are_a_configuration_error():
    """A login Netim refuses is not retried: the account has to be fixed."""
    responses.add(responses.POST, f"{BASE}/session/", status=401, json={})
    with pytest.raises(RegistrarConfigError):
        make_client().get_domain("exemple.fr")


@responses.activate
def test_dropped_session_is_reopened_once():
    """A 401 inside the session opens a new one and repeats the call."""
    open_session()
    responses.add(responses.GET, f"{BASE}/domain/exemple.fr/info/", status=401, json={})
    responses.add(
        responses.GET, f"{BASE}/domain/exemple.fr/info/", json={"status": "PENDING"}
    )

    state = make_client().get_domain("exemple.fr")

    assert state.status == "pending"
    assert [c.request.method for c in responses.calls].count("POST") == 2


@responses.activate
def test_server_errors_and_network_failures_are_unavailable():
    """Netim failing, or not being reachable, is something to retry later."""
    open_session()
    responses.add(responses.GET, f"{BASE}/domain/a.fr/info/", status=503)
    responses.add(
        responses.GET,
        f"{BASE}/domain/b.fr/info/",
        body=requests.ConnectionError("down"),
    )
    client = make_client()
    with pytest.raises(RegistrarUnavailable):
        client.get_domain("a.fr")
    with pytest.raises(RegistrarUnavailable):
        client.get_domain("b.fr")


@responses.activate
def test_get_domain_reads_the_domain():
    """Status, nameservers and dates come from the domain info."""
    open_session()
    responses.add(
        responses.GET,
        f"{BASE}/domain/exemple.fr/info/",
        json={
            "domain": "exemple.fr",
            "status": "ACTIVE, DELEGATED",
            "dateCreate": "2026-11-05",
            "dateExpiration": "2036-11-05",
            "ns": ["NS2.lst-domaines.fr.", "ns1.lst-domaines.fr"],
        },
    )
    state = make_client().get_domain("exemple.fr")
    assert state.status == "active"
    assert state.registrar_status == "ACTIVE, DELEGATED"
    assert state.nameservers == ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
    assert state.registered_at == date(2026, 11, 5)
    assert state.expires_at == date(2036, 11, 5)


@responses.activate
def test_get_domain_unknown_to_the_account_is_none():
    """A domain that is not in the account is not an error."""
    open_session()
    responses.add(
        responses.GET,
        f"{BASE}/domain/exemple.fr/info/",
        status=400,
        json={"message": "Domain not found"},
    )
    assert make_client().get_domain("exemple.fr") is None


@responses.activate
def test_get_domain_other_refusals_are_raised():
    """A refusal we do not recognise is never taken for 'not there'."""
    open_session()
    responses.add(
        responses.GET,
        f"{BASE}/domain/exemple.fr/info/",
        status=400,
        json={"message": "Something else entirely"},
    )
    with pytest.raises(RegistrarRejected, match="Something else"):
        make_client().get_domain("exemple.fr")


@responses.activate
@pytest.mark.parametrize(
    "answer,available,review",
    [
        ({"result": "AVAILABLE", "reason": ""}, True, False),
        ({"result": "AVAILABLE", "reason": "RESERVED"}, True, True),
        ({"result": "AVAILABLE", "reason": "PENDING APPLICATION"}, True, True),
        ({"result": "AVAILABLE", "reason": "PREMIUM"}, False, False),
        ({"result": "NOT AVAILABLE", "reason": ""}, False, False),
        ({"result": "CLOSED", "reason": "REGISTER"}, False, False),
    ],
)
def test_check(answer, available, review):
    """What can be registered, and what the registry examines first."""
    open_session()
    responses.add(responses.GET, f"{BASE}/domain/exemple.fr/check/", json=[answer])
    result = make_client().check("exemple.fr")
    assert result.available is available
    assert result.needs_review is review


@responses.activate
def test_check_unknown_is_unavailable():
    """Netim not knowing is not a no: the check is repeated later."""
    open_session()
    responses.add(
        responses.GET,
        f"{BASE}/domain/exemple.fr/check/",
        json=[{"result": "UNKNOWN", "reason": "TIMEOUT"}],
    )
    with pytest.raises(RegistrarUnavailable):
        make_client().check("exemple.fr")


@responses.activate
def test_create_contacts_makes_the_owner_and_uses_the_reseller_for_the_rest():
    """The holder is an owner contact; the reseller is every other contact."""
    open_session()
    responses.add(responses.POST, f"{BASE}/contact/", json="QL123")

    contacts = make_client().create_contacts("exemple.fr", HOLDER, HOLDER)

    assert contacts == {
        "owner": "QL123",
        "admin": "AA001",
        "tech": "AA001",
        "billing": "AA001",
    }
    sent = body_of(responses.calls[-1])["contact"]
    assert sent["isOwner"] == 1
    assert sent["bodyForm"] == "ORG"
    assert sent["bodyName"] == "Commune de Test"
    assert sent["companyNumber"] == "123456789"
    assert sent["zipCode"] == "29000"
    assert sent["phone"] == "+33 123456789"


@responses.activate
def test_create_contacts_refuses_what_netim_would_cut():
    """A legal name longer than Netim's field is refused, not truncated."""
    open_session()
    long_name = replace(HOLDER, organization="x" * 101)
    with pytest.raises(RegistrarRejected, match="bodyName"):
        make_client().create_contacts("exemple.fr", long_name, HOLDER)


@responses.activate
def test_create_domain_sends_the_contacts_the_duration_and_the_nameservers():
    """The registration names its contacts, its years and its nameservers."""
    open_session()
    responses.add(
        responses.POST,
        f"{BASE}/domain/exemple.fr/",
        json={"ID_OPE": 4521, "STATUS": "Pending", "MESSAGE": ""},
    )
    contacts = {"owner": "QL123", "admin": "AA001", "tech": "AA001", "billing": "AA001"}

    operation = make_client().create_domain(
        "exemple.fr", 10, contacts, ["ns1.lst-domaines.fr", "ns2.lst-domaines.fr"]
    )

    assert body_of(responses.calls[-1]) == {
        "idOwner": "QL123",
        "idAdmin": "AA001",
        "idTech": "AA001",
        "idBilling": "AA001",
        "duration": 10,
        "ns1": "ns1.lst-domaines.fr",
        "ns2": "ns2.lst-domaines.fr",
    }
    assert (operation.state, operation.reference) == ("pending", "4521")


@responses.activate
@pytest.mark.parametrize(
    "status,state",
    [
        ("Done", "done"),
        ("Pending", "pending"),
        ("Failed", "failed"),
        ("Cancelled", "failed"),
    ],
)
def test_operation_states(status, state):
    """An operation's status is one of three outcomes."""
    open_session()
    responses.add(
        responses.GET,
        f"{BASE}/operation/9",
        json={"ID_OPE": 9, "STATUS": status, "MESSAGE": "nope"},
    )
    operation = make_client().get_operation("9")
    assert operation.state == state
    assert operation.message == "nope"


@responses.activate
def test_set_nameservers():
    """Changing the nameservers is a PUT on the domain's DNS."""
    open_session()
    responses.add(
        responses.PUT,
        f"{BASE}/domain/exemple.fr/dns/",
        json={"ID_OPE": 7, "STATUS": "Done", "MESSAGE": ""},
    )
    operation = make_client().set_nameservers("exemple.fr", ["ns1.a.fr", "ns2.a.fr"])
    assert body_of(responses.calls[-1]) == {"ns1": "ns1.a.fr", "ns2": "ns2.a.fr"}
    assert operation.state == "done"


def test_too_many_nameservers_are_refused():
    """Netim takes five."""
    with pytest.raises(RegistrarRejected, match="at most 5"):
        make_client().set_nameservers("exemple.fr", [f"ns{i}.a.fr" for i in range(6)])
