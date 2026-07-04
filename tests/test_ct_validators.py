import app


def test_valid_instance_name():
    assert app.valid_instance_name('web-01')
    assert app.valid_instance_name('a')
    assert app.valid_instance_name('ubuntu-vm-2')
    # Must start with a letter, no trailing hyphen, no leading digit, no dots.
    assert not app.valid_instance_name('1bad')
    assert not app.valid_instance_name('trail-')
    assert not app.valid_instance_name('has.dot')
    assert not app.valid_instance_name('has space')
    assert not app.valid_instance_name('')
    assert not app.valid_instance_name('a' * 64)          # 64 > 63 limit
    assert not app.valid_instance_name('under_score')     # LXD disallows underscores


def test_name_regexes_anchor_newlines():
    # \Z anchoring means a trailing newline cannot sneak past a name check.
    assert not app.RE_INSTANCE.match('web\n')
    assert not app.RE_CT_NETWORK.match('br0\n')
    assert not app.RE_CT_POOL.match('pool\n')
    assert app.RE_CT_NETWORK.match('lxdbr0')
    assert app.RE_CT_POOL.match('default')


def test_image_alias_regex():
    assert app.RE_IMAGE_ALIAS.match('alpine/3.24/default')
    assert app.RE_IMAGE_ALIAS.match('ubuntu/22.04')
    assert not app.RE_IMAGE_ALIAS.match('bad alias')
    assert not app.RE_IMAGE_ALIAS.match('a;rm -rf')


def test_instance_types_and_actions():
    assert 'container' in app.INSTANCE_TYPES
    assert 'virtual-machine' in app.INSTANCE_TYPES
    assert app.STATE_ACTIONS == {'start', 'stop', 'restart', 'freeze', 'unfreeze'}
