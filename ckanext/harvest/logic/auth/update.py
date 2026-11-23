from ckan.plugins import toolkit as pt
from ckanext.harvest.logic.auth import user_is_sysadmin


def harvest_source_update(context, data_dict):
    '''
        Authorization check for harvest source update

        It forwards the checks to package_update, which will check for
        organization membership, whether if sysadmin, etc according to the
        instance configuration.
    '''
    model = context.get('model')
    user = context.get('user')
    source_id = data_dict['id']

    pkg = model.Package.get(source_id)
    if not pkg:
        raise pt.ObjectNotFound(pt._('Harvest source not found'))

    context['package'] = pkg

    try:
        pt.check_access('package_update', context, data_dict)
        return {'success': True}
    except pt.NotAuthorized:
        return {'success': False,
                'msg': pt._('User {0} not authorized to update harvest source {1}').format(user, source_id)}


def harvest_quarantine_replay(context, data_dict):
    '''
        Authorization check for replaying quarantined records

        Only users who can update the relevant harvest source (i.e. catalogue
        administrators) can replay records.  When replaying a single entry the
        source is looked up from the entry.
    '''
    source_id = data_dict.get('source_id')
    entry_id = data_dict.get('id')

    if not source_id and entry_id:
        from ckanext.harvest.model import HarvestQuarantine
        entry = HarvestQuarantine.get(entry_id)
        if not entry:
            raise pt.ObjectNotFound(pt._('Quarantine entry not found'))
        source_id = entry.harvest_source_id

    if not source_id:
        # Replaying everything is an administrative operation.
        if not user_is_sysadmin(context):
            return {'success': False,
                    'msg': pt._('Only sysadmins can replay all quarantined records')}
        return {'success': True}

    return harvest_source_update(context, {'id': source_id})


def harvest_sources_clear(context, data_dict):
    '''
        Authorization check for clearing history for all harvest sources

        Only sysadmins can do it
    '''
    if not user_is_sysadmin(context):
        return {'success': False, 'msg': pt._('Only sysadmins can clear history for all harvest jobs')}
    else:
        return {'success': True}


def harvest_source_clear(context, data_dict):
    '''
        Authorization check for clearing a harvest source

        It forwards to harvest_source_update
    '''
    return harvest_source_update(context, data_dict)


def harvest_objects_import(context, data_dict):
    '''
        Authorization check reimporting all harvest objects

        Only sysadmins can do it
    '''
    if not user_is_sysadmin(context):
        return {'success': False, 'msg': pt._('Only sysadmins can reimport all harvest objects')}
    else:
        return {'success': True}


def harvest_jobs_run(context, data_dict):
    '''
        Authorization check for running the pending harvest jobs

        Only sysadmins can do it
    '''
    if not user_is_sysadmin(context):
        return {'success': False, 'msg': pt._('Only sysadmins can run the pending harvest jobs')}
    else:
        return {'success': True}


def harvest_send_job_to_gather_queue(context, data_dict):
    '''
        Authorization check for sending a job to the gather queue

        It forwards the checks to harvest_job_create, ie the user can only run
        the job if she is allowed to create the job.
    '''
    from ckanext.harvest.logic.auth.create import harvest_job_create
    return harvest_job_create(context, data_dict)


def harvest_job_abort(context, data_dict):
    '''
        Authorization check for aborting a running harvest job

        Same permissions as running one
    '''
    return harvest_jobs_run(context, data_dict)


def harvest_sources_reindex(context, data_dict):
    '''
        Authorization check for reindexing all harvest sources

        Only sysadmins can do it
    '''
    if not user_is_sysadmin(context):
        return {'success': False, 'msg': pt._('Only sysadmins can reindex all harvest sources')}
    else:
        return {'success': True}


def harvest_source_reindex(context, data_dict):
    '''
        Authorization check for reindexing a harvest source

        It forwards to harvest_source_update
    '''
    return harvest_source_update(context, data_dict)
