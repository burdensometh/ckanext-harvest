# -*- coding: utf-8 -*-

import copy
import json
import logging
import re
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import contains_eager

from ckantoolkit import config

from ckan import plugins as p
from ckan import model
from ckan.model import Session, Package, PACKAGE_NAME_MAX_LENGTH

from ckan.logic.schema import default_create_package_schema
from ckan.lib.navl.validators import ignore_missing, ignore
from ckan.lib.munge import munge_title_to_name, munge_tag

from ckanext.harvest.model import (HarvestObject, HarvestObjectExtra,
                                   HarvestGatherError, HarvestObjectError,
                                   HarvestJob)

from ckan.plugins.core import SingletonPlugin, implements
from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.logic.schema import unicode_safe


log = logging.getLogger(__name__)


#: Fields that catalogers are allowed to maintain by hand. When both the local
#: record and the upstream source modified one of these fields, the local value
#: wins. This can be overridden globally (``ckan.harvest.merge_fields``) or per
#: harvest source (``merge_fields`` in the source config).
DEFAULT_MERGE_FIELDS = ('notes', 'tags')

#: Harvest object extra holding the upstream package dict that was used for the
#: last successful import. It is the "base" of the next three-way merge, so it
#: must not contain any of the local (manually merged) values.
MERGE_BASE_EXTRA = 'merge_base'

#: Harvest object extra holding the fields that were modified both locally and
#: upstream, together with the values and the chosen resolution.
MERGE_CONFLICTS_EXTRA = 'merge_conflicts'

#: Extras managed by the harvest extension itself. They are never merged, the
#: extension takes care of setting them on the dataset.
HARVEST_MANAGED_EXTRAS = (
    'harvest_object_id',
    'harvest_source_id',
    'harvest_source_title',
)

#: Fields that describe the dataset identity, harvest bookkeeping or values
#: derived by ``package_show`` (e.g. ``license_title``) rather than the
#: metadata itself. They are always taken from the upstream dict and never
#: reported as conflicts.
MERGE_SKIP_FIELDS = frozenset([
    'id',
    'name',
    'owner_org',
    'organization',
    'groups',
    'type',
    'state',
    'metadata_created',
    'metadata_modified',
    'revision_id',
    'revision_timestamp',
    'license_title',
    'license_url',
    'isopen',
    'tracking_summary',
    'num_resources',
    'num_tags',
    'relationships_as_object',
    'relationships_as_subject',
])

#: Fields that are merged by their own dedicated logic and must not go through
#: the generic per-field resolution.
NON_MERGEABLE_FIELDS = frozenset(['extras', 'tags'])

_MISSING = object()


class HarvesterBase(SingletonPlugin):
    '''
    Generic base class for harvesters, providing a number of useful functions.

    A harvester doesn't have to derive from this - it could just have:

        implements(IHarvester)
    '''
    implements(IHarvester)

    config = None

    _user_name = None

    @classmethod
    def _gen_new_name(cls, title, existing_name=None,
                      append_type=None):
        '''
        Returns a 'name' for the dataset (URL friendly), based on the title.

        If the ideal name is already used, it will append a number to it to
        ensure it is unique.

        If generating a new name because the title of the dataset has changed,
        specify the existing name, in case the name doesn't need to change
        after all.

        :param existing_name: the current name of the dataset - only specify
                              this if the dataset exists
        :type existing_name: string
        :param append_type: the type of characters to add to make it unique -
                            either 'number-sequence' or 'random-hex'.
        :type append_type: string
        '''

        # If append_type was given, use it. Otherwise, use the configured default.
        # If nothing was given and no defaults were set, use 'number-sequence'.
        if append_type:
            append_type_param = append_type
        else:
            append_type_param = config.get('ckanext.harvest.default_dataset_name_append',
                                           'number-sequence')

        ideal_name = munge_title_to_name(title)
        ideal_name = re.sub('-+', '-', ideal_name)  # collapse multiple dashes
        return cls._ensure_name_is_unique(ideal_name,
                                          existing_name=existing_name,
                                          append_type=append_type_param)

    @staticmethod
    def _ensure_name_is_unique(ideal_name, existing_name=None,
                               append_type='number-sequence'):
        '''
        Returns a dataset name based on the ideal_name, only it will be
        guaranteed to be different than all the other datasets, by adding a
        number on the end if necessary.

        If generating a new name because the title of the dataset has changed,
        specify the existing name, in case the name doesn't need to change
        after all.

        The maximum dataset name length is taken account of.

        :param ideal_name: the desired name for the dataset, if its not already
                           been taken (usually derived by munging the dataset
                           title)
        :type ideal_name: string
        :param existing_name: the current name of the dataset - only specify
                              this if the dataset exists
        :type existing_name: string
        :param append_type: the type of characters to add to make it unique -
                            either 'number-sequence' or 'random-hex'.
        :type append_type: string
        '''
        ideal_name = ideal_name[:PACKAGE_NAME_MAX_LENGTH]
        if existing_name == ideal_name:
            return ideal_name
        if append_type == 'number-sequence':
            MAX_NUMBER_APPENDED = 999
            APPEND_MAX_CHARS = len(str(MAX_NUMBER_APPENDED))
        elif append_type == 'random-hex':
            APPEND_MAX_CHARS = 5  # 16^5 = 1 million combinations
        else:
            raise NotImplementedError('append_type cannot be %s' % append_type)
        # Find out which package names have been taken. Restrict it to names
        # derived from the ideal name plus and numbers added
        like_q = u'%s%%' % \
            ideal_name[:PACKAGE_NAME_MAX_LENGTH-APPEND_MAX_CHARS]
        name_results = Session.query(Package.name)\
                              .filter(Package.name.ilike(like_q))\
                              .all()
        taken = set([name_result[0] for name_result in name_results])
        if existing_name and existing_name in taken:
            taken.remove(existing_name)
        if ideal_name not in taken:
            # great, the ideal name is available
            return ideal_name
        elif existing_name and existing_name.startswith(ideal_name):
            # the ideal name is not available, but its an existing dataset with
            # a name based on the ideal one, so there's no point changing it to
            # a different number
            return existing_name
        elif append_type == 'number-sequence':
            # find the next available number
            counter = 1
            while counter <= MAX_NUMBER_APPENDED:
                candidate_name = \
                    ideal_name[:PACKAGE_NAME_MAX_LENGTH-len(str(counter))] + \
                    str(counter)
                if candidate_name not in taken:
                    return candidate_name
                counter = counter + 1
            return None
        elif append_type == 'random-hex':
            return ideal_name[:PACKAGE_NAME_MAX_LENGTH-APPEND_MAX_CHARS] + \
                str(uuid.uuid4())[:APPEND_MAX_CHARS]

    _save_gather_error = HarvestGatherError.create
    _save_object_error = HarvestObjectError.create

    def _get_user_name(self):
        '''
        Returns the name of the user that will perform the harvesting actions
        (deleting, updating and creating datasets)

        By default this will be the old 'harvest' user to maintain
        compatibility. If not present, the internal site admin user will be
        used. This is the recommended setting, but if necessary it can be
        overridden with the `ckanext.harvest.user_name` config option:

           ckanext.harvest.user_name = harvest

        '''
        if self._user_name:
            return self._user_name

        config_user_name = config.get('ckanext.harvest.user_name')
        if config_user_name:
            self._user_name = config_user_name
            return self._user_name

        context = {'model': model,
                   'ignore_auth': True,
                   }

        # Check if 'harvest' user exists and if is a sysadmin
        try:
            user_harvest = p.toolkit.get_action('user_show')(
                context, {'id': 'harvest'})
            if user_harvest['sysadmin']:
                self._user_name = 'harvest'
                return self._user_name
        except p.toolkit.ObjectNotFound:
            pass

        context['defer_commit'] = True  # See ckan/ckan#1714
        self._site_user = p.toolkit.get_action('get_site_user')(context, {})
        self._user_name = self._site_user['name']

        return self._user_name

    def _create_harvest_objects(self, remote_ids, harvest_job):
        '''
        Given a list of remote ids and a Harvest Job, create as many Harvest Objects and
        return a list of their ids to be passed to the fetch stage.

        TODO: Not sure it is worth keeping this function
        '''
        try:
            object_ids = []
            if len(remote_ids):
                for remote_id in remote_ids:
                    # Create a new HarvestObject for this identifier
                    obj = HarvestObject(guid=remote_id, job=harvest_job)
                    obj.save()
                    object_ids.append(obj.id)
                return object_ids
            else:
                self._save_gather_error('No remote datasets could be identified', harvest_job)
        except Exception as e:
            self._save_gather_error('%r' % e.message, harvest_job)

    def _create_or_update_package(self, package_dict, harvest_object,
                                  package_dict_form='rest'):
        '''
        Creates a new package or updates an existing one according to the
        package dictionary provided.

        The package dictionary can be in one of two forms:

        1. 'rest' - as seen on the RESTful API:

                http://datahub.io/api/rest/dataset/1996_population_census_data_canada

           This is the legacy form. It is the default to provide backward
           compatibility.

           * 'extras' is a dict e.g. {'theme': 'health', 'sub-theme': 'cancer'}
           * 'tags' is a list of strings e.g. ['large-river', 'flood']

        2. 'package_show' form, as provided by the Action API (CKAN v2.0+):

               http://datahub.io/api/action/package_show?id=1996_population_census_data_canada

           * 'extras' is a list of dicts
                e.g. [{'key': 'theme', 'value': 'health'},
                        {'key': 'sub-theme', 'value': 'cancer'}]
           * 'tags' is a list of dicts
                e.g. [{'name': 'large-river'}, {'name': 'flood'}]

        Note that the package_dict must contain an id, which will be used to
        check if the package needs to be created or updated (use the remote
        dataset id).

        If the remote server provides the modification date of the remote
        package, add it to package_dict['metadata_modified'].

        :returns: The same as what import_stage should return. i.e. True if the
                  create or update occurred ok, 'unchanged' if it didn't need
                  updating or False if there were errors.


        TODO: Not sure it is worth keeping this function. If useful it should
        use the output of package_show logic function (maybe keeping support
        for rest api based dicts
        '''
        assert package_dict_form in ('rest', 'package_show')
        try:
            # Change default schema
            schema = default_create_package_schema()
            schema['id'] = [ignore_missing, unicode_safe]
            schema['__junk'] = [ignore]

            # Check API version
            if self.config:
                try:
                    api_version = int(self.config.get('api_version', 2))
                except ValueError:
                    raise ValueError('api_version must be an integer')
            else:
                api_version = 2

            user_name = self._get_user_name()
            context = {
                'model': model,
                'session': Session,
                'user': user_name,
                'api_version': api_version,
                'schema': schema,
                'ignore_auth': True,
            }

            if self.config and self.config.get('clean_tags', False):
                tags = package_dict.get('tags', [])
                package_dict['tags'] = self._clean_tags(tags)

            # Check if package exists
            try:
                # _find_existing_package can be overridden if necessary
                existing_package_dict = self._find_existing_package(package_dict)

                # In case name has been modified when first importing. See issue #101.
                package_dict['name'] = existing_package_dict['name']

                merge_base = self._get_merge_base(harvest_object)

                if self._upstream_version_changed(package_dict, merge_base):
                    log.info('Package with GUID %s exists and needs to be updated' % harvest_object.guid)

                    # Keep the upstream dict as it is now: it is the base used
                    # by the next three-way merge.
                    upstream_package_dict = copy.deepcopy(package_dict)

                    # Update package
                    context.update({'id': package_dict['id']})
                    package_dict.setdefault('name',
                                            existing_package_dict['name'])

                    package_dict, merge_conflicts = self._merge_package_fields(
                        package_dict, existing_package_dict, harvest_object,
                        merge_base=merge_base)

                    for field in p.toolkit.aslist(config.get('ckan.harvest.not_overwrite_fields')):
                        if field in existing_package_dict:
                            package_dict[field] = existing_package_dict[field]
                    new_package = p.toolkit.get_action(
                        'package_update' if package_dict_form == 'package_show'
                        else 'package_update_rest')(context, package_dict)

                    self._store_merge_metadata(
                        harvest_object, upstream_package_dict, merge_conflicts)
                else:
                    log.info('No changes to package with GUID %s, skipping...' % harvest_object.guid)
                    # NB harvest_object.current/package_id are not set
                    return 'unchanged'

                # Flag the other objects linking to this package as not current anymore
                Session.query(HarvestObject).filter(
                    HarvestObject.package_id == new_package["id"]).update(
                        {"current": False})

                # Flag this as the current harvest object
                harvest_object.package_id = new_package['id']
                harvest_object.current = True
                harvest_object.save()

            except p.toolkit.ObjectNotFound:
                # Package needs to be created

                # Get rid of auth audit on the context otherwise we'll get an
                # exception
                context.pop('__auth_audit', None)

                # Set name for new package to prevent name conflict, see issue #117
                if package_dict.get('name', None):
                    package_dict['name'] = self._gen_new_name(package_dict['name'])
                else:
                    package_dict['name'] = self._gen_new_name(package_dict['title'])

                log.info('Package with GUID %s does not exist, let\'s create it' % harvest_object.guid)
                harvest_object.current = True
                harvest_object.package_id = package_dict['id']

                # Keep the upstream dict as it is: it is the base used by the
                # next three-way merge. ``package_create`` may modify the dict
                # in place, so take the copy first.
                upstream_package_dict = copy.deepcopy(package_dict)

                # Defer constraints and flush so the dataset can be indexed with
                # the harvest object id (on the after_show hook from the harvester
                # plugin)
                harvest_object.add()

                model.Session.execute(
                    sa.text('SET CONSTRAINTS harvest_object_package_id_fkey DEFERRED')
                )
                model.Session.flush()

                new_package = p.toolkit.get_action(
                    'package_create' if package_dict_form == 'package_show'
                    else 'package_create_rest')(context, package_dict)

                # Record the upstream version this dataset was created from so
                # that the next harvest can perform a three-way merge.
                self._store_merge_metadata(
                    harvest_object, upstream_package_dict, [])

            Session.commit()

            return True

        except p.toolkit.ValidationError as e:
            log.exception(e)
            self._save_object_error('Invalid package with GUID %s: %r' % (harvest_object.guid, e.error_dict),
                                    harvest_object, 'Import')
        except Exception as e:
            log.exception(e)
            self._save_object_error('%r' % e, harvest_object, 'Import')

        return None

    def _find_existing_package(self, package_dict):
        data_dict = {'id': package_dict['id']}
        package_show_context = {'model': model, 'session': Session,
                                'ignore_auth': True}
        return p.toolkit.get_action('package_show')(
            package_show_context, data_dict)

    # ------------------------------------------------------------------
    # Three-way field merge
    #
    # Catalogers are allowed to maintain a small set of fields by hand (by
    # default the description/notes and the tags). Upstream still owns the
    # rest of the metadata (licenses, resources, organizations...). On every
    # import we compare three versions of the dataset:
    #
    #   base      - the upstream content that was used for the last
    #               successful import (``merge_base`` extra, or the content of
    #               the previous current harvest object)
    #   local     - the dataset as it is stored locally right now (possibly
    #               edited by a cataloger)
    #   upstream  - the freshly harvested upstream content
    #
    # A field is taken from upstream when the local copy is untouched, from
    # the local copy when upstream is untouched and, when both sides changed
    # it, from the side that has priority for that field. Every such genuine
    # conflict is recorded on the harvest object (``merge_conflicts`` extra)
    # so it can be reviewed later.
    # ------------------------------------------------------------------

    def _get_merge_fields(self):
        '''Returns the set of fields catalogers may maintain by hand.

        They are read, in order of precedence, from the harvest source
        config (``merge_fields``), the ``ckan.harvest.merge_fields`` ini
        option, or :data:`DEFAULT_MERGE_FIELDS`.
        '''
        merge_fields = None
        if isinstance(self.config, dict) and 'merge_fields' in self.config:
            merge_fields = self.config.get('merge_fields')
        if merge_fields is None:
            merge_fields = config.get('ckan.harvest.merge_fields')
        if merge_fields is None:
            return set(DEFAULT_MERGE_FIELDS)
        return set(p.toolkit.aslist(merge_fields))

    def _get_merge_base(self, harvest_object):
        '''Returns the upstream package dict used for the last successful
        import, or ``None`` if it is unknown.

        The value stored on the current harvest object is preferred. For
        records harvested before this information was stored we fall back to
        the content of the previous current harvest object, which is the last
        successfully imported upstream version.
        '''
        for extra in harvest_object.extras:
            if extra.key == MERGE_BASE_EXTRA:
                try:
                    return json.loads(extra.value)
                except (TypeError, ValueError):
                    log.warning('Ignoring invalid %s extra on harvest object %s',
                                MERGE_BASE_EXTRA, harvest_object.id)
                break

        previous = Session.query(HarvestObject).filter(
            HarvestObject.guid == harvest_object.guid,
            HarvestObject.current == True,  # noqa: E712
            HarvestObject.id != harvest_object.id,
        ).order_by(HarvestObject.import_finished.desc()).first()
        if previous:
            for extra in previous.extras:
                if extra.key == MERGE_BASE_EXTRA:
                    try:
                        return json.loads(extra.value)
                    except (TypeError, ValueError):
                        log.warning(
                            'Ignoring invalid %s extra on harvest object %s',
                            MERGE_BASE_EXTRA, previous.id)
                    break
            if previous.content:
                try:
                    return json.loads(previous.content)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _canonical(field, value):
        '''Returns a stable string representation of a field value.

        Values that only differ in dict ordering, in volatile data added by
        ``package_show`` (such as resource tracking information) or in the
        extra tag metadata compare as equal, so they are not mistaken for
        changes made by one of the sides.
        '''
        if field == 'tags':
            # An absent tag list and an empty one are the same thing: no tags.
            return HarvesterBase._canonical_value(
                HarvesterBase._tag_names(value)
                if value is not _MISSING else [])
        if field == 'resources' and isinstance(value, list):
            value = [HarvesterBase._canonical_resource(resource)
                     for resource in value]
        return HarvesterBase._canonical_value(value)

    @staticmethod
    def _canonical_value(value):
        if value is _MISSING:
            return '__missing__'
        return json.dumps(value, sort_keys=True, default=str)

    #: Resource keys that are informational / generated by CKAN and therefore
    #: must not influence whether a resource list changed.
    RESOURCE_VOLATILE_KEYS = frozenset([
        'id',
        'created',
        'last_modified',
        'revision_id',
        'revision_timestamp',
        'cache_last_updated',
        'cache_url',
        'cache_filepath',
        'datastore_active',
        'datastore_contains_all_records_of_source_file',
        'tracking_summary',
        'url_type',
        'openness_score',
        'openness_score_reason',
        'openness_score_failure_count',
        'mimetype_inner',
        'content_length',
        'package_id',
        'state',
    ])

    @classmethod
    def _canonical_resource(cls, resource):
        if not isinstance(resource, dict):
            return resource
        return dict(
            (key, value) for key, value in resource.items()
            if key not in cls.RESOURCE_VOLATILE_KEYS
        )

    def _values_equal(self, field, value_a, value_b):
        return self._canonical(field, value_a) == self._canonical(field, value_b)

    def _upstream_version_changed(self, package_dict, merge_base):
        '''Tells whether upstream published a different version than the one
        used for the last successful import.

        Re-collecting the same upstream version must be a no-op so that the
        result of a previous merge (including manual edits by catalogers) is
        preserved instead of being merged again.

        The recorded ``merge_base`` is the reference: comparing against the
        local ``metadata_modified`` would be wrong because a cataloger edit
        moves it forward and would then hide every later upstream update.

        When no base is recorded yet (records harvested before three-way
        merging was introduced) the import is performed once so that a base
        gets recorded; the merge itself falls back to the field priorities.
        '''
        if merge_base is None:
            return True

        upstream_modified = package_dict.get('metadata_modified')
        base_modified = merge_base.get('metadata_modified')
        if upstream_modified is not None and base_modified is not None:
            return upstream_modified != base_modified

        # Harvesters that do not provide a modification date: compare the
        # content itself, ignoring generated/volatile resource keys.
        return not self._values_equal(
            'package', self._normalize_package(package_dict),
            self._normalize_package(merge_base))

    @classmethod
    def _normalize_package(cls, package_dict):
        '''Returns a copy of a package dict suitable for comparing upstream
        versions, with volatile resource keys removed.'''
        if not isinstance(package_dict, dict):
            return package_dict
        normalized = dict(package_dict)
        if isinstance(normalized.get('resources'), list):
            normalized['resources'] = [
                cls._canonical_resource(resource)
                for resource in normalized['resources']]
        if isinstance(normalized.get('tags'), list):
            normalized['tags'] = cls._tag_names(normalized['tags'])
        return normalized

    @staticmethod
    def _tag_names(tags):
        if not tags:
            return []
        names = []
        for tag in tags:
            if isinstance(tag, dict):
                names.append(tag.get('name'))
            else:
                names.append(tag)
        return sorted(name for name in names if name)

    @staticmethod
    def _extras_to_dict(extras):
        '''Normalizes the two supported ``extras`` representations (a list of
        dicts or a plain dict) to a dict, dropping harvest managed extras.'''
        if not extras:
            return {}
        if isinstance(extras, dict):
            items = extras.items()
        else:
            items = [(extra.get('key'), extra.get('value'))
                     for extra in extras if isinstance(extra, dict)]
        return dict((key, value) for key, value in items
                    if key and key not in HARVEST_MANAGED_EXTRAS)

    def _build_extras(self, extras_dict, as_dict):
        if as_dict:
            return dict(extras_dict)
        return [{'key': key, 'value': value}
                for key, value in extras_dict.items()]

    def _resolve_field(self, field, base, local, upstream, merge_fields,
                       conflicts, has_base):
        '''Decides the value of a single field and records conflicts.

        ``base``/``local``/``upstream`` are values or ``_MISSING``. The field
        name is used both for priority lookup and for reporting; extras are
        passed as ``extras:<key>`` but looked up with the bare key.
        '''
        if self._values_equal(field, local, upstream):
            return upstream

        lookup_field = field.split('extras:', 1)[-1]
        prioritized = lookup_field in merge_fields

        if not has_base:
            # The previous upstream version is unknown, so we cannot tell
            # which side changed the field and there is nothing reliable to
            # report. Keep the local value for manually maintained fields and
            # let upstream win for everything else.
            log.debug('No merge base for field %s, keeping the %s value',
                      field, 'local' if prioritized else 'upstream')
            return local if prioritized else upstream

        if self._values_equal(field, local, base):
            # Local untouched, upstream wins (including deletions).
            return upstream
        if self._values_equal(field, upstream, base):
            # Upstream untouched, keep the local (manually maintained) value.
            return local

        # Both sides changed the field.
        resolution = 'local' if prioritized else 'upstream'
        conflicts.append({
            'field': field,
            'base': self._conflict_value(field, base),
            'local': self._conflict_value(field, local),
            'upstream': self._conflict_value(field, upstream),
            'resolution': resolution,
        })
        return local if prioritized else upstream

    def _conflict_value(self, field, value):
        '''Returns a JSON friendly, human readable value for the conflict
        report.'''
        if value is _MISSING:
            return None
        if field == 'tags':
            # Report tags by name instead of the whole tag dicts.
            return HarvesterBase._tag_names(value)
        return value

    def _merge_package_fields(self, package_dict, existing_package_dict,
                              harvest_object, merge_base=None):
        '''Returns ``(merged_dict, conflicts)`` for an update.

        ``package_dict`` is not modified.
        '''
        merge_fields = self._get_merge_fields()
        if merge_base is None:
            merge_base = self._get_merge_base(harvest_object)
        has_base = merge_base is not None
        base = merge_base or {}

        # The legacy REST form uses a plain dict for extras; the
        # ``package_show`` form uses a list of ``{'key', 'value'}`` dicts.
        extras_as_dict = isinstance(package_dict.get('extras'), dict) or \
            isinstance(existing_package_dict.get('extras'), dict)

        merged = copy.deepcopy(package_dict)
        conflicts = []

        # Merge the fields upstream published (now or in the base), plus the
        # manually maintained ones a cataloger added locally. Derived values
        # such as ``license_title`` or ``num_resources`` are never touched.
        candidate_fields = (
            set(package_dict) | set(base)
        ) - MERGE_SKIP_FIELDS - NON_MERGEABLE_FIELDS
        for field in merge_fields:
            if field in existing_package_dict:
                candidate_fields.add(field)
        candidate_fields -= MERGE_SKIP_FIELDS | NON_MERGEABLE_FIELDS

        for field in sorted(candidate_fields):
            resolved = self._resolve_field(
                field,
                base.get(field, _MISSING),
                existing_package_dict.get(field, _MISSING),
                package_dict.get(field, _MISSING),
                merge_fields, conflicts, has_base)
            if resolved is _MISSING:
                merged.pop(field, None)
            else:
                merged[field] = resolved

        # Tags are compared by name and rebuilt in the same format upstream
        # uses (a list of dicts for ``package_show``, strings for REST).
        resolved_tags = self._resolve_field(
            'tags',
            base.get('tags', _MISSING),
            existing_package_dict.get('tags', _MISSING),
            package_dict.get('tags', _MISSING),
            merge_fields, conflicts, has_base)
        if resolved_tags is _MISSING:
            merged['tags'] = []
        elif self._values_equal('tags', resolved_tags,
                                package_dict.get('tags', _MISSING)):
            merged['tags'] = package_dict.get('tags', [])
        else:
            tag_names = self._tag_names(resolved_tags)
            if isinstance(existing_package_dict.get('tags'), list) and any(
                    isinstance(tag, str)
                    for tag in existing_package_dict['tags']):
                merged['tags'] = tag_names
            else:
                merged['tags'] = [{'name': name} for name in tag_names]

        # Extras are merged key by key so that an upstream change to one
        # extra does not hide a cataloger's change to another one.
        base_extras = self._extras_to_dict(base.get('extras'))
        local_extras = self._extras_to_dict(existing_package_dict.get('extras'))
        upstream_extras = self._extras_to_dict(package_dict.get('extras'))

        merged_extras = dict(upstream_extras)
        for key in sorted(set(base_extras) | set(local_extras) |
                          set(upstream_extras)):
            resolved = self._resolve_field(
                'extras:%s' % key,
                base_extras.get(key, _MISSING),
                local_extras.get(key, _MISSING),
                upstream_extras.get(key, _MISSING),
                merge_fields, conflicts, has_base)
            if resolved is _MISSING:
                merged_extras.pop(key, None)
            else:
                merged_extras[key] = resolved

        if merged_extras or 'extras' in package_dict or \
                'extras' in existing_package_dict:
            merged['extras'] = self._build_extras(merged_extras,
                                                  extras_as_dict)

        if conflicts:
            log.warning(
                'Fields changed both locally and upstream for package with '
                'GUID %s: %s',
                harvest_object.guid,
                ', '.join(sorted(conflict['field'] for conflict in conflicts)))

        return merged, conflicts

    def _store_merge_metadata(self, harvest_object, merge_base, conflicts):
        '''Stores the merge base and the detected conflicts on the harvest
        object, replacing any values left over from a previous import.'''
        for extra in list(harvest_object.extras):
            if extra.key in (MERGE_BASE_EXTRA, MERGE_CONFLICTS_EXTRA):
                harvest_object.extras.remove(extra)

        harvest_object.extras.append(HarvestObjectExtra(
            key=MERGE_BASE_EXTRA,
            value=json.dumps(merge_base, default=str)))
        if conflicts:
            harvest_object.extras.append(HarvestObjectExtra(
                key=MERGE_CONFLICTS_EXTRA,
                value=json.dumps(conflicts, default=str)))

    def _clean_tags(self, tags):
        try:
            def _update_tag(tag_dict, key, newvalue):
                # update the dict and return it
                tag_dict[key] = newvalue
                return tag_dict

            # assume it's in the package_show form
            tags = [_update_tag(t, 'name', munge_tag(t['name'])) for t in tags if munge_tag(t['name']) != '']

        except TypeError:  # a TypeError is raised if `t` above is a string
            # REST format: 'tags' is a list of strings
            tags = [munge_tag(t) for t in tags if munge_tag(t) != '']
            tags = list(set(tags))
            return tags

        return tags

    @classmethod
    def last_error_free_job(cls, harvest_job):
        # TODO weed out cancelled jobs somehow.
        # look for jobs with no gather errors
        jobs = (model.Session.query(HarvestJob)
                .filter(HarvestJob.source == harvest_job.source)
                .filter(HarvestJob.gather_started != None)  # noqa: E711
                .filter(HarvestJob.status == 'Finished')
                .filter(HarvestJob.id != harvest_job.id)
                .filter(
            ~sa.exists().where(
                HarvestGatherError.harvest_job_id == HarvestJob.id))
                .outerjoin(HarvestObject,
                           sa.and_(HarvestObject.harvest_job_id == HarvestJob.id,
                                   HarvestObject.current == False,  # noqa: E712
                                   HarvestObject.report_status != 'not modified'))
                .options(contains_eager(HarvestJob.objects))
                .order_by(HarvestJob.gather_started.desc()))
        # now check them until we find one with no fetch/import errors
        # if objects count is 0, job was error free
        for job in jobs:
            if len(job.objects) == 0:
                return job
