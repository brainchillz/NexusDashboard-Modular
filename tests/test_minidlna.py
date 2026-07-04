"""MiniDLNA / ReadyMedia module tests — the pure logic behind the config
round-trip. These guard config-file injection (a newline into /etc/minidlna.conf
would inject arbitrary directives), the media_dir type-prefix parsing, and the
merge that preserves unmanaged keys on rewrite. No root/hardware needed.
"""
import os
import app


# ─── Registration ────────────────────────────────────────────────────

def test_minidlna_registered_as_service_no_alerts():
    svc = app.SYSTEM_SERVICES.get('minidlna')
    assert svc and svc['service'] == 'minidlna'
    assert svc.get('alert') is False          # a stopped media server isn't an emergency
    assert svc.get('pkg') == 'minidlna'
    # The service key must be a real module id so the summary can filter it.
    assert 'minidlna' in app.MODULE_IDS


def test_minidlna_is_a_toggleable_sharing_module():
    m = [x for x in app.MODULES if x['id'] == 'minidlna']
    assert m and m[0]['category'] == 'Sharing'


# ─── media_dir prefix parsing ────────────────────────────────────────

def test_split_media_dir_with_type_prefix():
    assert app._split_media_dir('V,/volume01/video') == ('V', '/volume01/video')
    assert app._split_media_dir('A,/srv/music') == ('A', '/srv/music')
    assert app._split_media_dir('P,/srv/photos') == ('P', '/srv/photos')


def test_split_media_dir_without_prefix():
    assert app._split_media_dir('/srv/media') == ('', '/srv/media')


def test_split_media_dir_comma_in_path_not_a_prefix():
    # A leading token that isn't a combination of A/V/P is part of the path.
    assert app._split_media_dir('/srv/a,b') == ('', '/srv/a,b')


# ─── parse / render round-trip ───────────────────────────────────────

def test_parse_render_round_trip(tmp_path, monkeypatch):
    conf = tmp_path / 'minidlna.conf'
    conf.write_text(
        '# a comment\n\n'
        'port=8200\n'
        'media_dir=V,/volume01/video\n'
        'media_dir=A,/volume01/music\n'
        'friendly_name=MediaBox\n'
        'inotify=yes\n'
        'log_dir=/var/log/minidlna\n'
    )
    monkeypatch.setattr(app, 'MINIDLNA_CONF', str(conf))
    pairs = app.minidlna_parse()
    # comments/blanks dropped; keys lowercased; media_dir repeats preserved in order
    assert ('port', '8200') in pairs
    assert ('media_dir', 'V,/volume01/video') in pairs
    assert ('media_dir', 'A,/volume01/music') in pairs
    assert ('log_dir', '/var/log/minidlna') in pairs
    # render is a stable flat key=value dump
    out = app.minidlna_render(pairs)
    assert 'friendly_name=MediaBox' in out
    assert out.count('media_dir=') == 2


def test_view_shape(tmp_path, monkeypatch):
    pairs = [('port', '8200'), ('media_dir', 'V,/srv/video'),
             ('friendly_name', 'Nexus'), ('inotify', 'no'), ('serial', '123')]
    view = app._minidlna_view(pairs)
    assert view['port'] == '8200'
    assert view['friendly_name'] == 'Nexus'
    assert view['inotify'] == 'no'
    assert view['media_dirs'] == [{'type': 'V', 'path': '/srv/video'}]


# ─── build (validation + merge) ──────────────────────────────────────

def _existing(monkeypatch, pairs):
    monkeypatch.setattr(app, 'minidlna_parse', lambda: list(pairs))


def _dirs_exist(monkeypatch, ok=True):
    monkeypatch.setattr(os.path, 'isdir', lambda p: ok)


def test_build_valid_preserves_unmanaged_keys(monkeypatch):
    _existing(monkeypatch, [('friendly_name', 'Old'),
                            ('log_dir', '/var/log/minidlna'),
                            ('media_dir', '/old'),
                            ('serial', '12345678')])
    _dirs_exist(monkeypatch)
    pairs, error = app._minidlna_build({
        'friendly_name': 'Nexus Media', 'port': '8200', 'inotify': True,
        'media_dirs': [{'type': 'V', 'path': '/srv/video'},
                       {'type': '', 'path': '/srv/mixed'}],
    })
    assert error is None
    d = dict(pairs)
    assert d['friendly_name'] == 'Nexus Media'       # replaced
    assert d['port'] == '8200'                        # added
    assert d['inotify'] == 'yes'
    assert d['log_dir'] == '/var/log/minidlna'        # unmanaged key preserved
    assert d['serial'] == '12345678'                  # unmanaged key preserved
    media = [v for k, v in pairs if k == 'media_dir']
    assert media == ['V,/srv/video', '/srv/mixed']    # old /old replaced; prefix kept


def test_build_rejects_newline_in_friendly_name(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    _, error = app._minidlna_build({
        'friendly_name': 'Nexus\nport=1', 'media_dirs': [{'path': '/srv/v'}]})
    assert error and 'friendly name' in error.lower()


def test_build_rejects_bad_port(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    for bad in ('0', '70000', 'abc', '80 80'):
        _, error = app._minidlna_build({'port': bad, 'media_dirs': [{'path': '/srv/v'}]})
        assert error and 'port' in error.lower(), bad


def test_build_rejects_relative_media_dir(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    _, error = app._minidlna_build({'media_dirs': [{'path': 'srv/video'}]})
    assert error and 'media directory' in error.lower()


def test_build_rejects_newline_in_media_dir(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    _, error = app._minidlna_build({'media_dirs': [{'path': '/srv/v\nlog_dir=/etc'}]})
    assert error is not None


def test_build_rejects_bad_type_prefix(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    _, error = app._minidlna_build({'media_dirs': [{'type': 'X', 'path': '/srv/v'}]})
    assert error and 'type' in error.lower()


def test_build_rejects_nonexistent_media_dir(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch, ok=False)
    _, error = app._minidlna_build({'media_dirs': [{'path': '/does/not/exist'}]})
    assert error and 'exist' in error.lower()


def test_build_requires_at_least_one_media_dir(monkeypatch):
    _existing(monkeypatch, [])
    _dirs_exist(monkeypatch)
    _, error = app._minidlna_build({'friendly_name': 'Nexus', 'media_dirs': []})
    assert error and 'media directory' in error.lower()


# ─── db stats (helper output parsing) ────────────────────────────────

def _fake_stats_run(payload, rc=0):
    def _run(args, input_data=None, no_sudo=False):
        return payload, '', rc
    return _run


def test_db_stats_parses_helper_json(monkeypatch):
    monkeypatch.setattr(app, 'run', _fake_stats_run(
        '{"available": true, "path": "/var/cache/minidlna/files.db",'
        ' "size": 14909440, "objects": 10863, "audio": 0, "video": 10084, "image": 0}'))
    s = app._minidlna_db_stats()
    assert s['available'] is True
    assert s['video'] == 10084 and s['objects'] == 10863
    assert s['size'] == 14909440


def test_db_stats_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(app, 'run', _fake_stats_run('', rc=1))
    assert app._minidlna_db_stats() is None


def test_db_stats_none_on_bad_json(monkeypatch):
    monkeypatch.setattr(app, 'run', _fake_stats_run('not json at all'))
    assert app._minidlna_db_stats() is None


def test_db_stats_unavailable_flag_passes_through(monkeypatch):
    # A missing db is a valid, successful response (available=false), not an error.
    monkeypatch.setattr(app, 'run', _fake_stats_run(
        '{"available": false, "path": "/var/cache/minidlna/files.db"}'))
    s = app._minidlna_db_stats()
    assert s is not None and s['available'] is False
