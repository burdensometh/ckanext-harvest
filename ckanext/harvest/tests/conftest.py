import os

import pytest


@pytest.fixture
def clean_db(reset_db, migrate_db_for):
    reset_db()
    migrate_db_for("harvest")


@pytest.fixture
def writable_storage_path(tmp_path, monkeypatch):
    """Point CKAN's file storage at a writable tmp directory.

    The container's default ``/var/lib/ckan`` is not writable by the
    test user. Harvest updates instantiate the resource uploader even
    without any file upload, which would otherwise raise
    ``PermissionError``.

    This deliberately patches ``ckan.common.config`` from inside the
    fixture body rather than via the ``ckan_config`` marker: the marker
    patch is applied by the ``ckan_config`` fixture, which is a
    dependency of ``with_plugins`` and therefore runs before plugin
    setup restores the initial config snapshot.
    """
    storage_path = str(tmp_path / "ckan-storage")
    os.makedirs(storage_path, exist_ok=True)

    from ckan.common import config

    monkeypatch.setitem(config, "ckan.storage_path", storage_path)
    return storage_path
