import json
import io
import app


# A trimmed but structurally-faithful simplestreams products document.
_DOC = {
    'format': 'products:1.0',
    'products': {
        'alpine:3.24:amd64:default': {
            'os': 'Alpine', 'release': '3.24', 'release_title': '3.24',
            'arch': 'amd64', 'variant': 'default',
            'aliases': 'alpine/3.24/default,alpine/3.24',
            'versions': {
                '20260101_00': {'items': {'lxd.tar.xz': {}, 'rootfs.squashfs': {}}},
                '20260201_00': {'items': {'lxd.tar.xz': {}, 'rootfs.squashfs': {},
                                          'disk.qcow2': {}}},  # newer version adds VM
            },
        },
        'ubuntu:22.04:amd64:default': {
            'os': 'Ubuntu', 'release': '22.04', 'arch': 'amd64', 'variant': 'default',
            'aliases': 'ubuntu/22.04',
            'versions': {'20260101_00': {'items': {'lxd.tar.xz': {}, 'rootfs.squashfs': {}}}},
        },
        'noimages:1:amd64:default': {  # no usable items → dropped
            'os': 'Nothing', 'release': '1', 'arch': 'amd64', 'variant': 'default',
            'aliases': '', 'versions': {'v': {'items': {'meta.tar.xz': {}}}},
        },
    },
}


def test_simplestreams_parse(monkeypatch):
    def fake_urlopen(req, timeout=0):
        return io.BytesIO(json.dumps(_DOC).encode())
    monkeypatch.setattr(app.urllib.request, 'urlopen', fake_urlopen)
    app._ss_cache.clear()

    out = app._simplestreams_products('https://example.test')
    by_alias = {p['alias']: p for p in out}

    # Product with no container/VM items is dropped.
    assert 'noimages' not in by_alias
    assert len(out) == 2

    alp = by_alias['alpine/3.24/default']
    # Latest version has both squashfs and qcow2 → both types detected.
    assert set(alp['types']) == {'container', 'virtual-machine'}
    assert alp['aliases'] == ['alpine/3.24/default', 'alpine/3.24']
    assert alp['os'] == 'Alpine'

    ub = by_alias['ubuntu/22.04']
    assert ub['types'] == ['container']


def test_ubuntu_index_layout(monkeypatch):
    """cloud-images.ubuntu.com has no flat images.json — products live in a
    download.json reached via index.json (datatype 'image-downloads'). The
    parser must follow that and prefer the numeric `version` for display."""
    import urllib.error
    index_doc = {'index': {
        'com.ubuntu.cloud:released:aws': {'datatype': 'image-ids', 'format': 'products:1.0',
                                          'path': 'streams/v1/aws.json'},
        'com.ubuntu.cloud:released:download': {'datatype': 'image-downloads', 'format': 'products:1.0',
                                               'path': 'streams/v1/download.json'},
    }}
    download_doc = {'products': {
        'com.ubuntu.cloud:server:24.04:amd64': {
            'os': 'ubuntu', 'release': 'noble', 'version': '24.04', 'arch': 'amd64',
            'aliases': '24.04,n,noble',
            'versions': {'20260101': {'items': {'squashfs': {}, 'lxd.tar.xz': {}, 'disk-kvm.img': {}}}},
        },
    }}

    def fake_get_json(url):
        if url.endswith('/streams/v1/images.json'):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        if url.endswith('/streams/v1/index.json'):
            return index_doc
        if url.endswith('download.json'):
            return download_doc
        raise AssertionError('unexpected url ' + url)

    monkeypatch.setattr(app, '_get_json', fake_get_json)
    app._ss_cache.clear()
    out = app._simplestreams_products('https://cloud-images.ubuntu.com/releases')
    assert len(out) == 1
    p = out[0]
    assert p['os'] == 'ubuntu'
    assert p['release'] == '24.04'          # numeric version preferred over codename
    assert p['variant'] == 'server'          # derived from the product key
    assert p['alias'] == '24.04'
    assert set(p['types']) == {'container', 'virtual-machine'}


def test_simplestreams_cache(monkeypatch):
    calls = {'n': 0}
    def fake_urlopen(req, timeout=0):
        calls['n'] += 1
        return io.BytesIO(json.dumps(_DOC).encode())
    monkeypatch.setattr(app.urllib.request, 'urlopen', fake_urlopen)
    app._ss_cache.clear()
    app._simplestreams_products('https://cache.test')
    app._simplestreams_products('https://cache.test')
    assert calls['n'] == 1  # second call served from cache
