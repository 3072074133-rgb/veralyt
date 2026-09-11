"""Create a separate analysis through the running local API; retain old tasks."""
import json
import time
from pathlib import Path

import httpx


def main():
    base = 'http://127.0.0.1:8000/api/v1'
    workbook = Path(__file__).resolve().parents[1] / 'tests/fixtures/monthly_financial_report.xlsx'
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.post(f'{base}/tasks')
        response.raise_for_status()
        task_id = response.json()['id']
        print(f'task_id={task_id}', flush=True)
        with workbook.open('rb') as stream:
            upload = client.post(f'{base}/tasks/{task_id}/files',
                                 files={'files': ('月度财务报表.xlsx', stream,
                                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
        upload.raise_for_status()
        if upload.json().get('rejected'):
            raise RuntimeError(upload.json())
        response = client.post(f'{base}/tasks/{task_id}/messages', json={'content': '分析报表'})
        response.raise_for_status()
        for _ in range(60):
            response = client.get(f'{base}/tasks/{task_id}')
            response.raise_for_status()
            snapshot = response.json()
            if snapshot['status'] in {'completed', 'completed_with_warnings', 'failed', 'needs_review', 'needs_clarification'}:
                print(json.dumps({'task_id': task_id, 'status': snapshot['status'],
                                  'error': snapshot.get('error'),
                                  'metrics': (snapshot.get('result') or {}).get('metrics')}, ensure_ascii=False))
                if snapshot['status'] not in {'completed', 'completed_with_warnings'}:
                    raise RuntimeError('Financial report did not complete')
                return
            time.sleep(0.5)
    raise TimeoutError('Analysis did not finish within 30 seconds')


if __name__ == '__main__':
    main()
