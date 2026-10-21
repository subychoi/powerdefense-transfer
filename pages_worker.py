"""GitHub runner: build a Pages artifact from a temporary release ZIP.

Only this Python source and workflow YAML belong in Git. No media commits.
No Instagram/Google credentials exist in the runner.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import urllib.request
import urllib.error
import zipfile


def check(ok, message):
    if not ok:
        raise RuntimeError(message)


def build_site(payload, destination, batch_id, expected_sha):
    check(re.fullmatch(r'[a-f0-9]{24}', batch_id), 'INVALID_BATCH')
    check(hashlib.sha256(Path(payload).read_bytes()).hexdigest() == expected_sha, 'PAYLOAD_HASH_MISMATCH')
    root = Path(destination)
    check(not root.exists(), 'DESTINATION_MUST_BE_NEW')
    with zipfile.ZipFile(payload) as archive:
        files = archive.infolist()
        names = [info.filename for info in files]
        check(len(names) == len(set(names)) and 3 <= len(names) <= 21, 'INVALID_ZIP_ENTRIES')
        check(sum(info.file_size for info in files) <= 160_010_000, 'ZIP_TOO_LARGE')
        for info in files:
            path = PurePosixPath(info.filename)
            check(len(path.parts) == 1 and not path.is_absolute() and '..' not in path.parts
                  and not stat.S_ISLNK(info.external_attr >> 16), 'UNSAFE_ZIP_PATH')
        index = json.loads(archive.read('delivery.json'))
        check(index['batch_id'] == batch_id and 2 <= len(index['images']) <= 20, 'INVALID_DELIVERY')
        expected = {'delivery.json'} | {item['name'] for item in index['images']}
        check(set(names) == expected and len(expected) == len(names), 'UNEXPECTED_PAYLOAD_FILES')
        validated = []
        for item in index['images']:
            check(re.fullmatch(r'\d{2}-[a-f0-9]{64}\.jpg', item['name']), 'INVALID_IMAGE_NAME')
            check(item['name'][3:-4] == item['sha256'], 'HASH_NAME_MISMATCH')
            raw = archive.read(item['name'])
            check(0 < len(raw) <= 8_000_000 and raw.startswith(b'\xff\xd8\xff')
                  and hashlib.sha256(raw).hexdigest() == item['sha256'], 'INVALID_JPEG_HASH')
            validated.append((item['name'], raw))
    (root / batch_id).mkdir(parents=True)
    for name, raw in validated:
        (root / batch_id / name).write_bytes(raw)
    (root / '.nojekyll').touch()
    (root / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Temporary media transfer</title>')
    (root / 'active.json').write_text(json.dumps({'batch_id': batch_id, 'phase': 'active',
        'payload_sha256': expected_sha, 'image_names': [name for name, _ in validated]}))


def clear_site(destination, batch_id, base_url):
    check(re.fullmatch(r'[a-f0-9]{24}', batch_id), 'INVALID_BATCH')
    check(re.fullmatch(r'https://[A-Za-z0-9-]+\.github\.io/[A-Za-z0-9._-]+/', base_url), 'INVALID_PAGES_BASE')
    # Never replace a different, possibly still needed batch with an empty site.
    request = urllib.request.Request(base_url + 'active.json', headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=30) as response:
        active = json.load(response)
    check(active.get('batch_id') == batch_id, 'DIFFERENT_ACTIVE_BATCH_DO_NOT_CLEAR')
    root = Path(destination)
    check(not root.exists(), 'DESTINATION_MUST_BE_NEW')
    root.mkdir(parents=True)
    (root / '.nojekyll').touch()
    (root / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>Transfer complete</title>')
    (root / 'active.json').write_text(json.dumps({'batch_id': batch_id, 'phase': 'cleared'}))


def guard_deploy(base_url, batch_id, payload_sha):
    # Workflow concurrency serializes jobs; this also protects a still-live batch
    # against a later manual dispatch from another machine.
    try:
        with urllib.request.urlopen(base_url + 'active.json', timeout=30) as r:
            active = json.loads(r.read(8192))
    except urllib.error.HTTPError as error:
        check(error.code == 404, 'CURRENT_DEPLOYMENT_UNVERIFIED')
        return
    check(active.get('phase') == 'cleared' or (active.get('batch_id') == batch_id and
        active.get('payload_sha256') == payload_sha), 'ACTIVE_BATCH_MUST_BE_CLEANED_FIRST')


def assert_no_media_in_git():
    output = subprocess.run(['git', 'rev-list', '--objects', '--all'], check=True,
                            capture_output=True, text=True).stdout
    for line in output.splitlines():
        if ' ' in line:
            path = line.split(' ', 1)[1].lower()
            check(not path.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.zip', '.tar', '.gz', '.mp4')),
                  'MEDIA_FOUND_IN_GIT_HISTORY')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['deploy', 'clear'])
    args = parser.parse_args()
    assert_no_media_in_git()
    batch = os.environ['BATCH_ID']
    if args.mode == 'deploy':
        check(re.fullmatch(r'pd-transfer-[a-f0-9]{24}', os.environ['RELEASE_TAG']), 'INVALID_RELEASE_TAG')
        check(os.environ['RELEASE_TAG'] == 'pd-transfer-' + batch, 'RELEASE_BATCH_MISMATCH')
        guard_deploy(os.environ['PAGES_BASE'], batch, os.environ['PAYLOAD_SHA256'])
        subprocess.run(['gh', 'release', 'download', os.environ['RELEASE_TAG'], '--repo',
            os.environ['GITHUB_REPOSITORY'], '--pattern', 'payload.zip', '--dir', 'incoming'], check=True)
        build_site('incoming/payload.zip', 'site', batch, os.environ['PAYLOAD_SHA256'])
    else:
        clear_site('site', batch, os.environ['PAGES_BASE'])


if __name__ == '__main__':
    main()
