"""Domain registration task: asks the registrars about the domains due for it."""

import logging

from celery import shared_task

from core.services import registrations

logger = logging.getLogger(__name__)


@shared_task
def process_domain_registrations():
    """Run the steps of every domain registration whose check is due.

    Meant to run every minute; what is not yet due is left alone, so it is the
    registrations' own ``next_check_at`` that sets how often each registrar is asked
    about each domain.
    """
    summary = registrations.process_due()
    logger.info("Domain registrations processed: %s", summary)
    return summary
