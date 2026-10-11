import json
from unittest.mock import Mock

import pytest

import usync


def test_safe_name_scrubs_unsafe_characters():
    assert usync.safe_name('Course/Notes: Week 1') == 'Course_Notes_ Week 1'


def test_unique_destination_deduplicates_identical_content(tmp_path):
    folder = tmp_path / 'resources'
    first, write = usync.unique_destination(folder, 'notes.pdf', b'data')
    assert write
    first.write_bytes(b'data')
    second, write = usync.unique_destination(folder, 'notes.pdf', b'data')
    assert second == first
    assert not write
    third, write = usync.unique_destination(folder, 'notes.pdf', b'different')
    assert third != first
    assert write


def test_canvas_headers_never_go_to_external_download_host(monkeypatch):
    authenticated_session = Mock()
    authenticated_session.get.return_value = Mock(status_code=200, headers={})
    anonymous_download = Mock(return_value=Mock(status_code=200, headers={}))
    monkeypatch.setattr(usync.requests, 'get', anonymous_download)

    trusted = 'https://canvas.sydney.edu.au'
    assert usync.get_with_scoped_credentials(authenticated_session, trusted + '/api/v1/files/5', trusted, timeout=5) .status_code == 200
    assert usync.get_with_scoped_credentials(authenticated_session, 'https://files.example.org/files/5/download', trusted, timeout=5) .status_code == 200
    assert authenticated_session.get.call_count == 1
    anonymous_download.assert_called_once_with('https://files.example.org/files/5/download', timeout=5, allow_redirects=False)


def test_plaintext_http_download_is_blocked(monkeypatch):
    session = Mock()
    with pytest.raises(ValueError, match='non-HTTPS'):
        usync.get_with_scoped_credentials(session, 'http://files.example.org/notes.pdf', 'https://edstem.org')
    session.get.assert_not_called()


def test_gpt_ready_archive_omits_redundant_text_but_retains_original_json(tmp_path):
    dump = tmp_path / 'latest_dump'
    pages = dump / 'canvas' / 'pages'
    pages.mkdir(parents=True)
    (pages / 'page.json').write_text(json.dumps({'body': '<p>Example course content</p>'}))
    (pages / 'page.txt').write_text('Example course content')
    output = tmp_path / 'gpt_ready'
    manifest = usync.build_gpt_ready_tree(dump, output, 'DEMO101')
    assert (output / 'canvas' / 'pages' / 'page.json').exists()
    assert not (output / 'canvas' / 'pages' / 'page.txt').exists()
    assert manifest['included_files'] == 1
    assert manifest['excluded_files'] == 1


def test_zip_tree_creates_archive(tmp_path):
    content = tmp_path / 'DEMO101'
    content.mkdir()
    (content / 'example.txt').write_text('hello')
    destination = tmp_path / 'DEMO101.zip'
    usync.zip_tree(content, destination)
    assert destination.exists()
    import zipfile
    with zipfile.ZipFile(destination) as zf:
        assert zf.namelist() == ['DEMO101/example.txt']


def test_redirect_to_external_host_drops_private_session_headers(monkeypatch):
    session = Mock()
    session.get.return_value = Mock(status_code=302, headers={'Location': 'https://cdn.example.org/file.pdf'})
    anonymous_download = Mock(return_value=Mock(status_code=200, headers={}))
    monkeypatch.setattr(usync.requests, 'get', anonymous_download)

    response = usync.get_with_scoped_credentials(session, 'https://edstem.org/api/files/3', 'https://edstem.org')
    assert response.status_code == 200
    session.get.assert_called_once()
    anonymous_download.assert_called_once_with('https://cdn.example.org/file.pdf', allow_redirects=False)


def test_canvas_pagination_cannot_send_token_to_foreign_host():
    http = usync.Http({'Authorization': 'Bearer pretend-secret'})
    http.s = Mock()
    first_page = Mock(status_code=200, headers={}, links={'next': {'url': 'https://malicious.example.org/api/v1/courses'}},
                      json=Mock(return_value=[{'id': 5}]))
    http.s.get.return_value = first_page
    with pytest.raises(RuntimeError, match='cross-origin API pagination'):
        http.pages('https://canvas.sydney.edu.au/api/v1/courses')
    assert http.s.get.call_count == 1
