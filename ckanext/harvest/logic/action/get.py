import hashlib
import json
import logging
from ckan.lib.base import config
from ckan.lib.helpers import date_str_to_datetime
from sqlalchemy import or_
from ckan.model import User, Package
import datetime

from ckan import logic
from ckan.plugins import PluginImplementations
from ckanext.harvest.interfaces import IHarvester

import ckan.plugins as p
from ckan.logic import NotFound, check_access, side_effect_free

from ckanext.harvest import model as harvest_model

from ckanext.harvest.model import (HarvestSource, HarvestJob, HarvestObject, HarvestLog)
from ckanext.harvest.logic.dictization import (harvest_source_dictize,
                                               harvest_job_dictize,
                                               harvest_object_dictize,
                                               harvest_log_dictize)

log = logging.getLogger(__name__)

#: Classification values for the entries returned by
#: ``harvest_source_catalog_at``
CATALOG_ENTRY_STATUSES = ("available", "pending", "withdrawn")


@side_effect_free
def harvest_source_show(context, data_dict):
    '''
    Returns the metadata of a harvest source

    This method just proxies the request to package_show. All auth checks and
    validation will be done there.

    :param id: the id or name of the harvest source
    :type id: string

    :param url: url of the harvest source (as an alternative to the id)
    :type url: string

    :returns: harvest source metadata
    :rtype: dictionary
    '''
    model = context.get('model')

    # Find the source by URL
    if data_dict.get('url') and not data_dict.get('id'):
        source = model.Session.query(harvest_model.HarvestSource) \
                      .filter_by(url=data_dict.get('url')) \
                      .first()
        if not source:
            raise NotFound
        data_dict['id'] = source.id

    source_dict = logic.get_action('package_show')(context, data_dict)

    # For compatibility with old code, add the active field
    # based on the package state
    source_dict['active'] = (source_dict['state'] == 'active')

    return source_dict


@side_effect_free
def harvest_source_show_status(context, data_dict):
    '''
    Returns a status report for a harvest source

    Given a particular source, returns a dictionary containing information
    about the source jobs, datasets created, errors, etc.
    Note that this information is already included on the output of
    harvest_source_show, under the 'status' field.

    :param id: the id or name of the harvest source
    :type id: string

    :rtype: dictionary
    '''

    p.toolkit.check_access('harvest_source_show_status', context, data_dict)

    model = context.get('model')

    source = harvest_model.HarvestSource.get(data_dict['id'])
    if not source:
        raise p.toolkit.ObjectNotFound('Harvest source {0} does not exist'.format(data_dict['id']))

    out = {
           'job_count': 0,
           'last_job': None,
           'total_datasets': 0,
           }

    jobs = harvest_model.HarvestJob.filter(source=source).all()

    job_count = len(jobs)
    if job_count == 0:
        return out

    out['job_count'] = job_count

    # Get the most recent job
    last_job = harvest_model.HarvestJob.filter(source=source) \
        .order_by(harvest_model.HarvestJob.created.desc()).first()

    if not last_job:
        return out

    out['last_job'] = harvest_job_dictize(last_job, context)

    # Overall statistics
    packages = model.Session.query(model.Package) \
        .join(harvest_model.HarvestObject) \
        .filter(harvest_model.HarvestObject.harvest_source_id == source.id) \
        .filter(
        harvest_model.HarvestObject.current == True  # noqa: E712
    ).filter(model.Package.state == u'active') \
        .filter(
        model.Package.private == False  # noqa: E712
    )
    out['total_datasets'] = packages.count()

    return out


@side_effect_free
def harvest_source_catalog_at(context, data_dict):
    '''
    Rebuild the catalog of a harvest source as it was at a given moment.

    Answers the question "which records of this source were already available
    when a report was produced on a given day?" by replaying the source's
    job history and the harvest object states up to (and including) the
    requested timestamp.

    Each entry of the catalog corresponds to one GUID known to the source at
    that moment and is classified as:

    * ``available``: the GUID had been successfully published by then. The
      entry carries the local dataset id (``package_id``) and the version
      that was in effect (``harvest_object_id``).
    * ``pending``: the GUID was already gathered but no import had finished
      successfully by then (it was still in flight or had only failed).
    * ``withdrawn``: the last successful import by then had marked the
      record as deleted (``report_status`` = "deleted").

    Periods of the source history whose results were not final at the
    requested moment (jobs still running, aborted or never finished) are
    reported explicitly under ``gaps``, together with the job ``coverage``
    window around the snapshot.

    Results are sorted by GUID and paginated with ``limit``/``offset``. The
    response echoes the effective parameters and includes a ``result_hash``
    computed over the full (unpaginated) result set, so separate page
    requests can be cross-checked against each other and against later
    re-runs.

    All timestamps are UTC.

    :param id: the id or name of the harvest source
    :type id: string
    :param at: ISO 8601 timestamp of the moment to reconstruct
    :type at: string
    :param status: only return entries of this kind: "available", "pending"
        or "withdrawn" (optional)
    :type status: string
    :param limit: maximum number of entries to return (default: 100)
    :type limit: int
    :param offset: offset of the first entry to return (default: 0)
    :type offset: int

    :rtype: dictionary with ``count``, ``result_hash``, ``summary``,
        ``results``, ``gaps`` and ``coverage`` keys
    '''

    # Validate the parameters before the auth check so that malformed
    # requests get a clean ValidationError (the auth functions expect a
    # source id to be present)
    source_ref = data_dict.get('id')
    if not source_ref:
        raise p.toolkit.ValidationError({'id': [u'Missing value']})

    at = _parse_catalog_at_timestamp(data_dict.get('at'))

    status_filter = data_dict.get('status')
    if status_filter is not None and status_filter not in CATALOG_ENTRY_STATUSES:
        raise p.toolkit.ValidationError({
            'status': [u'Must be one of: {0}'.format(
                ', '.join(CATALOG_ENTRY_STATUSES))]})

    limit = _parse_catalog_at_pagination(data_dict, 'limit', 100)
    offset = _parse_catalog_at_pagination(data_dict, 'offset', 0)

    check_access('harvest_source_catalog_at', context, data_dict)

    model = context['model']
    session = context['session']

    source = HarvestSource.get(source_ref)
    if not source:
        # Allow referencing the source by its dataset name
        pkg = model.Package.get(source_ref)
        if pkg:
            source = HarvestSource.get(pkg.id)
    if not source:
        raise p.toolkit.ObjectNotFound(
            'Harvest source {0} does not exist'.format(source_ref))

    # Every object of this source that already existed at the requested
    # moment. Availability is decided from the per-object timestamps
    # (``gathered``/``import_finished``), which are set once and never
    # rewritten, so they faithfully describe when each object was seen and
    # successfully imported.
    objects = session.query(HarvestObject) \
        .filter(HarvestObject.harvest_source_id == source.id) \
        .filter(HarvestObject.gathered <= at) \
        .all()

    by_guid = {}
    objects_per_job = {}
    for obj in objects:
        by_guid.setdefault(obj.guid, []).append(obj)
        objects_per_job[obj.harvest_job_id] = \
            objects_per_job.get(obj.harvest_job_id, 0) + 1

    entries = [_catalog_entry_at(guid, guid_objects, at)
               for guid, guid_objects in by_guid.items()]
    # Deterministic order so that paginated results can be re-checked
    entries.sort(key=lambda entry: entry['guid'])

    summary = dict.fromkeys(CATALOG_ENTRY_STATUSES, 0)
    for entry in entries:
        summary[entry['status']] += 1

    if status_filter is not None:
        entries = [e for e in entries if e['status'] == status_filter]

    result_hash = hashlib.sha1(json.dumps([
        [e['guid'], e['status'], e['package_id'], e['harvest_object_id']]
        for e in entries
    ]).encode('utf-8')).hexdigest()

    gaps, coverage = _catalog_gaps_and_coverage(
        session, source, at, objects_per_job)

    return {
        'source_id': source.id,
        'at': at.isoformat(),
        'count': len(entries),
        'limit': limit,
        'offset': offset,
        'result_hash': result_hash,
        'summary': summary,
        'results': entries[offset:offset + limit],
        'gaps': gaps,
        'coverage': coverage,
    }


def _parse_catalog_at_timestamp(value):
    '''Validate and normalize the ``at`` parameter to a naive UTC datetime.'''

    if value is None or value == '':
        raise p.toolkit.ValidationError({'at': [u'Missing value']})

    if isinstance(value, datetime.datetime):
        at = value
    else:
        try:
            at = date_str_to_datetime(str(value))
        except (TypeError, ValueError):
            raise p.toolkit.ValidationError(
                {'at': [u'Cannot parse timestamp {0!r}, '
                        u'use an ISO 8601 value'.format(value)]})

    if at.tzinfo is not None:
        at = at.astimezone(datetime.timezone.utc).replace(tzinfo=None)

    return at


def _parse_catalog_at_pagination(data_dict, key, default):
    '''Validate a ``limit``/``offset`` style parameter.'''

    value = data_dict.get(key, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise p.toolkit.ValidationError({key: [u'Must be an integer']})
    if value < 0:
        raise p.toolkit.ValidationError({key: [u'Must not be negative']})
    return value


def _iso_or_none(value):
    return value.isoformat() if value is not None else None


def _catalog_entry_at(guid, objects, at):
    '''Classify one GUID as it was at the given moment.

    ``objects`` are all the harvest objects of this GUID that were already
    gathered at that moment.
    '''

    # Imports that had successfully finished by then, oldest first. The
    # harvest object id breaks (unlikely) timestamp ties deterministically.
    completed = sorted(
        (obj for obj in objects
         if obj.state == u'COMPLETE'
         and obj.import_finished is not None
         and obj.import_finished <= at),
        key=lambda obj: (obj.import_finished,
                         obj.gathered or datetime.datetime.min,
                         obj.id))

    # The version in effect is the latest successful import that actually
    # set the local state: objects reported as "not modified" did not touch
    # the dataset (their ``package_id`` is usually empty), so they are
    # skipped in favour of the import they confirmed. Deletion markers do
    # set the local state (the dataset was withdrawn) even when they carry
    # no ``package_id``.
    effective = None
    last_package_id = None
    for obj in reversed(completed):
        if last_package_id is None and obj.package_id:
            last_package_id = obj.package_id
        if effective is None and \
                (obj.package_id or obj.report_status == u'deleted'):
            effective = obj

    first_gathered = min((obj.gathered for obj in objects if obj.gathered),
                         default=None)

    if effective is not None:
        return {
            'guid': guid,
            'status': (u'withdrawn' if effective.report_status == u'deleted'
                       else u'available'),
            'package_id': effective.package_id or last_package_id,
            'harvest_object_id': effective.id,
            'report_status': effective.report_status,
            'state': effective.state,
            'job_id': effective.harvest_job_id,
            'import_finished': _iso_or_none(effective.import_finished),
            'metadata_modified_date':
                _iso_or_none(effective.metadata_modified_date),
            'first_gathered': _iso_or_none(first_gathered),
            'attempts': len(objects),
        }

    # Known to the source but never successfully imported by then
    latest = max(objects,
                 key=lambda obj: (obj.gathered or datetime.datetime.min,
                                  obj.id))
    return {
        'guid': guid,
        'status': u'pending',
        'package_id': None,
        'harvest_object_id': None,
        'report_status': latest.report_status,
        'state': latest.state,
        'job_id': latest.harvest_job_id,
        'import_finished': None,
        'metadata_modified_date': None,
        'first_gathered': _iso_or_none(first_gathered),
        'attempts': len(objects),
    }


def _catalog_gaps_and_coverage(session, source, at, objects_per_job):
    '''Report the gaps of the source history at the given moment.

    A gap is any job created before the snapshot whose results were not
    final yet (still running, finished later, or aborted), plus the
    outright absence of any job finished before the snapshot. The coverage
    window brackets the snapshot with the last job finished before it and
    the first job created after it.
    '''

    gaps = []

    last_finished_job = session.query(HarvestJob) \
        .filter(HarvestJob.source_id == source.id) \
        .filter(HarvestJob.finished.isnot(None)) \
        .filter(HarvestJob.finished <= at) \
        .order_by(HarvestJob.finished.desc(), HarvestJob.id) \
        .first()

    if last_finished_job is None:
        gaps.append({
            'type': 'no_finished_job_before_snapshot',
            'reason': u'No job of this source had finished by the '
                      u'requested moment; the catalog is based on '
                      u'incomplete history',
            'job_id': None,
            'created': None,
            'finished': None,
            'status': None,
            'objects_gathered': sum(objects_per_job.values()),
        })

    unfinished_jobs = session.query(HarvestJob) \
        .filter(HarvestJob.source_id == source.id) \
        .filter(HarvestJob.created <= at) \
        .filter(or_(HarvestJob.finished.is_(None),
                    HarvestJob.finished > at)) \
        .order_by(HarvestJob.created, HarvestJob.id) \
        .all()

    for job in unfinished_jobs:
        if job.finished is not None:
            reason = u'job_finished_after_snapshot'
        elif job.status == u'Aborted':
            reason = u'job_aborted'
        else:
            reason = u'job_not_finished'
        gaps.append({
            'type': 'unfinished_job',
            'reason': reason,
            'job_id': job.id,
            'created': _iso_or_none(job.created),
            'finished': _iso_or_none(job.finished),
            'status': job.status,
            'objects_gathered': objects_per_job.get(job.id, 0),
        })

    next_job = session.query(HarvestJob) \
        .filter(HarvestJob.source_id == source.id) \
        .filter(HarvestJob.created > at) \
        .order_by(HarvestJob.created, HarvestJob.id) \
        .first()

    coverage = {
        'last_job_finished_before': {
            'id': last_finished_job.id,
            'finished': _iso_or_none(last_finished_job.finished),
        } if last_finished_job else None,
        'first_job_created_after': {
            'id': next_job.id,
            'created': _iso_or_none(next_job.created),
        } if next_job else None,
    }

    return gaps, coverage


@side_effect_free
def harvest_source_list(context, data_dict):
    '''
    TODO: Use package search
    '''

    organization_id = data_dict.get('organization_id')
    limit = config.get('ckan.harvest.harvest_source_limit', 100)

    sources = _get_sources_for_user(context, data_dict, organization_id=organization_id, limit=limit)

    last_job_status = p.toolkit.asbool(data_dict.get('return_last_job_status', False))

    return [harvest_source_dictize(source, context, last_job_status) for source in sources]


@side_effect_free
def harvest_job_show(context, data_dict):

    check_access('harvest_job_show', context, data_dict)

    id = data_dict.get('id')
    attr = data_dict.get('attr', None)

    job = HarvestJob.get(id, attr=attr)
    if not job:
        raise NotFound

    return harvest_job_dictize(job, context)


@side_effect_free
def harvest_job_report(context, data_dict):

    check_access('harvest_job_show', context, data_dict)

    model = context['model']
    id = data_dict.get('id')

    job = HarvestJob.get(id)
    if not job:
        raise NotFound

    report = {
        'gather_errors': [],
        'object_errors': {}
    }

    # Gather errors
    q = model.Session.query(harvest_model.HarvestGatherError) \
        .join(harvest_model.HarvestJob) \
        .filter(harvest_model.HarvestGatherError.harvest_job_id == job.id) \
        .order_by(harvest_model.HarvestGatherError.created.desc())

    for error in q.all():
        report['gather_errors'].append({
            'message': error.message
        })

    # Object errors

    # Check if the harvester for this job's source has a method for returning
    # the URL to the original document
    original_url_builder = None
    for harvester in PluginImplementations(IHarvester):
        if harvester.info()['name'] == job.source.type:
            if hasattr(harvester, 'get_original_url'):
                original_url_builder = harvester.get_original_url

    q = model.Session.query(harvest_model.HarvestObjectError, harvest_model.HarvestObject.guid) \
        .join(harvest_model.HarvestObject) \
        .filter(harvest_model.HarvestObject.harvest_job_id == job.id) \
        .order_by(harvest_model.HarvestObjectError.harvest_object_id)

    for error, guid in q.all():
        if error.harvest_object_id not in report['object_errors']:
            report['object_errors'][error.harvest_object_id] = {
                'guid': guid,
                'errors': []
            }
            if original_url_builder:
                url = original_url_builder(error.harvest_object_id)
                if url:
                    report['object_errors'][error.harvest_object_id]['original_url'] = url

        report['object_errors'][error.harvest_object_id]['errors'].append({
            'message': error.message,
            'line': error.line,
            'type': error.stage
         })

    return report


@side_effect_free
def harvest_job_list(context, data_dict):
    '''Returns a list of jobs and details of objects and errors.

    :param status: filter by e.g. "New" or "Finished" jobs
    :param source_id: filter by a harvest source
    '''

    check_access('harvest_job_list', context, data_dict)

    session = context['session']

    source_id = data_dict.get('source_id', False)
    status = data_dict.get('status', False)

    query = session.query(HarvestJob)

    if source_id:
        query = query.filter(HarvestJob.source_id == source_id)

    if status:
        query = query.filter(HarvestJob.status == status)

    query = query.order_by(HarvestJob.created.desc())

    jobs = query.all()

    context['return_error_summary'] = False
    return [harvest_job_dictize(job, context) for job in jobs]


@side_effect_free
def harvest_object_show(context, data_dict):

    p.toolkit.check_access('harvest_object_show', context, data_dict)

    id = data_dict.get('id')
    dataset_id = data_dict.get('dataset_id')

    if id:
        attr = data_dict.get('attr', None)
        obj = HarvestObject.get(id, attr=attr)
    elif dataset_id:
        model = context['model']

        pkg = model.Package.get(dataset_id)
        if not pkg:
            raise p.toolkit.ObjectNotFound('Dataset not found')

        obj = model.Session.query(HarvestObject) \
            .filter(HarvestObject.package_id == pkg.id) \
            .filter(
                HarvestObject.current == True  # noqa: E712
            ).order_by(HarvestObject.import_finished.desc()) \
            .first()
    else:
        raise p.toolkit.ValidationError(
            'Please provide either an "id" or a "dataset_id" parameter')

    if not obj:
        raise p.toolkit.ObjectNotFound('Harvest object not found')

    return harvest_object_dictize(obj, context)


@side_effect_free
def harvest_object_list(context, data_dict):

    check_access('harvest_object_list', context, data_dict)

    session = context['session']

    only_current = p.toolkit.asbool(data_dict.get('only_current', True))
    source_id = data_dict.get('source_id', False)

    query = session.query(HarvestObject)

    if source_id:
        query = query.filter(HarvestObject.harvest_source_id == source_id)

    if only_current:
        query = query.filter(
            HarvestObject.current == True  # noqa: E712
        )

    objects = query.all()

    return [getattr(obj, 'id') for obj in objects]


@side_effect_free
def harvesters_info_show(context, data_dict):
    '''Returns details of the installed harvesters.'''

    check_access('harvesters_info_show', context, data_dict)

    available_harvesters = []
    for harvester in PluginImplementations(IHarvester):
        info = harvester.info()
        if not info or 'name' not in info:
            log.error('Harvester %r does not provide the harvester name in the info response' % str(harvester))
            continue
        info['show_config'] = (info.get('form_config_interface', '') == 'Text')
        available_harvesters.append(info)

    return available_harvesters


@side_effect_free
def harvest_log_list(context, data_dict):
    '''Returns a list of harvester log entries.

    :param limit: number of logs to be shown default: 100
    :param offset: use with ``per_page`` default: 0
    :param level: filter log entries by level(debug, info, warning, error, critical)
    '''

    check_access('harvest_log_list', context, data_dict)

    session = context['session']

    try:
        limit = int(data_dict.get('limit', 100))
    except ValueError:
        limit = 100

    if data_dict.get('per_page', False):
        try:
            limit = int(data_dict.get('per_page', 100))
        except ValueError:
            limit = 100

    try:
        offset = int(data_dict.get('offset', 0))
    except ValueError:
        offset = 0

    level = data_dict.get('level', None)

    query = session.query(HarvestLog)

    if level is not None:
        query = query.filter(HarvestLog.level == level.upper())

    query = query.order_by(HarvestLog.created.desc())
    logs = query.offset(offset).limit(limit).all()

    out = [harvest_log_dictize(obj, context) for obj in logs]
    return out


def _get_sources_for_user(context, data_dict, organization_id=None, limit=None):

    session = context['session']
    user = context.get('user', '')

    only_active = p.toolkit.asbool(data_dict.get('only_active', False))
    only_to_run = p.toolkit.asbool(data_dict.get('only_to_run', False))

    query = session.query(HarvestSource) \
        .order_by(HarvestSource.created.desc())

    if organization_id:
        query = query.join(
            Package, HarvestSource.id == Package.id
        ).filter(Package.owner_org == organization_id)

    if only_active:
        query = query.filter(
            HarvestSource.active == True  # noqa: E712
        ) \

    if only_to_run:
        query = query.filter(HarvestSource.frequency != 'MANUAL')
        query = query.filter(or_(HarvestSource.next_run <= datetime.datetime.utcnow(),
                                 HarvestSource.next_run == None  # noqa: E711
                                 )
                             )

    user_obj = User.get(user)
    # Sysadmins will get all sources
    if user_obj and not user_obj.sysadmin:
        # This only applies to a non sysadmin user when using the
        # publisher auth profile. When using the default profile,
        # normal users will never arrive at this point, but even if they
        # do, they will get an empty list.

        publisher_filters = []
        publishers_for_the_user = user_obj.get_groups(u'publisher')
        for publisher_id in [g.id for g in publishers_for_the_user]:
            publisher_filters.append(HarvestSource.publisher_id == publisher_id)

        if len(publisher_filters):
            query = query.filter(or_(*publisher_filters))
        else:
            # This user does not belong to a publisher yet, no sources for him/her
            return []

        log.debug('User %s with publishers %r has Harvest Sources: %r',
                  user, publishers_for_the_user, [(hs.id, hs.url) for hs in query])

    sources = query.limit(limit).all() if limit else query.all()

    return sources


def harvest_get_notifications_recipients(context, data_dict):
    """ get all recipients for a harvest source
        Return a list of dicts like {'name': 'Jhon', 'email': jhon@source.com'} """

    check_access('harvest_get_notifications_recipients', context, data_dict)

    source_id = data_dict['source_id']
    source = p.toolkit.get_action('harvest_source_show')(context, {'id': source_id})
    recipients = []

    # gather sysadmins
    model = context['model']
    sysadmins = model.Session.query(model.User).filter(
        model.User.sysadmin == True  # noqa: E712
    ).all()

    # Send mail to all sysadmins with a non-empty email address
    for sysadmin in sysadmins:
        email_address = sysadmin.email
        if email_address and email_address.strip():
            recipients.append({
                'name': sysadmin.name,
                'email': email_address,
            })

    # gather organization-admins
    if source.get('organization'):
        members = p.toolkit.get_action('member_list')(context, {
            'id': source['organization']['id'],
            'object_type': 'user',
            'capacity': 'admin'
        })

        # Get access to email address by running action as admin user
        context['user'] = p.toolkit.get_action('get_site_user')({'ignore_auth': True})['name']
        for member in members:
            member_details = p.toolkit.get_action(
                'user_show')(context, {'id': member[0]})

            email_address = member_details.get('email', None)
            if email_address and email_address.strip():
                recipients.append({
                    'name': member_details['name'],
                    'email': email_address
                })

    return recipients
