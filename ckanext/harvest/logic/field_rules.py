# -*- coding: utf-8 -*-
"""Field rules that a harvest source puts on the records it accepts.

A remote catalogue can change the shape of its metadata (for example after a
form upgrade) so that some records no longer carry a usable title or one of the
licences agreed with the source.  Rather than letting those records overwrite
good local datasets (or failing the whole job), the harvester validates every
record against the rules configured on the harvest source and quarantines the
records that do not comply.

The rules are configured as JSON in the harvest source ``config`` field::

    {
        "field_rules": {
            "required": ["title"],
            "allowed": {
                "license_id": ["cc-by", "uk-ogl"]
            }
        }
    }

``required`` fields must be present and usable (non-empty).  Fields listed in
``allowed`` must be present and take one of the listed values.  For
convenience the top-level ``required_fields``, ``allowed_field_values`` and
``allowed_licenses`` keys are accepted as aliases, so older source
configurations can adopt the feature incrementally.

The module is deliberately free of CKAN imports so that the rule logic can be
unit tested and reused by custom harvesters.
"""

from __future__ import absolute_import

import logging

log = logging.getLogger(__name__)

#: Key under which the rules live in the harvest source config.
FIELD_RULES_KEY = 'field_rules'

REASON_MISSING = 'missing'
REASON_NOT_ALLOWED = 'not_allowed'

#: Alias keys accepted at the top level of the config, for sources that do not
#: want to nest them under ``field_rules``.
TOP_LEVEL_ALIASES = {
    'required_fields': 'required',
    'allowed_field_values': 'allowed',
    'allowed_licenses': 'allowed_licenses',
}

VALID_REASON_VALUES = (REASON_MISSING, REASON_NOT_ALLOWED)


def _iter_values(value):
    """Return ``value`` as a list of cleaned string values."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return [str(value).strip()]
    return [str(item).strip() for item in value if str(item).strip()]


def _apply_rule_source(source, required, allowed, nested):
    """Merge one rule source into the ``required``/``allowed`` accumulators.

    ``nested`` is True for ``field_rules`` (where ``required``/``allowed`` are
    the canonical names) and False for the top level of the config (where only
    the explicit alias keys are read, to avoid clashing with unrelated
    harvester options).
    """
    if not isinstance(source, dict):
        return

    if nested:
        for key in ('required', 'required_fields'):
            required.extend(_iter_values(source.get(key)))
        for key in ('allowed', 'allowed_field_values'):
            allowed_values = source.get(key)
            if not isinstance(allowed_values, dict):
                continue
            for field, values in allowed_values.items():
                field = str(field).strip()
                if not field:
                    continue
                allowed.setdefault(field, []).extend(_iter_values(values))
        for key in ('allowed_licenses', 'licenses'):
            licences = _iter_values(source.get(key))
            if licences:
                allowed.setdefault('license_id', []).extend(licences)
    else:
        required.extend(_iter_values(source.get('required_fields')))
        allowed_values = source.get('allowed_field_values')
        if isinstance(allowed_values, dict):
            for field, values in allowed_values.items():
                field = str(field).strip()
                if not field:
                    continue
                allowed.setdefault(field, []).extend(_iter_values(values))
        licences = _iter_values(source.get('allowed_licenses'))
        if licences:
            allowed.setdefault('license_id', []).extend(licences)


def parse_field_rules(config):
    """Normalise the field rules found in a harvest source config.

    Returns a dict with two keys::

        {'required': ['title'], 'allowed': {'license_id': ['cc-by']}}

    Invalid or missing configuration results in empty rules, i.e. no
    validation, preserving the behaviour of sources that do not use the
    feature.
    """
    rules = {'required': [], 'allowed': {}}

    if not isinstance(config, dict):
        return rules

    required = []
    allowed = {}

    _apply_rule_source(config.get(FIELD_RULES_KEY), required, allowed,
                       nested=True)
    _apply_rule_source(config, required, allowed, nested=False)

    # Keep values in a stable order and remove duplicates.
    rules['required'] = _dedupe(required)
    rules['allowed'] = {
        field: _dedupe(values) for field, values in allowed.items()
    }
    return rules


def validate_field_rules_config(config):
    """Raise ``ValueError`` if ``config`` describes malformed field rules.

    Called from the harvester's ``validate_config`` so that a bad rule set is
    rejected in the harvest source form instead of silently disabling
    validation.
    """
    if not isinstance(config, dict):
        return

    field_rules = config.get(FIELD_RULES_KEY)
    if field_rules is not None and not isinstance(field_rules, dict):
        raise ValueError('field_rules must be a dictionary')

    for source, nested in ((field_rules, True), (config, False)):
        if not isinstance(source, dict):
            continue

        required = source.get('required' if nested else 'required_fields')
        if required is not None and not isinstance(required, list):
            raise ValueError(
                '%s must be a list of field names' %
                ('required' if nested else 'required_fields'))

        allowed_values = source.get(
            'allowed' if nested else 'allowed_field_values')
        if allowed_values is not None:
            if not isinstance(allowed_values, dict):
                raise ValueError(
                    '%s must be a dictionary of field -> allowed values' %
                    ('allowed' if nested else 'allowed_field_values'))
            for field, values in allowed_values.items():
                if isinstance(values, str) or not isinstance(
                        values, (list, tuple)):
                    raise ValueError(
                        "Values allowed for field '%s' must be a list" % field)

        for licence_key in ('allowed_licenses', 'licenses'):
            if not nested and licence_key == 'licenses':
                # ``licenses`` is too generic for the top level.
                continue
            licences = source.get(licence_key)
            if licences is not None and not isinstance(licences, list):
                raise ValueError('%s must be a list of licence ids' %
                                 licence_key)


def _dedupe(values):
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def rules_configured(rules):
    """Return True if the normalised ``rules`` actually validate anything."""
    return bool(rules.get('required') or rules.get('allowed'))


def _is_usable(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def _comparable(value):
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def validate_package_dict(package_dict, rules):
    """Validate ``package_dict`` against normalised ``rules``.

    Returns a list of dicts.  Each dict contains at least ``field`` and
    ``reason``; ``value`` and ``allowed`` are included when they help explain
    why a value was rejected.
    """
    violations = []
    if not package_dict:
        package_dict = {}

    for field in rules.get('required', []):
        if not _is_usable(package_dict.get(field)):
            violations.append({'field': field, 'reason': REASON_MISSING})

    required_fields = set(rules.get('required', []))
    for field, allowed_values in rules.get('allowed', {}).items():
        value = package_dict.get(field)
        if not _is_usable(value):
            # ``allowed`` means "one of the agreed values", so a missing
            # value is not compliant either.  Avoid reporting it twice when
            # the field is also required.
            if field not in required_fields:
                violations.append({
                    'field': field,
                    'reason': REASON_MISSING,
                    'allowed': list(allowed_values),
                })
            continue

        values = value if isinstance(value, (list, tuple, set)) else [value]
        if not any(_comparable(item) in allowed_values for item in values):
            violations.append({
                'field': field,
                'reason': REASON_NOT_ALLOWED,
                'value': value,
                'allowed': list(allowed_values),
            })

    return violations


def format_violation(violation):
    """Build a human readable message for a single violation."""
    field = violation.get('field')
    reason = violation.get('reason')
    if reason == REASON_NOT_ALLOWED:
        allowed = ', '.join(violation.get('allowed', []))
        return (
            "Field '{field}' value '{value}' is not allowed by the harvest "
            "source field rules (allowed values: {allowed})".format(
                field=field,
                value=violation.get('value'),
                allowed=allowed or '<none>',
            )
        )
    return (
        "Field '{field}' is missing or empty but is required by the harvest "
        "source field rules".format(field=field)
    )
