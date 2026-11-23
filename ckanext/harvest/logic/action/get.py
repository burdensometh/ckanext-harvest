import logging
from ckan.lib.base import config
from sqlalchemy import or_, and_, func
from sqlalchemy.orm import joinedload
from ckan.model import User, Package
import datetime

from ckan import logic
from ckan.plugins import PluginImplementations
from ckanext.harvest.interfaces import IHarvester

import ckan.plugins as p
from ckan.logic import NotFound, check_access, side_effect_free

from ckanext.harvest import model as harvest_model

from ckanext.harvest.model import (HarvestSource, HarvestJob, HarvestObject,
                                   HarvestLog, HarvestObjectChange,
                                   HarvestObjectError, HarvestGatherError)
from ckanext.harvest.logic.dictization import (harvest_source_dictize,
                                               harvest_job_dictize,
                                               harvest_object_dictize,
                                               harvest_log_dictize,
                                               harvest_object_change_dictize)

log = logging.getLogger(__name__)


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


# Default page size and hard cap for the change / failure feeds
_CHANGE_FEED_DEFAULT_LIMIT = 100
_CHANGE_FEED_MAX_LIMIT = 1000


def _parse_feed_limit(data_dict):
    try:
        limit = int(data_dict.get('limit', _CHANGE_FEED_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise p.toolkit.ValidationError(
            {'limit': ['Must be a positive integer']})
    if limit < 1:
        raise p.toolkit.ValidationError(
            {'limit': ['Must be a positive integer']})
    return min(limit, _CHANGE_FEED_MAX_LIMIT)


def _parse_cursor(cursor):
    """Decodes a ``(created, id)`` keyset cursor.

    The two columns together give a total, stable ordering even when
    several rows share the same timestamp.
    """
    if not cursor:
        return None
    try:
        created_str, row_id = cursor.split('|', 1)
        created = datetime.datetime.fromisoformat(created_str)
    except (ValueError, AttributeError):
        raise p.toolkit.ValidationError(
            {'cursor': ['Invalid cursor value']})
    return created, row_id


def _encode_cursor(created, row_id):
    return '{0}|{1}'.format(created.isoformat(), row_id)


@side_effect_free
def harvest_changes_list(context, data_dict):
    '''Returns the paginated feed of dataset changes.

    Only successfully published objects are represented here, as either
    an ``added`` or ``updated`` change. Each harvest object produces at
    most one change record (the mapping is unique), so re-processing the
    same object can never duplicate an entry.

    Pagination is keyset based and therefore stable while new records are
    inserted: pass the ``next_cursor`` value of one page as ``cursor`` on
    the next request. Plain ``offset`` is also supported for one-off
    queries.

    :param change_type: filter by ``added`` or ``updated``
    :param source_id: filter by harvest source id
    :param since: ISO datetime, only return changes created at/after it
    :param until: ISO datetime, only return changes created before it
    :param limit: page size (default 100, max 1000)
    :param cursor: keyset pagination cursor from a previous response
    :param offset: offset based pagination (mutually exclusive with
        ``cursor``)

    :rtype: dictionary with ``count``, ``results``, ``next_cursor`` and
        ``prev_cursor`` keys.
    '''
    check_access('harvest_changes_list', context, data_dict)

    session = context['session']
    limit = _parse_feed_limit(data_dict)

    query = session.query(HarvestObjectChange)

    change_type = data_dict.get('change_type')
    if change_type:
        if change_type not in (HarvestObjectChange.CHANGE_ADDED,
                               HarvestObjectChange.CHANGE_UPDATED):
            raise p.toolkit.ValidationError({
                'change_type': ['Must be one of: added, updated']
            })
        query = query.filter(
            HarvestObjectChange.change_type == change_type)

    source_id = data_dict.get('source_id')
    if source_id:
        query = query.filter(
            HarvestObjectChange.harvest_source_id == source_id)

    since = data_dict.get('since')
    if since:
        query = query.filter(HarvestObjectChange.created >= since)

    until = data_dict.get('until')
    if until:
        query = query.filter(HarvestObjectChange.created < until)

    # Total, deterministic ordering: newest first, id as tie breaker so
    # rows with identical timestamps always come out in the same order.
    query = query.options(joinedload(HarvestObjectChange.source))
    query = query.order_by(HarvestObjectChange.created.desc(),
                           HarvestObjectChange.id.desc())

    cursor = _parse_cursor(data_dict.get('cursor'))
    if cursor:
        created, row_id = cursor
        query = query.filter(
            or_(HarvestObjectChange.created < created,
                and_(HarvestObjectChange.created == created,
                     HarvestObjectChange.id < row_id)))
    elif data_dict.get('offset'):
        try:
            offset = int(data_dict.get('offset'))
        except (TypeError, ValueError):
            raise p.toolkit.ValidationError(
                {'offset': ['Must be a non-negative integer']})
        if offset < 0:
            raise p.toolkit.ValidationError(
                {'offset': ['Must be a non-negative integer']})
        query = query.offset(offset)

    count = query.order_by(None).count()
    # Fetch one extra row to know whether another page exists
    rows = query.limit(limit + 1).all()
    has_next = len(rows) > limit
    page = rows[:limit]

    results = [harvest_object_change_dictize(row, context)
               for row in page]

    next_cursor = None
    if has_next and page:
        next_cursor = _encode_cursor(page[-1].created, page[-1].id)

    return {
        'count': count,
        'results': results,
        'limit': limit,
        'next_cursor': next_cursor,
    }


@side_effect_free
def harvest_changes_failed_list(context, data_dict):
    '''Returns the paginated feed of objects that could not be processed.

    Failures are kept independent from successful changes. The feed
    merges two kinds of records:

    * object errors (``harvest_object_error``): fetch/import failures
      attached to a gathered object, identified by its guid;
    * gather errors (``harvest_gather_error``): job-level failures where
      no object could be gathered at all.

    One object appears at most once per page even if it accumulated
    several error messages: all messages are returned together on the
    single entry.

    The same filtering, keyset cursor and limit semantics as
    ``harvest_changes_list`` apply.
    '''
    check_access('harvest_changes_failed_list', context, data_dict)

    session = context['session']
    limit = _parse_feed_limit(data_dict)
    source_id = data_dict.get('source_id')
    since = data_dict.get('since')
    until = data_dict.get('until')

    object_filters = []
    gather_filters = []
    if source_id:
        object_filters.append(
            HarvestObject.harvest_source_id == source_id)
        gather_filters.append(HarvestJob.source_id == source_id)
    if since:
        object_filters.append(HarvestObjectError.created >= since)
        gather_filters.append(HarvestGatherError.created >= since)
    if until:
        object_filters.append(HarvestObjectError.created < until)
        gather_filters.append(HarvestGatherError.created < until)

    # Latest error per harvest object, one row per object regardless of
    # how many error messages it accumulated.
    latest_object_error = (
        session.query(
            HarvestObjectError.harvest_object_id.label('ref_id'),
            func.max(HarvestObjectError.created).label('created'),
        )
        .group_by(HarvestObjectError.harvest_object_id)
        .subquery()
    )
    object_query = (
        session.query(
            latest_object_error.c.created.label('created'),
            latest_object_error.c.ref_id.label('ref_id'),
        )
        .join(HarvestObject,
              HarvestObject.id == latest_object_error.c.ref_id)
        .filter(*object_filters)
    )

    gather_query = (
        session.query(
            HarvestGatherError.created.label('created'),
            HarvestGatherError.id.label('ref_id'),
        )
        .join(HarvestJob,
              HarvestJob.id == HarvestGatherError.harvest_job_id)
        .filter(*gather_filters)
    )

    unioned = object_query.union_all(gather_query).subquery()
    feed_query = session.query(
        unioned.c.created, unioned.c.ref_id
    ).order_by(unioned.c.created.desc(), unioned.c.ref_id.desc())

    cursor = _parse_cursor(data_dict.get('cursor'))
    if cursor:
        created, row_id = cursor
        feed_query = feed_query.filter(
            or_(unioned.c.created < created,
                and_(unioned.c.created == created,
                     unioned.c.ref_id < row_id)))
    elif data_dict.get('offset'):
        try:
            offset = int(data_dict.get('offset'))
        except (TypeError, ValueError):
            raise p.toolkit.ValidationError(
                {'offset': ['Must be a non-negative integer']})
        if offset < 0:
            raise p.toolkit.ValidationError(
                {'offset': ['Must be a non-negative integer']})
        feed_query = feed_query.offset(offset)

    count = session.query(unioned).count()
    rows = feed_query.limit(limit + 1).all()
    has_next = len(rows) > limit
    page = rows[:limit]

    object_ids = []
    gather_ids = []
    # The two subqueries can never share an id (object vs gather error
    # tables have independent primary keys), so classify by existence in
    # the harvest_object table in a single query.
    existing_object_ids = {
        row[0] for row in session.query(HarvestObject.id)
        .filter(HarvestObject.id.in_(
            [ref_id for _created, ref_id in page] or [None]))
        .all()
    }
    for _created, ref_id in page:
        if ref_id in existing_object_ids:
            object_ids.append(ref_id)
        else:
            gather_ids.append(ref_id)

    object_errors = (
        session.query(HarvestObjectError)
        .filter(HarvestObjectError.harvest_object_id.in_(
            object_ids or [None]))
        .all()
    )
    gather_errors = (
        session.query(HarvestGatherError)
        .filter(HarvestGatherError.id.in_(gather_ids or [None]))
        .all()
    )
    objects = {
        obj.id: obj
        for obj in session.query(HarvestObject)
        .filter(HarvestObject.id.in_(object_ids or [None]))
        .all()
    }
    gather_by_id = {err.id: err for err in gather_errors}

    results = []
    for created, ref_id in page:
        if ref_id in objects:
            obj = objects[ref_id]
            results.append({
                'failure_type': 'object_error',
                'id': obj.id,
                'created': created.isoformat(),
                'harvest_source_id': obj.harvest_source_id,
                'harvest_job_id': obj.harvest_job_id,
                'guid': obj.guid,
                'package_id': obj.package_id,
                'state': obj.state,
                'errors': [
                    {
                        'message': err.message,
                        'stage': err.stage,
                        'line': err.line,
                        'created': err.created.isoformat()
                        if err.created else None,
                    }
                    for err in object_errors
                    if err.harvest_object_id == obj.id
                ],
            })
        elif ref_id in gather_by_id:
            err = gather_by_id[ref_id]
            results.append({
                'failure_type': 'gather_error',
                'id': err.id,
                'created': created.isoformat(),
                'harvest_source_id': err.job.source_id
                if err.job else None,
                'harvest_job_id': err.harvest_job_id,
                'guid': None,
                'package_id': None,
                'state': None,
                'errors': [
                    {
                        'message': err.message,
                        'stage': 'Gather',
                        'line': None,
                        'created': err.created.isoformat()
                        if err.created else None,
                    }
                ],
            })

    next_cursor = None
    if has_next and page:
        last_created, last_id = page[-1]
        next_cursor = _encode_cursor(last_created, last_id)

    return {
        'count': count,
        'results': results,
        'limit': limit,
        'next_cursor': next_cursor,
    }
