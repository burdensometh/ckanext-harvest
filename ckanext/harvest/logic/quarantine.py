# -*- coding: utf-8 -*-
"""Persistence helpers for records quarantined by the field rules.

The public actions (:func:`harvest_quarantine_list` and
:func:`harvest_quarantine_replay`) and the harvester import stage both use the
helpers here so that quarantine bookkeeping stays in one place.
"""

from __future__ import absolute_import

import datetime
import json
import logging

from ckan.model.meta import Session

from ckanext.harvest.model import (
    HarvestQuarantine,
    QUARANTINE_STATUS_PENDING,
    QUARANTINE_STATUS_REPLAYED,
)
from ckanext.harvest.logic.field_rules import format_violation

log = logging.getLogger(__name__)


def violations_as_json(violations):
    return json.dumps(violations or [])


def violations_from_json(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def format_violations_message(violations):
    """Render a human readable, one-line-per-field message."""
    return '\n'.join(format_violation(v) for v in violations or [])


def _source_id_for_object(harvest_object):
    source_id = getattr(harvest_object, 'harvest_source_id', None)
    if not source_id:
        job = getattr(harvest_object, 'job', None)
        if job is not None and job.source is not None:
            source_id = job.source.id
    return source_id


def source_id_for_object(harvest_object):
    """Public wrapper used by the harvester import stage."""
    return _source_id_for_object(harvest_object)


def quarantine_object(harvest_object, violations):
    """Record (or refresh) a quarantine entry for ``harvest_object``.

    The original payload is kept so that the record can be reviewed and
    replayed after the source rules are corrected.  Entries are unique per
    source/guid, so re-harvesting the same non-compliant record updates the
    existing entry instead of piling up duplicates.
    """
    source_id = _source_id_for_object(harvest_object)
    guid = harvest_object.guid or ''
    now = datetime.datetime.utcnow()

    entry = None
    if source_id:
        entry = HarvestQuarantine.get_by_source_and_guid(source_id, guid)

    if entry is None:
        entry = HarvestQuarantine(guid=guid, created=now)
        entry.harvest_source_id = source_id

    entry.content = harvest_object.content
    entry.violations = violations_as_json(violations)
    entry.status = QUARANTINE_STATUS_PENDING
    entry.package_id = getattr(harvest_object, 'package_id', None)
    entry.harvest_object_id = getattr(harvest_object, 'id', None)
    entry.harvest_job_id = getattr(harvest_object, 'harvest_job_id', None)
    entry.updated = now
    entry.save()

    log.info(
        'Quarantined harvest object %s (%s) for source %s: %s',
        entry.harvest_object_id,
        guid,
        source_id,
        ', '.join(v.get('field', '?') for v in violations or []),
    )
    return entry


def mark_replayed(source_id, guid, package_id=None):
    """Mark any pending quarantine entry for this record as replayed."""
    if not source_id:
        return None
    entry = HarvestQuarantine.get_by_source_and_guid(source_id, guid)
    if entry is None or entry.status == QUARANTINE_STATUS_REPLAYED:
        return entry
    entry.status = QUARANTINE_STATUS_REPLAYED
    entry.updated = datetime.datetime.utcnow()
    if package_id:
        entry.package_id = package_id
    entry.save()
    return entry


def quarantined_entry_or_raise(entry_id):
    """Return a quarantine entry or raise ``ObjectNotFound``."""
    from ckan.plugins import toolkit

    entry = HarvestQuarantine.get(entry_id)
    if entry is None:
        raise toolkit.ObjectNotFound('Quarantine entry not found')
    return entry


def list_entries(source_id=None, status=None, limit=None):
    query = Session.query(HarvestQuarantine)
    if source_id:
        query = query.filter(HarvestQuarantine.harvest_source_id == source_id)
    if status:
        query = query.filter(HarvestQuarantine.status == status)
    query = query.order_by(HarvestQuarantine.created.desc())
    if limit:
        query = query.limit(limit)
    return query.all()


def quarantine_dictize(entry):
    return {
        'id': entry.id,
        'guid': entry.guid,
        'content': entry.content,
        'violations': violations_from_json(entry.violations),
        'status': entry.status,
        'package_id': entry.package_id,
        'harvest_object_id': entry.harvest_object_id,
        'harvest_source_id': entry.harvest_source_id,
        'harvest_job_id': entry.harvest_job_id,
        'created': entry.created.isoformat() if entry.created else None,
        'updated': entry.updated.isoformat() if entry.updated else None,
    }
