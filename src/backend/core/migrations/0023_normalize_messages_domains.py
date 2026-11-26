from django.db import migrations


def normalize_messages_domains(apps, schema_editor):
    """Store the domains of Messages subscriptions stripped, lowercased and sorted.

    Messages grants them as stored, and the API refuses a domain another
    subscription holds by matching the stored values. Rows written before the API
    normalized them are rewritten. Metadata that is not an object, and a list holding
    anything but strings, are left alone.
    """
    ServiceSubscription = apps.get_model("core", "ServiceSubscription")
    for subscription in ServiceSubscription.objects.filter(service__type="messages"):
        metadata = subscription.metadata
        if not isinstance(metadata, dict):
            continue
        domains = metadata.get("domains")
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            continue
        normalized = sorted({d.strip().lower() for d in domains if d.strip()})
        if normalized != domains:
            ServiceSubscription.objects.filter(pk=subscription.pk).update(
                metadata={**metadata, "domains": normalized}
            )


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_remove_service_out_of_sync"),
    ]

    operations = [
        migrations.RunPython(normalize_messages_domains, migrations.RunPython.noop),
    ]
