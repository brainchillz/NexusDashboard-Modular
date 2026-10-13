"""NFS: /etc/exports.d drop-ins are read, and a rewrite keeps `-fsid=`.

Real case from node2 after its rebuild: every export lived in
/etc/exports.d/node2.exports (restored there by hand, fsids carried over
verbatim) and the dashboard said "No exports configured" — it only ever
read /etc/exports, which is also where a create went, so the page then
listed one export while exportfs served eight.
"""
import pytest

import app
from nexusdash.modules import nfs

DROPIN = (
    "# node2 NFS exports. Restored from the pre-rebuild capture.\n"
    "# fsid= values are carried over verbatim -- changing one gives clients ESTALE.\n"
    "\n"
    "/volume01/share -fsid=ffa63abd-c548-4c35-b76a-9ad69ad9c207 *(rw,sync,no_subtree_check)\n"
    "/volume01/music -fsid=4b7d4dc5-c4ed-4c9a-8ce8-3cd3e3ca5018 *(rw,sync,no_subtree_check) # Music\n"
    "/ALLFLASH/ESXI-VM -fsid=a12af9f7-6b9c-49fc-8ff5-10f72f4e9b47 *(rw,sync,no_subtree_check,no_root_squash)\n"
)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    main = tmp_path / 'exports'
    main.write_text('# header only\n')
    d = tmp_path / 'exports.d'
    d.mkdir()
    (d / 'node2.exports').write_text(DROPIN)
    (d / 'zfs.exports.lock').write_text('')          # zfs's lock file: not an exports file
    (d / 'README').write_text('/not/an/export *(rw)\n')  # wrong suffix: exportfs ignores it
    monkeypatch.setattr(app, 'EXPORTS_FILE', str(main))
    monkeypatch.setattr(app, 'EXPORTS_DIR', str(d))
    return main, d


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app, '_resolve_identity', lambda: ('tester', 'admin'))
    monkeypatch.setattr(app, 'load_disabled_modules', lambda: set())
    app.app.config['TESTING'] = True
    return app.app.test_client()


def test_dropins_are_listed_with_their_source(tree):
    main, d = tree
    exports = nfs.parse_exports()
    assert [e['path'] for e in exports] == ['/volume01/share', '/volume01/music', '/ALLFLASH/ESXI-VM']
    assert {e['source'] for e in exports} == {str(d / 'node2.exports')}
    assert not any(e['editable'] for e in exports)
    share = exports[0]
    assert share['defaults'] == 'fsid=ffa63abd-c548-4c35-b76a-9ad69ad9c207'
    assert share['clients'] == [{'host': '*', 'options': 'rw,sync,no_subtree_check'}]
    assert exports[1]['comment'] == 'Music'
    assert exports[1]['clients'][0]['options'] == 'rw,sync,no_subtree_check'   # the '# Music' did not leak in


def test_main_file_rows_are_editable_and_come_first(tree):
    main, d = tree
    main.write_text('/srv/a 10.0.0.5(rw,sync)\n/srv/bare 10.0.0.6\n')
    exports = nfs.parse_exports()
    assert exports[0]['path'] == '/srv/a' and exports[0]['editable'] and exports[0]['source'] == str(main)
    assert exports[1]['clients'] == [{'host': '10.0.0.6', 'options': ''}]   # bare host no longer vanishes
    assert exports[2]['path'] == '/volume01/share'


def test_one_file_signature_reads_only_that_file(tree):
    main, d = tree
    assert nfs.parse_exports(str(d / 'node2.exports')) and nfs.parse_exports(str(main)) == []


def test_unreadable_dropin_is_reported_not_hidden(tree, monkeypatch):
    main, d = tree
    real_open = open
    def deny(path, *a, **k):
        if str(path).endswith('node2.exports'):
            raise PermissionError(path)
        return real_open(path, *a, **k)
    monkeypatch.setattr('builtins.open', deny)
    rows = nfs.parse_exports()
    assert len(rows) == 1 and rows[0]['path'] is None and 'not readable' in rows[0]['error']


def test_create_refuses_a_path_a_dropin_already_exports(tree, client, monkeypatch):
    monkeypatch.setattr(app, 'run_safe', lambda *a, **k: pytest.fail('must not write'))
    r = client.post('/api/nfs/exports', json={'path': '/volume01/share', 'clients': []})
    assert r.status_code == 400
    assert 'node2.exports' in r.get_json()['error'] and 'move' in r.get_json()['error']


def test_delete_refuses_a_dropin_export(tree, client, monkeypatch):
    monkeypatch.setattr(app, 'run_safe', lambda *a, **k: pytest.fail('must not write'))
    r = client.delete('/api/nfs/exports/volume01/share')
    assert r.status_code == 400
    assert 'node2.exports' in r.get_json()['error']


def test_create_still_appends_to_the_main_file(tree, client, monkeypatch):
    main, d = tree
    calls = []
    monkeypatch.setattr(app, 'run_safe', lambda argv, **k: calls.append((argv, k.get('input_data'))) or {'success': True})
    r = client.post('/api/nfs/exports', json={'path': '/srv/new', 'clients': [{'host': '10.0.0.0/24', 'options': 'rw,sync'}]})
    assert r.status_code == 200
    argv, body = calls[0]
    assert argv == ['tee', str(main)]
    assert body == '# header only\n/srv/new\t10.0.0.0/24(rw,sync)\n'
    assert (d / 'node2.exports').read_text() == DROPIN               # untouched


def test_edit_keeps_fsid_and_comment(tree, client, monkeypatch):
    """After the drop-in lines are moved into /etc/exports, an Edit from the
    page must not strip `-fsid=` (every mounted client would get ESTALE)."""
    main, d = tree
    main.write_text(DROPIN)
    (d / 'node2.exports').unlink()
    calls = []
    monkeypatch.setattr(app, 'run_safe', lambda argv, **k: calls.append((argv, k.get('input_data'))) or {'success': True})
    r = client.post('/api/nfs/exports', json={'path': '/volume01/music',
                                              'clients': [{'host': '192.168.10.0/24', 'options': 'rw,sync,no_subtree_check'}]})
    assert r.status_code == 200
    body = calls[0][1]
    assert '/volume01/music\t-fsid=4b7d4dc5-c4ed-4c9a-8ce8-3cd3e3ca5018 192.168.10.0/24(rw,sync,no_subtree_check)  # Music\n' in body
    assert body.count('/volume01/music') == 1
    assert '/volume01/share -fsid=ffa63abd' in body                 # neighbours verbatim
    assert nfs._parse_export_line(body.splitlines()[4])['defaults'] == 'fsid=4b7d4dc5-c4ed-4c9a-8ce8-3cd3e3ca5018'


def test_summary_counts_dropin_exports(tree, monkeypatch):
    from nexusdash.core import summary
    assert len(summary.parse_exports()) == 3
