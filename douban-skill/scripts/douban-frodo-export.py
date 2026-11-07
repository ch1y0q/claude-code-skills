#!/usr/bin/env python3
"""
Douban Collection Full Export via Frodo API (Mobile App Backend)

Exports available book/movie/music/game collections to timestamped CSV snapshots.
No login or cookies required — uses HMAC-SHA1 signature auth.

The API key and HMAC secret are Douban's mobile app credentials, extracted from
the public APK. They are the same for all users and do not identify you. No
personal credentials are used or stored. Data is fetched only from frodo.douban.com.

Usage:
    DOUBAN_USER=<user_id> python3 douban-frodo-export.py
    DOUBAN_USER=<user_id> DOUBAN_OUTPUT_DIR=/custom/path python3 douban-frodo-export.py

Environment:
    DOUBAN_USER (required): Douban user ID from profile URL
    DOUBAN_OUTPUT_DIR (optional): Override output directory
"""

import hmac
import hashlib
import base64
import csv
import json
import os
import platform
import re
import socket
import sys
import time
import tempfile
import shutil
import datetime
import urllib.parse
import urllib.request
import urllib.error

# --- Frodo API Auth ---
# Public credentials from the Douban Android APK, shared by all app users.
API_KEY = '0dad551ec0f84ed02907ff5c42e8ec70'
HMAC_SECRET = b'bf7dddc7c9cfe6f7'
BASE_URL = 'https://frodo.douban.com'
USER_AGENT = (
    'api-client/1 com.douban.frodo/7.22.0.beta9(231) Android/23 '
    'product/Mate40 vendor/HUAWEI model/Mate40 brand/HUAWEI '
    'rom/android network/wifi platform/AndroidPad'
)

# --- Rate Limiting ---
# 1.5s between pages, 2s between categories. Tested with 1200+ items.
PAGE_DELAY = 1.5
CATEGORY_DELAY = 2.0
ITEMS_PER_PAGE = 50
MAX_PAGES_SAFETY = 500  # Guard against infinite pagination loops

# --- Category Definitions ---
CATEGORIES = [
    ('book',  'done',  '读过', '书.csv'),
    ('book',  'doing', '在读', '书.csv'),
    ('book',  'mark',  '想读', '书.csv'),
    ('movie', 'done',  '看过', '影视.csv'),
    ('movie', 'doing', '在看', '影视.csv'),
    ('movie', 'mark',  '想看', '影视.csv'),
    ('music', 'done',  '听过', '音乐.csv'),
    ('music', 'doing', '在听', '音乐.csv'),
    ('music', 'mark',  '想听', '音乐.csv'),
    ('game',  'done',  '玩过', '游戏.csv'),
    ('game',  'doing', '在玩', '游戏.csv'),
    ('game',  'mark',  '想玩', '游戏.csv'),
]

URL_PREFIX = {
    'book':  'https://book.douban.com/subject/',
    'movie': 'https://movie.douban.com/subject/',
    'music': 'https://music.douban.com/subject/',
    'game':  'https://www.douban.com/game/',
}

CSV_FIELDS = ['title', 'url', 'date', 'rating', 'status', 'comment']


def get_download_dir():
    """Get the platform-appropriate Downloads directory."""
    system = platform.system()
    if system == 'Darwin':
        return os.path.expanduser('~/Downloads')
    elif system == 'Windows':
        return os.path.join(os.environ.get('USERPROFILE', os.path.expanduser('~')), 'Downloads')
    else:
        return os.path.expanduser('~/Downloads')


def get_output_dir(user_id):
    """Determine output directory from env or platform default."""
    base = os.environ.get('DOUBAN_OUTPUT_DIR')
    if not base:
        base = os.path.join(get_download_dir(), 'douban-sync')
    return os.path.join(base, user_id)


def compute_signature(url_path, timestamp):
    """Compute Frodo API HMAC-SHA1 signature.

    Signs: METHOD & url_encoded_path & timestamp (path only, no query params).
    """
    raw = '&'.join(['GET', urllib.parse.quote(url_path, safe=''), timestamp])
    sig = hmac.new(HMAC_SECRET, raw.encode(), hashlib.sha1)
    return base64.b64encode(sig.digest()).decode()


def fetch_json(url, params):
    """Make an authenticated GET request to the Frodo API.

    Returns (data_dict, status_code). Catches HTTP errors, network errors,
    and timeouts — all return a synthetic error dict so the caller can retry.
    """
    query = urllib.parse.urlencode(params)
    full_url = f'{url}?{query}'
    req = urllib.request.Request(full_url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode('utf-8')), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:200]
        return {'error': body, 'code': e.code}, e.code
    except urllib.error.URLError as e:
        return {'error': f'Network error: {e.reason}'}, 0
    except socket.timeout:
        return {'error': 'Request timed out'}, 0
    except json.JSONDecodeError as e:
        return {'error': f'Invalid JSON response: {e}'}, 0


def preflight_check(user_id):
    """Verify user exists by fetching one page of book interests.

    Returns True if the user has any data, False if the user ID appears invalid.
    Prints a warning and continues if the check itself fails (network issue).
    """
    api_path = f'/api/v2/user/{user_id}/interests'
    ts = str(int(time.time()))
    sig = compute_signature(api_path, ts)
    params = {
        'type': 'book', 'status': 'done', 'start': 0, 'count': 1,
        'apiKey': API_KEY, '_ts': ts, '_sig': sig, 'os_rom': 'android',
    }
    data, code = fetch_json(f'{BASE_URL}{api_path}', params)
    if code == 0:
        print(f'Warning: Could not verify user ID (network issue). Proceeding anyway.')
        return True
    if code != 200:
        print(f'Error: API returned HTTP {code} for user "{user_id}".')
        print(f'  Check that the user ID is correct (from douban.com/people/<ID>/).')
        return False
    total = data.get('total', -1)
    if total == -1:
        print(f'Warning: Unexpected API response. Proceeding anyway.')
        return True
    return True


EXPORT_AUDIT = []


def fetch_all_interests(user_id, type_name, status):
    """Traverse API offset slots, not returned item count; fail closed."""
    api_path = f'/api/v2/user/{user_id}/interests'
    all_items, seen = [], set()
    start, total, duplicates, pages = 0, None, 0, 0
    while pages < MAX_PAGES_SAFETY:
        for attempt in range(4):
            ts = str(int(time.time()))
            params = {'type': type_name, 'status': status, 'start': start,
                      'count': ITEMS_PER_PAGE, 'apiKey': API_KEY, '_ts': ts,
                      '_sig': compute_signature(api_path, ts), 'os_rom': 'android'}
            data, code = fetch_json(f'{BASE_URL}{api_path}', params)
            if code == 200:
                break
            # Error bodies can contain request credentials; do not log them.
            print(f'  Request failed: HTTP {code}, attempt {attempt + 1}/4')
            if attempt == 3:
                raise RuntimeError(f'{type_name}/{status}: page {start} failed (HTTP {code}); no backup published')
            time.sleep(5 * (2 ** attempt))
        if not isinstance(data, dict) or type(data.get('total')) is not int or not isinstance(data.get('interests'), list):
            raise RuntimeError('Invalid API schema; no backup published')
        if total is None:
            total = data['total']
            if total < 0:
                raise RuntimeError('Invalid negative total')
        elif data['total'] != total:
            raise RuntimeError('Collection changed during export; no backup published')
        if 'start' in data and data['start'] != start:
            raise RuntimeError('API returned unexpected offset')
        if 'count' in data and data['count'] != ITEMS_PER_PAGE:
            raise RuntimeError('API changed page size; pagination needs review')
        items = data['interests']
        if len(items) > ITEMS_PER_PAGE:
            raise RuntimeError('API page exceeds requested size; no backup published')
        for item in items:
            sid = item.get('subject', {}).get('id')
            if sid is None or str(sid) == '':
                raise RuntimeError('Missing subject identity; no backup published')
            key = str(sid)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            all_items.append(item)
        if len(all_items) > total:
            raise RuntimeError('Unique records exceed reported total; no backup published')
        pages += 1
        print(f'  Offset {start}, returned {len(items)}, unique {len(all_items)}, reported {total}')
        # Probe one page beyond the reported range to confirm exhaustion.
        if start >= total:
            if items:
                raise RuntimeError('Unexpected records beyond total; no backup published')
            EXPORT_AUDIT.append({'type': type_name, 'status': status,
                'reported_total': total, 'unique_exported': len(all_items),
                'unavailable_slots': total-len(all_items), 'duplicates_removed': duplicates,
                'pages': pages, 'terminal_empty_offset': start})
            return all_items
        start += ITEMS_PER_PAGE
        time.sleep(PAGE_DELAY)
    raise RuntimeError('Pagination safety limit reached; no backup published')


def extract_rating(interest):
    """Convert Frodo API rating to star string.

    Frodo returns {value: N, max: 5} where N is 1-5.
    Some older entries may use max=10 scale (value 2-10).
    API values are typically integers; round() handles any edge cases.
    """
    r = interest.get('rating')
    if not r or not isinstance(r, dict):
        return ''
    val = r.get('value', 0)
    max_val = r.get('max', 5)
    if not val:
        return ''
    stars = round(val) if max_val <= 5 else round(val / 2)
    return '★' * max(0, min(5, stars))


def interest_to_row(interest, type_name, status_cn):
    """Convert a single Frodo API interest object to a CSV row dict."""
    subject = interest.get('subject', {})
    sid = subject.get('id', '')
    prefix = URL_PREFIX.get(type_name, 'https://www.douban.com/subject/')
    url = f'{prefix}{sid}/' if sid else subject.get('url', '')

    date_raw = interest.get('create_time', '') or ''
    date = date_raw[:10] if re.match(r'\d{4}-\d{2}-\d{2}', date_raw) else ''

    return {
        'title': subject.get('title', ''),
        'url': url,
        'date': date,
        'rating': extract_rating(interest),
        'status': status_cn,
        'comment': interest.get('comment', ''),
    }


def write_csv(filepath, rows):
    """Write rows to a CSV file with UTF-8 BOM for Excel compatibility."""
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    EXPORT_AUDIT.clear()
    user_id = os.environ.get('DOUBAN_USER', '').strip()
    if not user_id:
        print('Error: DOUBAN_USER environment variable is required')
        print('Usage: DOUBAN_USER=<your_douban_id> python3 douban-frodo-export.py')
        sys.exit(1)

    # Extract ID from URL if user pasted a full profile URL
    url_match = re.search(r'douban\.com/people/([A-Za-z0-9._-]+)', user_id)
    if url_match:
        user_id = url_match.group(1)

    if not re.match(r'^[A-Za-z0-9._-]+$', user_id):
        print(f'Error: DOUBAN_USER contains invalid characters: {user_id}')
        sys.exit(1)

    output_dir = get_output_dir(user_id)
    print(f'Douban Export for user: {user_id}')
    print(f'Output directory: {output_dir}\n')

    # Pre-flight: verify user ID is valid before spending time on full export
    if not preflight_check(user_id):
        sys.exit(1)

    # Collect data grouped by output file, then write all at the end.
    file_data = {}
    grand_total = 0

    for type_name, status, status_cn, outfile in CATEGORIES:
        print(f'=== {status_cn} ({type_name}) ===')
        items = fetch_all_interests(user_id, type_name, status)

        if outfile not in file_data:
            file_data[outfile] = []

        for item in items:
            file_data[outfile].append(interest_to_row(item, type_name, status_cn))

        count = len(items)
        grand_total += count
        if count > 0:
            print(f'  Collected: {count}\n')
        else:
            print(f'  (empty)\n')

        time.sleep(CATEGORY_DELAY)

    # Publish only complete snapshots. Never overwrite an existing backup.
    os.makedirs(output_dir, exist_ok=True)
    staging = tempfile.mkdtemp(prefix='.pending-', dir=output_dir)
    snapshot = os.path.join(output_dir, 'snapshot-' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    try:
        print('--- Writing verified snapshot ---')
        for filename, rows in file_data.items():
            keys = [(r['url'], r['status']) for r in rows]
            if len(keys) != len(set(keys)):
                raise RuntimeError('Duplicate CSV records; no backup published')
            write_csv(os.path.join(staging, filename), rows)
            print(f'  {filename}: {len(rows)} rows')
        with open(os.path.join(staging, 'manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'user_id': user_id, 'total_rows': grand_total, 'categories': EXPORT_AUDIT,
                       'coverage': 'all API offset slots traversed; unavailable slots are not exported'}, f, ensure_ascii=False, indent=2)
        os.rename(staging, snapshot)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(f'\nDone! {grand_total} unique items exported to {snapshot}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\nExport interrupted by user.')
        sys.exit(130)
