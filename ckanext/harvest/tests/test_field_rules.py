# -*- coding: utf-8 -*-
"""Tests for the harvest source field rules (no CKAN database needed)."""

import pytest

from ckanext.harvest.logic.field_rules import (
    parse_field_rules,
    rules_configured,
    validate_field_rules_config,
    validate_package_dict,
    format_violation,
)


class TestParseFieldRules(object):

    def test_empty_config(self):
        rules = parse_field_rules({})
        assert rules == {'required': [], 'allowed': {}}
        assert not rules_configured(rules)

    def test_non_dict_config(self):
        assert parse_field_rules(None) == {'required': [], 'allowed': {}}
        assert parse_field_rules('nonsense') == {'required': [], 'allowed': {}}

    def test_required(self):
        rules = parse_field_rules({'field_rules': {'required': ['title']}})
        assert rules['required'] == ['title']
        assert rules_configured(rules)

    def test_allowed(self):
        rules = parse_field_rules({
            'field_rules': {'allowed': {'license_id': ['cc-by', 'uk-ogl']}}
        })
        assert rules['allowed'] == {'license_id': ['cc-by', 'uk-ogl']}

    def test_top_level_aliases(self):
        rules = parse_field_rules({
            'required_fields': ['title'],
            'allowed_field_values': {'license_id': ['cc-by']},
            'allowed_licenses': ['ogl'],
        })
        assert rules['required'] == ['title']
        assert set(rules['allowed']['license_id']) == {'cc-by', 'ogl'}

    def test_dedupes_values(self):
        rules = parse_field_rules({
            'field_rules': {'required': ['title', 'title']}
        })
        assert rules['required'] == ['title']


class TestValidateFieldRulesConfig(object):

    def test_valid_config(self):
        validate_field_rules_config({
            'field_rules': {
                'required': ['title'],
                'allowed': {'license_id': ['cc-by']},
            }
        })

    def test_valid_aliases(self):
        validate_field_rules_config({
            'required_fields': ['title'],
            'allowed_field_values': {'license_id': ['cc-by']},
            'allowed_licenses': ['uk-ogl'],
        })

    def test_field_rules_not_a_dict(self):
        with pytest.raises(ValueError):
            validate_field_rules_config({'field_rules': ['title']})

    def test_required_not_a_list(self):
        with pytest.raises(ValueError):
            validate_field_rules_config(
                {'field_rules': {'required': 'title'}})

    def test_allowed_not_a_dict(self):
        with pytest.raises(ValueError):
            validate_field_rules_config(
                {'field_rules': {'allowed': ['license_id']}})

    def test_allowed_values_not_a_list(self):
        with pytest.raises(ValueError):
            validate_field_rules_config(
                {'field_rules': {'allowed': {'license_id': 'cc-by'}}})

    def test_allowed_licenses_not_a_list(self):
        with pytest.raises(ValueError):
            validate_field_rules_config(
                {'field_rules': {'allowed_licenses': 'cc-by'}})


class TestValidatePackageDict(object):

    def rules(self, **kwargs):
        return parse_field_rules({'field_rules': kwargs})

    def test_no_rules(self):
        assert validate_package_dict({'title': 'x'}, self.rules()) == []

    def test_required_missing(self):
        violations = validate_package_dict(
            {'name': 'no title'}, self.rules(required=['title']))
        assert violations == [{'field': 'title', 'reason': 'missing'}]

    def test_required_empty_string(self):
        violations = validate_package_dict(
            {'title': '   '}, self.rules(required=['title']))
        assert violations == [{'field': 'title', 'reason': 'missing'}]

    def test_required_present(self):
        assert validate_package_dict(
            {'title': 'A dataset'}, self.rules(required=['title'])) == []

    def test_allowed_present_and_allowed(self):
        violations = validate_package_dict(
            {'license_id': 'cc-by'},
            self.rules(allowed={'license_id': ['cc-by']}))
        assert violations == []

    def test_allowed_present_but_not_allowed(self):
        violations = validate_package_dict(
            {'license_id': 'unknown'},
            self.rules(allowed={'license_id': ['cc-by']}))
        assert violations == [{
            'field': 'license_id',
            'reason': 'not_allowed',
            'value': 'unknown',
            'allowed': ['cc-by'],
        }]

    def test_allowed_missing(self):
        violations = validate_package_dict(
            {}, self.rules(allowed={'license_id': ['cc-by']}))
        assert violations == [{
            'field': 'license_id',
            'reason': 'missing',
            'allowed': ['cc-by'],
        }]

    def test_required_and_allowed_missing_reported_once(self):
        violations = validate_package_dict(
            {},
            self.rules(required=['license_id'],
                       allowed={'license_id': ['cc-by']}))
        assert violations == [{'field': 'license_id', 'reason': 'missing'}]

    def test_multiple_violations_keep_field_details(self):
        violations = validate_package_dict(
            {'license_id': 'other', 'owner_org': 'org'},
            self.rules(required=['title'],
                       allowed={'license_id': ['cc-by']}))
        fields = {v['field']: v['reason'] for v in violations}
        assert fields == {'title': 'missing', 'license_id': 'not_allowed'}


class TestFormatViolation(object):

    def test_missing(self):
        message = format_violation({'field': 'title', 'reason': 'missing'})
        assert 'title' in message
        assert 'missing' in message

    def test_not_allowed(self):
        message = format_violation({
            'field': 'license_id',
            'reason': 'not_allowed',
            'value': 'other',
            'allowed': ['cc-by', 'uk-ogl'],
        })
        assert 'license_id' in message
        assert 'other' in message
        assert 'cc-by, uk-ogl' in message
