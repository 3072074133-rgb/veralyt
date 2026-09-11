"""Exercise followups on the dedicated monthly-report verification task."""
import time
import httpx

base = 'http://127.0.0.1:8000/api/v1/tasks/bcaddb4b-47b6-4cfa-8e9a-23da7c8bd8bb'
with httpx.Client(trust_env=False, timeout=30) as client:
    original = client.get(base).json()['active_run_id']
    for question, expected in [('你好', 'off_topic'), ('你觉得利润率怎么样', 'needs_clarification'),
                               ('净利润率', 'completed'), ('谢谢你', 'off_topic')]:
        response = client.post(base + '/messages', json={'content': question})
        response.raise_for_status()
        for _ in range(180):
            runs = client.get(base + '/runs').json()
            run = next(r for r in runs if r['question'] == question)
            if run['status'] not in {'queued', 'running'}:
                break
            time.sleep(0.25)
        assert run['status'] == expected, run
        print(question, run['status'], (run.get('result') or {}).get('summary', ''), flush=True)
    assert client.get(base).json()['active_run_id'] == original
    print('Original report preserved')
