import app


def test_proxy_addr_regex():
    ok = ['tcp:0.0.0.0:80', 'tcp:127.0.0.1:443', 'udp:0.0.0.0:53',
          'tcp:[::]:8080', 'tcp:192.168.1.5:22']
    for a in ok:
        assert app.RE_PROXY_ADDR.match(a), a
    bad = ['notaddr', 'http:0.0.0.0:80', 'tcp:0.0.0.0', '0.0.0.0:80',
           'tcp:0.0.0.0:80\n', 'tcp:0.0.0.0:99999x']
    for a in bad:
        assert not app.RE_PROXY_ADDR.match(a), a


def test_snapname_regex():
    assert app.RE_SNAPNAME.match('snap-1')
    assert app.RE_SNAPNAME.match('export-20260704-120000')
    assert not app.RE_SNAPNAME.match('-leading')
    assert not app.RE_SNAPNAME.match('has space')
    assert not app.RE_SNAPNAME.match('trailing\n')


def test_fingerprint_regex():
    assert app.RE_FINGERPRINT.match('1dff57f65a2f')
    assert app.RE_FINGERPRINT.match('a' * 64)
    assert not app.RE_FINGERPRINT.match('XYZ')        # non-hex
    assert not app.RE_FINGERPRINT.match('abc\n')


def test_config_edit_allowlist():
    # Only safe keys are editable; arbitrary keys must be ignored by the filter.
    assert 'limits.cpu' in app.CONFIG_EDIT_KEYS
    assert 'limits.memory' in app.CONFIG_EDIT_KEYS
    assert 'raw.lxc' not in app.CONFIG_EDIT_KEYS
    assert 'security.privileged' in app.CONFIG_EDIT_KEYS


def test_portforward_is_a_module():
    assert 'portforward' in app.MODULE_IDS
