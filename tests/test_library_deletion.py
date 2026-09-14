from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.repository import repository


def test_dataset_delete_removes_versions_and_files_after_unbinding(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'app.sqlite')
    with TestClient(app) as client:
        task_id = client.post('/api/v1/tasks').json()['id']
        uploaded = client.post(
            f'/api/v1/tasks/{task_id}/files?publish_to_library=true',
            files={'files': ('test.csv', b'name,value\na,1\n', 'text/csv')},
        )
        assert uploaded.status_code == 200
        asset_id = client.get('/api/v1/datasets').json()['items'][0]['id']
        directory = tmp_path / 'assets' / asset_id
        assert directory.exists()
        assert client.delete(f'/api/v1/datasets/{asset_id}').status_code == 409
        assert client.get(f'/api/v1/datasets/{asset_id}').json()['status'] == 'active'
        assert client.delete(f'/api/v1/tasks/{task_id}').status_code == 204
        assert client.delete(f'/api/v1/datasets/{asset_id}').status_code == 204
        assert not directory.exists()
        assert client.get(f'/api/v1/datasets/{asset_id}').status_code == 404
        with repository.connect() as connection:
            assert not connection.execute('PRAGMA foreign_key_check').fetchall()
            assert not connection.execute('SELECT 1 FROM dataset_revisions').fetchall()
