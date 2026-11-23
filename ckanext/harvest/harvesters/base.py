# -*- coding: utf-8 -*-

import datetime
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

from ckanext.harvest.model import (HarvestObject, HarvestGatherError,
                                   HarvestObjectError, HarvestJob)

from ckan.plugins.core import SingletonPlugin, implements
from ckanext.harvest.interfaces import IHarvester
from ckanext.harvest.logic.schema import unicode_safe


log = logging.getLogger(__name__)

# Default look-back applied when resuming an incremental harvest. Existing
# installations worked with a hard-coded one hour, so keep that as the default
# while allowing it to be configured per source (``incremental_overlap``) or
# globally (``ckan.harvest.incremental_overlap``).
DEFAULT_INCREMENTAL_OVERLAP_MINUTES = 60

# A small fixed margin on top of the configured overlap. Timestamps coming
# from the remote search index may have a different precision (or clock skew)
# than the checkpoint we store, so moving the lower bound a bit earlier
# guarantees that records sharing the boundary timestamp are not skipped.
INCREMENTAL_SAFETY_MARGIN = datetime.timedelta(seconds=1)


class HarvesterBase(SingletonPlugin):
    '''
    Generic base class for harvesters, providing a number of useful functions.

    A harvester doesn't have to derive from this - it could just have:

        implements(IHarvester)
    '''
    implements(IHarvester)

    config = None

    _user_name = None

    # Harvesters whose source exposes a stable modification timestamp and a
    # stable dataset id ordering can set this to True. They then get a
    # resumable incremental window: the position is only advanced once every
    # harvested object in the window has been processed successfully.
    incremental_supported = False

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

    # ------------------------------------------------------------------
    # Resumable incremental windows
    # ------------------------------------------------------------------

    def _get_incremental_overlap(self):
        '''Return the overlap (as a ``timedelta``) to apply when resuming an
        incremental harvest.

        The overlap re-fetches records whose modification timestamp falls
        slightly before the stored checkpoint, so that updates which arrive
        late on the remote side (or whose ``metadata_modified`` is older than
        the moment they became visible) are not missed.

        It can be configured per source with the ``incremental_overlap`` key
        in the harvester config (in minutes) or globally with the
        ``ckan.harvest.incremental_overlap`` option. When neither is set the
        historical one-hour look-back is used.
        '''
        overlap = None
        if self.config:
            overlap = self.config.get('incremental_overlap')
        if overlap is None:
            overlap = config.get('ckan.harvest.incremental_overlap')
        if overlap is None:
            overlap = DEFAULT_INCREMENTAL_OVERLAP_MINUTES

        try:
            overlap_minutes = float(overlap)
        except (TypeError, ValueError):
            log.warning(
                'Invalid incremental overlap %r, falling back to %s minutes',
                overlap, DEFAULT_INCREMENTAL_OVERLAP_MINUTES)
            overlap_minutes = DEFAULT_INCREMENTAL_OVERLAP_MINUTES

        if overlap_minutes < 0:
            log.warning(
                'Negative incremental overlap %r, using 0 instead', overlap)
            overlap_minutes = 0

        return datetime.timedelta(minutes=overlap_minutes)

    @classmethod
    def _serialize_incremental_timestamp(cls, value):
        '''Serialize a datetime for use in a Solr range query.

        CKAN archives ``metadata_modified`` in UTC. Naive datetimes coming
        from the database are therefore assumed to be UTC already.
        '''
        if value is None:
            return None
        if value.tzinfo is not None:
            value = value.astimezone(datetime.timezone.utc).replace(
                tzinfo=None)
        # Solr expects a trailing ``Z`` and understands microseconds.
        return value.isoformat()

    def _get_incremental_window(self, harvest_job, now=None):
        '''Compute the incremental window for a job.

        Returns a ``(since, until)`` tuple of naive UTC datetimes, or
        ``(None, None)`` when the harvester cannot use an incremental window
        (either because the source type does not support it or because
        ``force_all`` is set).

        ``since`` is ``None`` for the first run (there is no checkpoint yet),
        meaning a full harvest is performed. On later runs ``since`` is the
        stored checkpoint moved back by the configured overlap plus a safety
        margin, so that records sharing the boundary timestamp (or arriving
        late) are included.

        ``until`` is the upper bound captured at the start of the gather
        stage. It is persisted on the job and only becomes the new checkpoint
        once every object gathered for the window has been processed.
        '''
        if not self.incremental_supported:
            return None, None

        until = now or datetime.datetime.utcnow()
        checkpoint = getattr(harvest_job.source, 'incremental_checkpoint',
                             None)

        if self.config and self.config.get('force_all', False):
            # A forced full harvest still establishes/advances the
            # checkpoint once every gathered object has been processed.
            return None, until

        if checkpoint is None:
            # No completed window yet - behave like the historical full run,
            # but remember ``until`` so a checkpoint can be established.
            return None, until

        since = checkpoint - self._get_incremental_overlap() \
            - INCREMENTAL_SAFETY_MARGIN

        if since >= until:
            # Defensive: never produce an empty or inverted window because of
            # clock skew between the checkpoint and ``until``.
            since = until - INCREMENTAL_SAFETY_MARGIN

        return since, until

    def _set_incremental_window(self, harvest_job, since, until):
        '''Record the window bounds on the job so the checkpoint can be
        advanced (or not) when the job finishes.

        The values are only set on the in-memory object; the caller (usually
        :func:`ckanext.harvest.queue.gather_stage`) persists the job when the
        gather stage completes. This avoids committing a half-finished gather
        just to store the bounds.
        '''
        if not self.incremental_supported:
            return
        harvest_job.incremental_since = since
        harvest_job.incremental_until = until

    @staticmethod
    def _job_incremental_complete(harvest_job):
        '''Return True when every object gathered for the job's window has
        been processed successfully.

        Only objects in the ``COMPLETE`` state count as done. Objects still
        in flight (``WAITING``/``FETCH``/``IMPORT``) or that ended in
        ``ERROR`` keep the window open, so the next run will request the same
        window again and re-collect the records that failed.
        '''
        unfinished = (
            model.Session.query(HarvestObject.id)
            .filter(HarvestObject.harvest_job_id == harvest_job.id)
            .filter(HarvestObject.state != 'COMPLETE')
            .count()
        )
        return unfinished == 0

    def advance_incremental_checkpoint(self, harvest_job):
        '''Advance the source checkpoint if the job's window is complete.

        This is called when a job is finished. It only moves the checkpoint
        forward when:

        * the job actually gathered an incremental window, and
        * every object in that window was processed successfully
          (``COMPLETE``), and
        * the job finished without gather errors (an aborted gather means we
          cannot know which records were seen).

        An object that ended in ``ERROR`` keeps the window open on purpose:
        the position is only advanced once the whole window has been processed
        successfully, so a failure in any single object leaves the position
        where it was. The next run re-requests the same window (with the
        overlap) and collects the failed records again.

        :returns: the new checkpoint (or ``None`` if it was not advanced)
        '''
        if not self.incremental_supported:
            return None

        until = getattr(harvest_job, 'incremental_until', None)
        if until is None:
            return None

        if not self._job_incremental_complete(harvest_job):
            log.info(
                'Not advancing incremental checkpoint for job %s: objects '
                'still pending', harvest_job.id)
            return None

        if harvest_job.gather_errors:
            log.info(
                'Not advancing incremental checkpoint for job %s: gather '
                'errors present', harvest_job.id)
            return None

        source = harvest_job.source
        if source is None:
            log.warning(
                'Cannot advance incremental checkpoint for job %s: source '
                'is missing', harvest_job.id)
            return None
        current = getattr(source, 'incremental_checkpoint', None)
        if current is not None and current >= until:
            return current

        source.incremental_checkpoint = until
        source.save()
        log.info(
            'Advanced incremental checkpoint for source %s to %s',
            source.id, until)
        return until

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

                # Check modified date
                if 'metadata_modified' not in package_dict or \
                   package_dict['metadata_modified'] > existing_package_dict.get('metadata_modified'):
                    log.info('Package with GUID %s exists and needs to be updated' % harvest_object.guid)
                    # Update package
                    context.update({'id': package_dict['id']})
                    package_dict.setdefault('name',
                                            existing_package_dict['name'])

                    for field in p.toolkit.aslist(config.get('ckan.harvest.not_overwrite_fields')):
                        if field in existing_package_dict:
                            package_dict[field] = existing_package_dict[field]
                    new_package = p.toolkit.get_action(
                        'package_update' if package_dict_form == 'package_show'
                        else 'package_update_rest')(context, package_dict)

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
