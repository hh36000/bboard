#!/usr/bin/env python3
"""Exercise the real local API with two deterministic workers, including retries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


def request(base, path, payload=None, idempotency_key=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Accept': 'application/json'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    if idempotency_key is not None:
        headers['Idempotency-Key'] = idempotency_key
    with urlopen(Request(base + path, data=data, headers=headers), timeout=40) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='http://localhost:6600')
    args = parser.parse_args()
    base = args.base.rstrip('/')
    if urlsplit(base).hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('This demonstration only writes to a local development server.')
    creation = {'content': 'coordinator: Check that both workers can share this board.\n'}
    creation_key = str(uuid4())
    board = request(base, '/write', creation, creation_key)
    recovered = request(base, '/write', creation, creation_key)
    assert recovered['key'] == board['key'] and recovered['duplicate']
    path = '/' + board['key']
    print('Open the live board:', board['url'], flush=True)
    print('Two deterministic HTTP workers. No model calls.', flush=True)

    def reviewer():
        incoming = request(base, path + '/wait?after_revision=0&timeout_seconds=5')
        assert incoming['changed'] and 'researcher:' in incoming['content']
        return request(base, path + '/append', {'content': '\nreviewer: Read the research result and confirmed it.\n'}, str(uuid4()))

    with ThreadPoolExecutor(1) as pool:
        reply = pool.submit(reviewer)
        addition = {'content': '\nresearcher: Both workers can reach the same board.\n'}
        addition_key = str(uuid4())
        request(base, path + '/append', addition, addition_key)  # Deliberately discard the save receipt.
        retried = request(base, path + '/append', addition, addition_key)
        assert retried['duplicate'] and retried['appended_revision'] == 1
        reviewed = reply.result(timeout=10)
    final = request(base, path + '?format=json')
    assert final['revision'] == 2 and reviewed['revision'] == 2
    assert final['content'].count('researcher:') == 1 and final['content'].count('reviewer:') == 1
    history = request(base, path + '/events')
    original = request(base, path + '/revisions/0')
    assert [event['revision'] for event in history['events']] == [0, 1, 2]
    assert all(event['recorded_at'].endswith('Z') for event in history['events'])
    assert original['content'] == creation['content']
    print('PASS: create retry, worker wait/reply, lost-response retry, final read, UTC history and original state. Revision 2; each contribution appears once.')


if __name__ == '__main__':
    main()
