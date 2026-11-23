# -*- coding: utf-8 -*-
"""Building and storing harvest change records.

Change records (``HarvestObjectChange``) give data consumers a stable,
paginated feed of what each harvest did to the local datasets. They are
only written for successfully published harvest objects, so failures
stay a separate concern handled by the error tables.

The module is deliberately harvester agnostic: it works from the
serialized content of the current and previous harvest objects (which
for the bundled CKAN harvester is a ``package_show`` style dict) and
falls back to the live package when object content cannot be parsed.
"""
import json
import logging

from ckan import model
from ckan.model import Session
from ckan.plugins import toolkit

from ckanext.harvest.model import (
    HarvestObject,
    HarvestObjectChange,
)

log = logging.getLogger(__name__)

# Fields that consumers are told about explicitly when an update changes
# them. Everything else on the package is irrelevant for the change feed.
TITLE_FIELD = "title"
LICENSE_FIELD = "license"
RESOURCES_FIELD = "resources"


def _load_package_dict(harvest_object):
    """Return the package dict stored on a harvest object.

    Returns ``None`` when the object has no JSON content that looks like
    a package dict (custom harvesters may store arbitrary content).
    """
    if not harvest_object.content:
        return None
    try:
        data = json.loads(harvest_object.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _live_package_dict(package_id):
    """Fallback source of field values when object content is unusable."""
    if not package_id:
        return None
    try:
        return toolkit.get_action("package_show")(
            {"model": model, "session": Session, "ignore_auth": True},
            {"id": package_id},
        )
    except Exception:
        log.debug(
            "Could not read package %s for change details", package_id
        )
        return None


def _license_value(package_dict):
    """License identity for a package dict.

    ``license_id`` is the stable machine identity, ``license_title`` is
    kept alongside for human consumption.
    """
    if not package_dict:
        return None
    license_id = package_dict.get("license_id")
    if not license_id:
        return None
    return {
        "id": license_id,
        "title": package_dict.get("license_title"),
    }


def _resource_identity(resource):
    """Stable identity of a resource within a package."""
    return resource.get("id") or resource.get("url")


def _resource_summary(resource):
    """Consumer-facing subset of a resource dict."""
    return {
        "id": resource.get("id"),
        "name": resource.get("name"),
        "url": resource.get("url"),
        "description": resource.get("description"),
        "format": resource.get("format"),
    }


def _resource_map(package_dict):
    if not package_dict:
        return {}
    resources = package_dict.get("resources") or []
    out = {}
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        identity = _resource_identity(resource)
        if identity is None:
            continue
        out[identity] = _resource_summary(resource)
    return out


def _previous_object(harvest_object):
    """Most recent earlier harvest object for the same local package."""
    if not harvest_object.package_id:
        return None
    return (
        Session.query(HarvestObject)
        .filter(HarvestObject.package_id == harvest_object.package_id)
        .filter(HarvestObject.id != harvest_object.id)
        .order_by(HarvestObject.import_finished.desc())
        .first()
    )


def build_added_details(harvest_object):
    """Details for a dataset that was published for the first time."""
    package_dict = _load_package_dict(harvest_object)
    if package_dict is None:
        package_dict = _live_package_dict(harvest_object.package_id) or {}

    return {
        TITLE_FIELD: package_dict.get("title"),
        LICENSE_FIELD: _license_value(package_dict),
        RESOURCES_FIELD: list(
            _resource_map(package_dict).values()
        ),
    }


def build_updated_details(harvest_object):
    """Field-level diff between the previous and current publication.

    Only title, license and resource list changes are reported. The
    ``changed_fields`` key lists which of them actually differ.
    """
    new_dict = _load_package_dict(harvest_object)
    if new_dict is None:
        new_dict = _live_package_dict(harvest_object.package_id) or {}

    previous = _previous_object(harvest_object)
    old_dict = _load_package_dict(previous) if previous else None
    if old_dict is None and previous is not None:
        # The previous object carries no usable JSON, so historical
        # values are unknown: report the new value with no "old" side.
        old_dict = {}

    details = {}
    changed_fields = []

    old_title = old_dict.get(TITLE_FIELD) if old_dict else None
    new_title = new_dict.get(TITLE_FIELD)
    if old_title != new_title:
        details[TITLE_FIELD] = {"old": old_title, "new": new_title}
        changed_fields.append(TITLE_FIELD)

    old_license = _license_value(old_dict)
    new_license = _license_value(new_dict)
    if old_license != new_license:
        details[LICENSE_FIELD] = {"old": old_license, "new": new_license}
        changed_fields.append(LICENSE_FIELD)

    old_resources = _resource_map(old_dict)
    new_resources = _resource_map(new_dict)
    added_resources = [
        resource
        for identity, resource in new_resources.items()
        if identity not in old_resources
    ]
    removed_resources = [
        resource
        for identity, resource in old_resources.items()
        if identity not in new_resources
    ]
    if added_resources or removed_resources:
        details[RESOURCES_FIELD] = {
            "added": added_resources,
            "removed": removed_resources,
            "unchanged": [
                resource
                for identity, resource in new_resources.items()
                if identity in old_resources
            ],
        }
        changed_fields.append(RESOURCES_FIELD)

    details["changed_fields"] = changed_fields
    return details


def record_harvest_object_change(harvest_object):
    """Create the change record for a successfully published object.

    Only ``added`` and ``updated`` report statuses produce records. The
    unique constraint on ``harvest_object_id`` plus the existence check
    make this idempotent: reprocessing the same object never yields two
    rows, even across concurrent workers.

    Returns the ``HarvestObjectChange`` row, or ``None`` when nothing
    was recorded (e.g. *not modified* or *errored* objects).
    """
    report_status = harvest_object.report_status
    if report_status not in (
        HarvestObjectChange.CHANGE_ADDED,
        HarvestObjectChange.CHANGE_UPDATED,
    ):
        return None

    existing = (
        Session.query(HarvestObjectChange)
        .filter(
            HarvestObjectChange.harvest_object_id == harvest_object.id
        )
        .first()
    )
    if existing:
        return existing

    if report_status == HarvestObjectChange.CHANGE_ADDED:
        details = build_added_details(harvest_object)
    else:
        details = build_updated_details(harvest_object)

    change = HarvestObjectChange(
        harvest_object_id=harvest_object.id,
        harvest_job_id=harvest_object.harvest_job_id,
        harvest_source_id=harvest_object.harvest_source_id,
        package_id=harvest_object.package_id,
        guid=harvest_object.guid or "",
        change_type=report_status,
        change_details=json.dumps(details),
    )
    Session.add(change)
    try:
        Session.flush()
    except Exception:
        # Another worker may have inserted the same record in parallel.
        Session.rollback()
        existing = (
            Session.query(HarvestObjectChange)
            .filter(
                HarvestObjectChange.harvest_object_id
                == harvest_object.id
            )
            .first()
        )
        if existing:
            return existing
        raise
    return change
