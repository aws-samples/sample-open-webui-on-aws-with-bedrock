"""Run pinned Open WebUI's real provider function without importing the application."""

import ast
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest


def external_provider(post):
    root = os.environ.get('OWUI_TEST_SOURCE_DIR')
    if not root:
        pytest.skip('Set OWUI_TEST_SOURCE_DIR to the documented pinned source checkout')
    path = Path(root) / 'backend/open_webui/retrieval/web/external.py'
    assert path.is_file(), 'Configured pinned Open WebUI source is missing'
    syntax = ast.parse(path.read_text())
    definition = next(item for item in syntax.body if isinstance(item, ast.FunctionDef) and item.name == 'search_external')
    namespace = {'List': list, 'Optional': Optional, 'Request': object,
                 'requests': SimpleNamespace(post=post), 'SearchResult': lambda **item: item,
                 'include_user_info_headers': lambda headers, user: headers,
                 'FORWARD_SESSION_INFO_HEADER_CHAT_ID': 'X-OpenWebUI-Chat-Id',
                 'get_filtered_results': lambda records, domains: [record for record in records if 'allowed.example' in record['link']],
                 'log': logging.getLogger(__name__)}
    module = ast.Module(body=[definition], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['search_external']


def test_real_hook_posts_query_count_and_bearer_key():
    records = [{'link': f'https://allowed.example/{index}', 'title': str(index), 'snippet': 'Fixture'} for index in range(5)]
    calls = []
    def post(url, **arguments):
        calls.append((url, arguments))
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: records)
    provider = external_provider(post)
    result = provider(SimpleNamespace(state=SimpleNamespace()), 'https://provider.example/search', 'fixture-key', 'query', 5)
    assert result == records
    assert calls[0][1]['json'] == {'query': 'query', 'count': 5}
    assert calls[0][1]['headers']['Authorization'] == 'Bearer fixture-key'


def test_real_hook_preserves_empty_results():
    provider = external_provider(lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: []))
    assert provider(SimpleNamespace(state=SimpleNamespace()), 'https://provider.example/search', 'key', 'query', 5) == []


def test_real_hook_applies_host_domain_filter_and_requested_count():
    records = [{'link': 'https://blocked.example/', 'title': 'Excluded', 'snippet': 'Fixture'},
               {'link': 'https://allowed.example/one', 'title': 'One', 'snippet': 'Fixture'},
               {'link': 'https://allowed.example/two', 'title': 'Two', 'snippet': 'Fixture'}]
    provider = external_provider(lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: records))
    assert provider(SimpleNamespace(state=SimpleNamespace()), 'https://provider.example/search', 'key', 'query', 1, ['allowed.example']) == records[1:2]


def test_real_hook_maps_http_failure_to_empty_results():
    def reject():
        raise RuntimeError('Synthetic upstream failure')
    provider = external_provider(lambda *args, **kwargs: SimpleNamespace(raise_for_status=reject))
    assert provider(SimpleNamespace(state=SimpleNamespace()), 'https://provider.example/search', 'key', 'query', 5) == []
