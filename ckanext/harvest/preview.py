# -*- coding: utf-8 -*-
"""Read-only helpers used to preview the effect of a harvest configuration.

Nothing in this module writes to the database.  The helpers are used by
``CKANHarvester.preview_harvest_object`` and, through it, by the
``harvest_objects_preview`` action and the ``harvester preview`` CLI command.

The preview feature is meant to answer, for every existing harvest object:
"what would the import stage do with the pending configuration, and how does
the result differ from the record that is currently published?".
"""

from __future__ import absolute_import

import logging
import re

log = logging.getLogger(__name__)


# Preview statuses -----------------------------------------------------------
STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_UNCHANGED = "unchanged"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"

# Extras injected by the harvest plugin itself; they are not part of the
# remote record and must not show up as configuration changes.
HARVEST_EXTRAS = (
    "harvest_object_id",
    "harvest_source_id",
    "harvest_source_title",
)

# Fields that a harvester configuration can influence.  Keeping this list
# explicit avoids noisy diffs from derived/volatile fields such as
# ``metadata_modified`` or ``tracking_summary``.  ``license_title`` and
# ``license_url`` are derived by CKAN from ``license_id`` and are not stored
# on the package, so they are compared through ``license_id`` only.
SCALAR_FIELDS = (
    "name",
    "title",
    "notes",
    "url",
    "version",
    "license_id",
    "author",
    "author_email",
    "maintainer",
    "maintainer_email",
    "private",
    "owner_org",
)

RESOURCE_FIELDS = (
    "url",
    "name",
    "description",
    "format",
    "mimetype",
    "size",
    "hash",
    "resource_type",
)

RESOURCE_FILTER_FIELDS = (
    "format",
    "mimetype",
    "mimetype_inner",
    "url",
    "name",
    "description",
    "resource_type",
)

DATASET_FILTER_FIELDS = (
    "name",
    "title",
    "license_id",
    "organization",
    "owner_org",
    "tags",
    "type",
)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_scalar(value):
    """Return a comparable representation for a top level field."""
    if value is None or value == "" or value == [] or value == {}:
        return None
    return value


def extras_to_dict(package_dict):
    """Return ``{key: value}`` for a package dict in either API form."""
    extras = (package_dict or {}).get("extras") or []
    items = []
    if isinstance(extras, dict):
        items = list(extras.items())
    else:
        for extra in extras:
            if isinstance(extra, dict) and extra.get("key") is not None:
                items.append((extra.get("key"), extra.get("value")))

    result = {}
    for key, value in items:
        if key in HARVEST_EXTRAS:
            continue
        result[key] = value
    return result


def tag_names(package_dict):
    """Return the set of tag names of a package dict."""
    names = set()
    for tag in (package_dict or {}).get("tags") or []:
        if isinstance(tag, dict):
            name = tag.get("name")
        else:
            name = tag
        if name:
            names.add(name)
    return names


def group_names(package_dict):
    """Return the set of group names (falling back to ids)."""
    names = set()
    for group in (package_dict or {}).get("groups") or []:
        if isinstance(group, dict):
            name = group.get("name") or group.get("id")
        else:
            name = group
        if name:
            names.add(name)
    return names


def _resource_key(resource, index):
    # Harvester-created resources get a new local id, so the remote id must
    # not be part of the identity used for the comparison.
    url = resource.get("url") or ""
    name = resource.get("name") or ""
    if url or name:
        return (url, name, "")
    return ("index:%s" % index, "", "")


def resource_map(package_dict):
    """Map resources to their relevant fields, keyed by url/name/id."""
    result = {}
    for index, resource in enumerate((package_dict or {}).get("resources") or []):
        key = _resource_key(resource, index)
        # Duplicate keys are unusual, but keep the output deterministic.
        while key in result:
            key = (key[0], key[1], key[2], index)
        result[key] = {
            field: _normalise_scalar(resource.get(field))
            for field in RESOURCE_FIELDS
        }
    return result


def _resource_summary(key, values):
    return {
        "url": key[0],
        "name": key[1],
        "format": values.get("format"),
    }


def diff_packages(proposed, published):
    """Compare a transformed package dict with the published record.

    Both dictionaries are expected in ``package_show`` form (extras as a list
    of dicts, tags/groups as dicts).  Returns a dictionary with ``changes``,
    ``resources`` and ``has_changes``.
    """
    changes = []

    for field in SCALAR_FIELDS:
        # The import stage only sends the fields present in the remote
        # record; a field absent from the proposed dict is left untouched.
        if field not in (proposed or {}):
            continue
        old = _normalise_scalar((published or {}).get(field))
        new = _normalise_scalar((proposed or {}).get(field))
        if old != new:
            changes.append({"field": field, "old": old, "new": new})

    old_tags = sorted(tag_names(published))
    new_tags = sorted(tag_names(proposed))
    if old_tags != new_tags:
        changes.append({"field": "tags", "old": old_tags, "new": new_tags})

    old_groups = sorted(group_names(published))
    new_groups = sorted(group_names(proposed))
    if old_groups != new_groups:
        changes.append({"field": "groups", "old": old_groups, "new": new_groups})

    old_extras = extras_to_dict(published)
    new_extras = extras_to_dict(proposed)
    for key in sorted(set(old_extras) | set(new_extras)):
        old = _normalise_scalar(old_extras.get(key))
        new = _normalise_scalar(new_extras.get(key))
        if old != new:
            changes.append(
                {"field": "extras:%s" % key, "old": old, "new": new}
            )

    old_resources = resource_map(published)
    new_resources = resource_map(proposed)

    added = [
        _resource_summary(key, new_resources[key])
        for key in sorted(set(new_resources) - set(old_resources))
    ]
    removed = [
        _resource_summary(key, old_resources[key])
        for key in sorted(set(old_resources) - set(new_resources))
    ]
    changed = []
    for key in sorted(set(old_resources) & set(new_resources)):
        old_values = old_resources[key]
        new_values = new_resources[key]
        field_changes = []
        for field in RESOURCE_FIELDS:
            if old_values.get(field) != new_values.get(field):
                field_changes.append(
                    {
                        "field": field,
                        "old": old_values.get(field),
                        "new": new_values.get(field),
                    }
                )
        if field_changes:
            changed.append(
                {
                    "url": key[0],
                    "name": key[1],
                    "format": new_values.get("format"),
                    "changes": field_changes,
                }
            )

    resources = {"added": added, "removed": removed, "changed": changed}
    has_resource_changes = bool(added or removed or changed)

    return {
        "changes": changes,
        "resources": resources,
        "has_changes": bool(changes) or has_resource_changes,
    }


# ---------------------------------------------------------------------------
# New configuration options: license defaults & filters
# ---------------------------------------------------------------------------

def normalise_license_defaults(raw):
    """Validate the ``license_defaults`` option.

    Returns ``(defaults, errors)``.  ``defaults`` is ``None`` when the option
    is not set.
    """
    if raw is None:
        return None, []

    if not isinstance(raw, dict):
        return None, ["license_defaults must be a dictionary"]

    errors = []
    defaults = dict(raw)

    license_id = defaults.get("license_id")
    if license_id is not None and not isinstance(license_id, str):
        errors.append("license_defaults.license_id must be a string")
        license_id = None

    license_url = defaults.get("license_url")
    if license_url is not None and not isinstance(license_url, str):
        errors.append("license_defaults.license_url must be a string")

    if license_id is None and license_url is None:
        errors.append(
            "license_defaults must set at least one of license_id or license_url"
        )

    override = defaults.get("override")
    if override is not None and not isinstance(override, bool):
        errors.append("license_defaults.override must be a boolean")

    return defaults, errors


def apply_license_defaults(package_dict, defaults, notes=None):
    """Fill in missing license fields from ``license_defaults``.

    ``package_dict`` is mutated in place.  Returns the list of applied
    changes so callers can surface them in the preview.

    Only ``license_id`` is stored on a CKAN package; ``license_title`` and
    ``license_url`` are derived from it by the license registry.  A configured
    ``license_url`` is therefore reported but not written.
    """
    applied = []
    if not defaults:
        return applied

    override = bool(defaults.get("override", False))

    license_id = defaults.get("license_id")
    if license_id:
        current = package_dict.get("license_id")
        if (not current or override) and current != license_id:
            package_dict["license_id"] = license_id
            applied.append(
                {"field": "license_id", "old": current, "new": license_id})
            if notes is not None:
                notes.append(
                    "license default applied: license_id = %r (was %r)"
                    % (license_id, current)
                )

    license_url = defaults.get("license_url")
    if license_url and notes is not None:
        notes.append(
            "license_url %r is derived from license_id by CKAN and cannot be "
            "set directly; configure license_id instead" % license_url
        )

    return applied


def _compile_matcher(raw, allowed_fields):
    errors = []
    if not isinstance(raw, dict):
        return None, ["each filter rule must be an object"]

    field = raw.get("field", "url")
    if field not in allowed_fields:
        errors.append(
            "unsupported filter field %r (allowed: %s)"
            % (field, ", ".join(allowed_fields))
        )
        return None, errors

    values = raw.get("values")
    if values is not None:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            errors.append("filter 'values' must be a list")
            values = None
        else:
            values = [str(value).strip().lower() for value in values]

    pattern = raw.get("pattern")
    compiled = None
    if pattern is not None:
        try:
            compiled = re.compile(str(pattern), re.IGNORECASE)
        except re.error as exc:
            errors.append("invalid filter pattern %r: %s" % (pattern, exc))

    if values is None and compiled is None:
        errors.append("filter rule needs 'values' or 'pattern'")

    if errors:
        return None, errors

    return {
        "field": field,
        "values": values,
        "pattern": compiled,
        "raw": raw,
    }, []


def matcher_repr(matcher):
    field = matcher["field"]
    if matcher.get("values") is not None and matcher.get("pattern") is not None:
        return "%s in %s and matches %r" % (
            field,
            sorted(matcher["values"]),
            matcher["raw"].get("pattern"),
        )
    if matcher.get("values") is not None:
        return "%s in %s" % (field, sorted(matcher["values"]))
    return "%s matches %r" % (field, matcher["raw"].get("pattern"))


def _normalise_filter_option(raw, option_name, allowed_fields):
    """Validate a ``{"include": [...], "exclude": [...]}`` option."""
    if raw is None:
        return None, []

    errors = []
    rules = {"include": [], "exclude": []}

    if not isinstance(raw, dict):
        return None, ["%s must be a dictionary" % option_name]

    for bucket in ("include", "exclude"):
        entries = raw.get(bucket)
        if entries is None:
            continue
        if not isinstance(entries, list):
            errors.append("%s.%s must be a list" % (option_name, bucket))
            continue
        for entry in entries:
            matcher, matcher_errors = _compile_matcher(entry, allowed_fields)
            errors.extend(
                ["%s: %s" % (option_name, err) for err in matcher_errors]
            )
            if matcher:
                rules[bucket].append(matcher)

    if errors:
        return None, errors

    return rules, errors


def normalise_resource_filters(raw):
    """Validate and normalise the ``resource_filters`` option.

    Supported shapes::

        {"include": [{"field": "format", "values": ["CSV"]}],
         "exclude": [{"field": "url", "pattern": "\\\\.zip$"}]}

        {"include_formats": ["CSV"], "exclude_formats": ["PDF"]}

    Returns ``(rules, errors)`` where ``rules`` always has ``include`` and
    ``exclude`` lists.
    """
    rules, errors = _normalise_filter_option(
        raw, "resource_filters", RESOURCE_FILTER_FIELDS
    )
    if rules is None:
        return None, errors

    shorthand = (
        ("include_formats", "format", "values", "include"),
        ("exclude_formats", "format", "values", "exclude"),
        ("include_mimetypes", "mimetype", "values", "include"),
        ("exclude_mimetypes", "mimetype", "values", "exclude"),
        ("include_url_patterns", "url", "pattern", "include"),
        ("exclude_url_patterns", "url", "pattern", "exclude"),
    )
    for key, field, kind, bucket in shorthand:
        values = raw.get(key)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            errors.append("resource_filters.%s must be a list" % key)
            continue
        for value in values:
            matcher, matcher_errors = _compile_matcher(
                {"field": field, kind: value}, RESOURCE_FILTER_FIELDS
            )
            errors.extend(
                ["resource_filters: %s" % err for err in matcher_errors]
            )
            if matcher:
                rules[bucket].append(matcher)

    return rules, errors


def normalise_dataset_filters(raw):
    """Validate and normalise the ``dataset_filters`` option.

    Same shape as ``resource_filters`` but matched against dataset fields.
    Returns ``(rules, errors)``.
    """
    return _normalise_filter_option(
        raw, "dataset_filters", DATASET_FILTER_FIELDS
    )


def _field_values(package_dict, field):
    if field == "organization":
        organization = package_dict.get("organization")
        if isinstance(organization, dict):
            values = [organization.get("name"), organization.get("id")]
        else:
            values = [organization]
        values.extend([package_dict.get("owner_org")])
    elif field == "tags":
        values = list(tag_names(package_dict))
    else:
        value = package_dict.get(field)
        values = value if isinstance(value, (list, tuple, set)) else [value]

    return [str(value) for value in values if value is not None]


def _matches(package_dict, matcher):
    values = _field_values(package_dict, matcher["field"])
    lowered = [value.strip().lower() for value in values]

    if matcher.get("values") is not None:
        if not any(value in matcher["values"] for value in lowered):
            return False

    if matcher.get("pattern") is not None:
        if not any(matcher["pattern"].search(value) for value in values):
            return False

    return True


def matches_any(package_dict, rules):
    return any(_matches(package_dict, rule) for rule in rules or [])


def _resource_label(resource, index):
    return {
        "index": index,
        "url": resource.get("url"),
        "name": resource.get("name"),
        "format": resource.get("format"),
    }


def filter_resources(resources, rules, removed):
    """Apply resource filters, appending a reason for every removed resource.

    ``removed`` is a list that is mutated in place; this makes it easy to
    surface the reasons in the preview output.
    """
    include = (rules or {}).get("include") or []
    exclude = (rules or {}).get("exclude") or []

    kept = []
    for index, resource in enumerate(resources or []):
        if include and not matches_any(resource, include):
            removed.append(
                {
                    "index": index,
                    "resource": _resource_label(resource, index),
                    "reason": "resource did not match any include filter",
                    "filter": None,
                }
            )
            continue

        matched = next(
            (rule for rule in exclude if _matches(resource, rule)), None
        )
        if matched is not None:
            removed.append(
                {
                    "index": index,
                    "resource": _resource_label(resource, index),
                    "reason": "resource matched an exclude filter",
                    "filter": matcher_repr(matched),
                }
            )
            continue

        kept.append(resource)

    return kept


def dataset_block_reason(package_dict, rules):
    """Return a reason if ``package_dict`` is filtered out, else ``None``."""
    if not rules:
        return None

    include = rules.get("include") or []
    exclude = rules.get("exclude") or []

    if include and not matches_any(package_dict, include):
        return "dataset did not match any include filter"

    matched = next(
        (rule for rule in exclude if _matches(package_dict, rule)), None
    )
    if matched is not None:
        return "dataset matched an exclude filter (%s)" % matcher_repr(matched)

    return None


def _organization_values(package_dict):
    values = []
    organization = package_dict.get("organization")
    if isinstance(organization, dict):
        values.extend([organization.get("name"), organization.get("id")])
    elif organization:
        values.append(organization)
    if package_dict.get("owner_org"):
        values.append(package_dict.get("owner_org"))
    return [value for value in values if value]


def gather_block_reason(package_dict, config):
    """Evaluate the gather time organization/group filters against a record.

    The gather stage applies ``organizations_filter_include`` /
    ``organizations_filter_exclude`` and the equivalent ``groups_*`` options
    through remote search facets.  For an already gathered record we can apply
    the same semantics to the record content, which makes the preview answer
    "would this record still be selected?".
    """
    org_values = set(_organization_values(package_dict))
    group_values = set()
    for group in package_dict.get("groups") or []:
        if isinstance(group, dict):
            group_values.update(
                value for value in (group.get("name"), group.get("id")) if value
            )
        elif group:
            group_values.add(group)

    checks = (
        (
            "organizations_filter_include",
            "organization is not in organizations_filter_include",
            org_values,
        ),
        (
            "organizations_filter_exclude",
            "organization is in organizations_filter_exclude",
            org_values,
        ),
        (
            "groups_filter_include",
            "groups are not in groups_filter_include",
            group_values,
        ),
        (
            "groups_filter_exclude",
            "groups are in groups_filter_exclude",
            group_values,
        ),
    )

    for option, reason, values in checks:
        configured = config.get(option) or []
        if not configured:
            continue
        configured = set(configured)
        overlap = values & configured
        if option.endswith("_include"):
            if not overlap:
                return "%s (%s)" % (reason, sorted(configured))
        elif overlap:
            return "%s (%s)" % (reason, sorted(overlap))

    return None

