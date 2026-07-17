"""dnsmasq module — pure renderers, validators, probe packets, hosts import,
store round-trip, and Nexus registration/gate wiring."""
import os
import copy
import struct
import shutil
import socket

import pytest

import app
from nexusdash.modules import dnsmasq as dm


def _settings(**kw):
    s = copy.deepcopy(dm.DEFAULTS['settings'])
    s.update(kw)
    return s


# ─── Renderers (pure; no dnsmasq binary needed) ───────────────────────

def test_render_main_defaults():
    text = dm.render_main(_settings())
    assert 'port=0' not in text
    assert 'domain=lan' in text and 'expand-hosts' in text
    assert 'server=1.1.1.1' in text and 'no-resolv' in text
    assert 'cache-size=1000' in text
    assert 'dhcp-leasefile=' in text
    assert 'listen-address=127.0.0.1' not in text   # no listen restriction set


def test_render_main_dns_disabled():
    assert 'port=0' in dm.render_main(_settings(dns_enabled=False))


def test_render_main_interface_pins_loopback():
    text = dm.render_main(_settings(interfaces=['eth0']))
    assert 'interface=eth0' in text and 'listen-address=127.0.0.1' in text


def test_render_dhcp_disabled_empty():
    dhcp = {'ranges': [{'start': '10.0.0.100', 'end': '10.0.0.199', 'tag': '',
                        'netmask': '', 'lease': '12h', 'enabled': True}]}
    assert 'dhcp-range' not in dm.render_dhcp(dhcp, _settings(dhcp_enabled=False))


def test_render_dhcp_range_and_boot():
    dhcp = {'ranges': [{'start': '10.0.0.100', 'end': '10.0.0.199', 'tag': 'lan',
                        'interface': '', 'netmask': '255.255.255.0', 'lease': '12h',
                        'enabled': True}],
            'boot': {'filename': 'ipxe.efi', 'server': '10.0.0.5'}}
    text = dm.render_dhcp(dhcp, _settings(dhcp_enabled=True))
    assert 'dhcp-range=set:lan,10.0.0.100,10.0.0.199,255.255.255.0,12h' in text
    assert 'dhcp-authoritative' in text
    assert 'dhcp-boot=ipxe.efi,,10.0.0.5' in text          # external boot server
    assert 'dhcp-hostsfile=' in text and 'dhcp-optsfile=' in text


def test_render_dhcp_boot_no_server():
    dhcp = {'ranges': [], 'boot': {'filename': 'pxelinux.0', 'server': ''}}
    assert 'dhcp-boot=pxelinux.0\n' in dm.render_dhcp(dhcp, _settings(dhcp_enabled=True))


def test_render_hosts_a_aaaa_disabled():
    dns = {'hosts': [
        {'id': 'h_1', 'name': 'nas.lan', 'a': '10.0.0.5', 'aaaa': 'fd00::5', 'enabled': True},
        {'id': 'h_2', 'name': 'off.lan', 'a': '10.0.0.6', 'aaaa': '', 'enabled': False}]}
    text = dm.render_hosts(dns)
    assert '10.0.0.5 nas.lan' in text and 'fd00::5 nas.lan' in text
    assert '# disabled: 10.0.0.6 off.lan' in text
    assert '\n10.0.0.6 off.lan' not in text


def test_render_dns_records():
    dns = {'addresses': [{'domain': 'ads.com', 'ip': '0.0.0.0', 'enabled': True}],
           'cnames': [{'alias': 'www.lan', 'target': 'nas.lan', 'enabled': True}],
           'forwards': [{'domain': 'corp', 'upstream': '10.1.1.1', 'enabled': True}]}
    text = dm.render_dns(dns)
    assert 'address=/ads.com/0.0.0.0' in text
    assert 'cname=www.lan,nas.lan' in text
    assert 'server=/corp/10.1.1.1' in text


# ─── Validators ───────────────────────────────────────────────────────

def test_dns_validate():
    assert dm._dns_validate('hosts', {'name': 'nas.lan', 'a': '10.0.0.5'})[1] is None
    assert dm._dns_validate('hosts', {'name': 'bad name!', 'a': '1.2.3.4'})[1]
    assert dm._dns_validate('hosts', {'name': 'x.lan'})[1]              # no A/AAAA
    # newline / directive smuggling blocked
    assert dm._dns_validate('addresses', {'domain': 'a.com\nport=0', 'ip': '0.0.0.0'})[1]


def test_dhcp_validate():
    ok = dm._dhcp_validate('ranges', {'start': '10.0.0.1', 'end': '10.0.0.9',
                                      'netmask': '255.255.255.0', 'lease': '12h'})
    assert ok[1] is None
    assert dm._dhcp_validate('ranges', {'start': '10.0.0.9', 'end': '10.0.0.1'})[1]   # end<start
    assert dm._dhcp_validate('ranges', {'start': '10.0.0.1', 'end': '10.0.0.9',
                                        'tag': 'a', 'interface': 'eth0'})[1]           # both
    assert dm._dhcp_validate('static_leases', {'mac': 'nope', 'ip': '10.0.0.1'})[1]


# ─── Hosts import + probe packets ─────────────────────────────────────

def test_parse_hosts_text():
    text = "127.0.0.1 localhost\n10.0.0.5 nas nas.lan\nbad-line\n"
    entries, skipped, invalid = dm.parse_hosts_text(text)
    assert ('nas', 'a', '10.0.0.5') in entries and ('nas.lan', 'a', '10.0.0.5') in entries
    assert skipped == 1 and invalid == 1


def test_probe_discover_and_offer_roundtrip():
    pkt = dm.build_discover(0x1234, b'\x02\x00\xaa\xbb\xcc\xdd')
    assert len(pkt) >= 300 and pkt[0] == 1
    assert struct.unpack('!I', pkt[4:8])[0] == 0x1234
    # craft an OFFER for that xid
    off = struct.pack('!BBBBIHH', 2, 1, 6, 0, 0x1234, 0, 0)
    off += socket.inet_aton('0.0.0.0') + socket.inet_aton('10.0.0.50') + b'\x00' * 8
    off += b'\x00' * 208 + b'\x63\x82\x53\x63'
    off += bytes([53, 1, 2]) + bytes([54, 4]) + socket.inet_aton('10.0.0.1') + bytes([255])
    got = dm.parse_offer(off, 0x1234)
    assert got == {'offer_ip': '10.0.0.50', 'server_id': '10.0.0.1'}
    assert dm.parse_offer(off, 0x9999) is None      # wrong xid


# ─── Store round-trip (temp state dir) ────────────────────────────────

def test_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, 'STATE_DIR', str(tmp_path))
    d = dm.load_store('dns')
    d['hosts'].append({'id': dm.new_id('h'), 'name': 'x.lan', 'a': '10.0.0.9',
                       'aaaa': '', 'enabled': True, 'comment': ''})
    dm.save_store('dns', d)
    assert dm.load_store('dns')['hosts'][0]['name'] == 'x.lan'
    assert dm.bump_serial('dns', dm.load_store('dns')) == 1


# ─── Nexus registration / wiring ──────────────────────────────────────

def test_module_registered_and_default_off():
    assert 'dnsmasq' in app.MODULE_IDS
    m = next(x for x in app.MODULES if x['id'] == 'dnsmasq')
    assert m['category'] == 'DNS' and m['label'] == 'DNS & DHCP'
    assert dm.MODULE.get('default_enabled') is False
    # blueprint endpoint maps back to the module for the disabled-gate
    assert app.module_for_endpoint('dnsmasq.route_status') == 'dnsmasq'


def test_service_and_history_wired():
    assert app.SYSTEM_SERVICES['dnsmasq']['service'] == 'dnsmasq'
    assert app.SYSTEM_SERVICES['dnsmasq']['alert'] is False
    assert {'dns_hits', 'dns_misses', 'dns_cache_size', 'dhcp_leases'} <= app.HISTORY_METRICS


def test_hooks_callable_and_cheap(tmp_path, monkeypatch):
    monkeypatch.setattr(dm, 'STATE_DIR', str(tmp_path))
    monkeypatch.setattr(dm, 'run', lambda *a, **k: ('inactive', '', 3))  # is-active → down
    s = dm.summary()
    assert set(s) >= {'installed', 'active', 'dns_enabled', 'dhcp_enabled', 'leases', 'hosts'}
    # alert suppressed when the drop-in isn't present (node not wired as a DNS server)
    monkeypatch.setattr(dm.os.path, 'exists', lambda p: False)
    assert dm.alerts() == []


# ─── Real dnsmasq (gated on the binary) ───────────────────────────────

@pytest.mark.skipif(not shutil.which('dnsmasq'), reason='dnsmasq not installed')
def test_validate_render_real_dnsmasq():
    stores = {n: copy.deepcopy(dm.DEFAULTS[n]) for n in dm.STORE_NAMES}
    stores['settings']['dhcp_enabled'] = True
    stores['dhcp']['ranges'] = [{'start': '10.0.0.100', 'end': '10.0.0.199', 'tag': 'lan',
                                 'interface': '', 'netmask': '255.255.255.0', 'lease': '12h',
                                 'enabled': True}]
    ok, output = dm.validate_render(dm.render_all(stores))
    assert ok, output


@pytest.mark.skipif(not shutil.which('dnsmasq'), reason='dnsmasq not installed')
def test_validate_render_rejects_garbage():
    stores = {n: copy.deepcopy(dm.DEFAULTS[n]) for n in dm.STORE_NAMES}
    stores['settings']['extra_options'] = 'not-a-real-option=1'
    ok, output = dm.validate_render(dm.render_all(stores))
    assert not ok and 'bad option' in output
