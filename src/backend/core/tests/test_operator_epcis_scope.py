"""Tests for the operator "epcis" scope in the admin form and the data.gouv export."""

import csv
import io
from unittest import mock

import pytest

from core import factories
from core.admin import OperatorAdminForm
from core.tasks.datagouv import upload_deployment_operators_dataset

pytestmark = pytest.mark.django_db


def _form_data(operator, **overrides):
    data = {
        "name": operator.name,
        "url": operator.url,
        "is_active": "on",
        "config": "{}",
        "departements": "",
        "epcis": "",
    }
    data.update(overrides)
    return data


def test_operator_admin_form_saves_epcis():
    """The epcis textarea is stored as a list in config and shown back."""
    operator = factories.OperatorFactory(config={})
    form = OperatorAdminForm(
        _form_data(operator, epcis="200000001\n 200000002 \n\n"), instance=operator
    )
    assert form.is_valid(), form.errors
    form.save()
    operator.refresh_from_db()
    assert operator.config["epcis"] == ["200000001", "200000002"]

    form = OperatorAdminForm(instance=operator)
    assert form.fields["epcis"].initial == "200000001\n200000002"


def test_operator_admin_form_clears_epcis():
    """An empty epcis textarea removes the key from config."""
    operator = factories.OperatorFactory(
        config={"epcis": ["200000001"], "other": "kept"}
    )
    form = OperatorAdminForm(
        _form_data(operator, config='{"other": "kept"}'), instance=operator
    )
    assert form.is_valid(), form.errors
    form.save()
    operator.refresh_from_db()
    assert operator.config == {"other": "kept"}


@pytest.mark.parametrize("value", ["12345678", "1234567890", "20000000a", "²00000001"])
def test_operator_admin_form_rejects_invalid_siren(value):
    """Anything but 9 ASCII digits is refused."""
    operator = factories.OperatorFactory(config={})
    form = OperatorAdminForm(_form_data(operator, epcis=value), instance=operator)
    assert not form.is_valid()
    assert "epcis" in form.errors


def test_datagouv_operators_export_includes_epcis():
    """The operators CSV has a sorted, comma separated epcis column."""
    operator = factories.OperatorFactory(
        status="partenaire",
        config={"departements": ["42"], "epcis": ["200000002", "200000001"]},
    )
    service = factories.ServiceFactory()
    factories.OperatorServiceConfigFactory(operator=operator, service=service)

    with mock.patch("core.tasks.datagouv._upload_data_to_data_gouv") as upload:
        upload_deployment_operators_dataset()

    csv_bytes = upload.call_args.args[2]
    rows = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8")), delimiter=";"))
    assert len(rows) == 1
    assert rows[0]["departements"] == "42"
    assert rows[0]["epcis"] == "200000001,200000002"
