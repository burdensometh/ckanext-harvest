import sqlalchemy as sa
import pytest


@pytest.mark.usefixtures('with_plugins', 'clean_db')
def test_migration_upgrade_and_downgrade(migrate_db_for):
    from ckan import model

    engine = model.Session.bind
    insp = sa.inspect(engine)
    assert 'incremental_checkpoint' in [
        c['name'] for c in insp.get_columns('harvest_source')]
    assert 'incremental_since' in [
        c['name'] for c in insp.get_columns('harvest_job')]
    assert 'incremental_until' in [
        c['name'] for c in insp.get_columns('harvest_job')]

    # Downgrade one revision drops the columns.
    migrate_db_for('harvest', '75d650dfd519', forward=False)
    insp = sa.inspect(engine)
    assert 'incremental_checkpoint' not in [
        c['name'] for c in insp.get_columns('harvest_source')]
    assert 'incremental_since' not in [
        c['name'] for c in insp.get_columns('harvest_job')]
