"""Tests for the registrar configuration and helpers (core/services/registrars)."""

from django.conf import settings

import pytest

from core.services.registrars import (
    RegistrarConfigError,
    get_client,
    international_phone,
    parse_config,
)
from core.services.registrars.nameshield import NameshieldClient
from core.services.registrars.netim import NetimClient

CONTACT = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "domaines@operateur.example",
    "phone": "01 23 45 67 89",
    "address1": "1 rue de la Paix",
    "zip_code": "75002",
    "city": "Paris",
}


def netim_config(**overrides):
    """A complete Netim configuration, as stored on the operator's service config."""
    return {
        "registrar": "netim",
        "environment": "ote",
        "account_id": "AA001",
        "contact": dict(CONTACT),
        **overrides,
    }


def nameshield_config(**overrides):
    """A complete Nameshield configuration."""
    contact = {**CONTACT, "organization": "Opérateur", "identifier": "123456789"}
    return {
        "registrar": "nameshield",
        "environment": "ote",
        "contact": contact,
        **overrides,
    }


@pytest.mark.parametrize(
    "value,expected",
    [
        ("01 23 45 67 89", "+33 123456789"),
        ("01.23.45.67.89", "+33 123456789"),
        ("+33123456789", "+33 123456789"),
        ("+33 1 23 45 67 89", "+33 123456789"),
        # Overseas calling codes have three digits.
        ("+590 690 12 34 56", "+590 690123456"),
        ("+262 692 12 34 56", "+262 692123456"),
    ],
)
def test_international_phone(value, expected):
    """Phones are written the way registrars ask: country code, space, number."""
    assert international_phone(value) == expected


@pytest.mark.parametrize("value", ["", "abc", "123", "+3312"])
def test_international_phone_refuses_what_is_not_a_phone(value):
    """Anything that is not a phone number is refused rather than guessed at."""
    with pytest.raises(ValueError):
        international_phone(value)


def test_parse_config_builds_the_account():
    """A complete configuration gives the account the clients are built from."""
    account = parse_config(netim_config(), "s3cret")
    assert account.registrar == "netim"
    assert account.environment == "ote"
    assert account.account_id == "AA001"
    assert account.secret == "s3cret"
    assert account.contact.phone == "+33 123456789"
    assert account.contact.country == "FR"


def test_parse_config_names_everything_that_is_wrong():
    """The admin is told all the problems at once."""
    with pytest.raises(RegistrarConfigError) as error:
        parse_config({"registrar": "unknown", "contact": {"email": "a@b.fr"}}, None)
    message = str(error.value)
    assert "registrar must be one of" in message
    assert "environment must be one of" in message
    assert "API secret is not set" in message
    assert "contact is missing first_name" in message


def test_parse_config_reads_numbers_as_text():
    """JSON can hold a zip code or an identifier as a number."""
    config = nameshield_config()
    config["contact"]["zip_code"] = 75002
    config["contact"]["identifier"] = 123456789
    contact = parse_config(config, "token").contact
    assert contact.zip_code == "75002"
    assert contact.identifier == "123456789"


def test_the_salt_key_is_a_list():
    """Single or several, the salts that encrypt secrets are read as a rotation list."""
    assert isinstance(settings.SALT_KEY, list)
    assert settings.SALT_KEY


def test_parse_config_netim_needs_the_reseller_id():
    """Netim logs in with the reseller ID; Nameshield has no such thing."""
    config = netim_config()
    del config["account_id"]
    with pytest.raises(RegistrarConfigError, match="account_id"):
        parse_config(config, "s3cret")
    assert parse_config(nameshield_config(), "token").account_id == ""


def test_parse_config_nameshield_needs_the_operator_organization():
    """Nameshield names the operator as a body corporate, which needs its SIREN."""
    config = nameshield_config()
    del config["contact"]["identifier"]
    with pytest.raises(RegistrarConfigError, match="identifier"):
        parse_config(config, "token")


def test_parse_config_refuses_a_bad_phone():
    """A phone no registrar would take is caught when the operator is configured."""
    config = netim_config()
    config["contact"]["phone"] = "pas un numéro"
    with pytest.raises(RegistrarConfigError, match="phone"):
        parse_config(config, "s3cret")


def test_get_client_follows_the_registrar():
    """Each registrar gets its own client."""
    assert isinstance(get_client(parse_config(netim_config(), "s")), NetimClient)
    assert isinstance(
        get_client(parse_config(nameshield_config(), "t")), NameshieldClient
    )
