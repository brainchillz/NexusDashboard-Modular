"""Anonymous registry-v2 client: "is there a newer image for this tag?"

Pure stdlib. Used by the docker module's update checker and by halogen's
"newer release available" hint. Reads only: a bearer token with pull scope
(anonymous), then a HEAD on the tag's manifest for its content digest, or the
tags list. A registry that needs credentials, or is unreachable, yields an
error string — never an exception to the caller.

Docker records the digest of what it pulled under RepoDigests (for a
multi-arch tag that is the INDEX digest), and a HEAD with the index media
types in Accept returns that same index digest, so the comparison is exact:
same digest = up to date, different = the tag moved.
"""
import re
import json
import urllib.parse
import urllib.request
import urllib.error

DOCKER_HUB = 'docker.io'
HUB_REGISTRY = 'registry-1.docker.io'
HUB_AUTH = 'https://auth.docker.io/token?service=registry.docker.io&scope=repository:%s:pull'
KNOWN_AUTH = {'ghcr.io': 'https://ghcr.io/token?scope=repository:%s:pull'}
ACCEPT = ', '.join(['application/vnd.docker.distribution.manifest.list.v2+json',
                    'application/vnd.oci.image.index.v1+json',
                    'application/vnd.docker.distribution.manifest.v2+json',
                    'application/vnd.oci.image.manifest.v1+json'])
TIMEOUT = 8
RE_SEMVER = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$')


def parse_ref(ref):
    """'nginx' → ('docker.io','library/nginx','latest',None);
    'ghcr.io/o/n:1.2' → ('ghcr.io','o/n','1.2',None);
    'x@sha256:…' → digest pinned (tag None)."""
    ref = (ref or '').strip()
    digest = None
    if '@' in ref:
        ref, digest = ref.split('@', 1)
    parts = ref.split('/')
    if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        host, path = parts[0], '/'.join(parts[1:])
    else:
        host, path = DOCKER_HUB, ref
        if '/' not in path:
            path = 'library/' + path
    tag = None
    if ':' in path.rsplit('/', 1)[-1]:
        path, tag = path.rsplit(':', 1)
    if digest is None and tag is None:
        tag = 'latest'
    return host, path, tag, digest


def _get(url, headers=None, method='GET'):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, dict(r.headers), r.read()


def _token(host, repo):
    """Anonymous bearer token for pull scope, or None when the registry does
    not challenge (plain v2 endpoint)."""
    if host == DOCKER_HUB:
        url = HUB_AUTH % repo
    elif host in KNOWN_AUTH:
        url = KNOWN_AUTH[host] % repo
    else:
        try:
            _get('https://%s/v2/' % host)
            return None                                   # no challenge: open registry
        except urllib.error.HTTPError as e:
            if e.code != 401:
                raise
            chal = e.headers.get('WWW-Authenticate', '')
            m = dict(re.findall(r'(\w+)="([^"]*)"', chal))
            if 'realm' not in m:
                raise
            q = {'scope': 'repository:%s:pull' % repo}
            if m.get('service'):
                q['service'] = m['service']
            url = m['realm'] + '?' + urllib.parse.urlencode(q)
    _, _, body = _get(url)
    doc = json.loads(body)
    return doc.get('token') or doc.get('access_token')


def _registry_host(host):
    return HUB_REGISTRY if host == DOCKER_HUB else host


def remote_digest(host, repo, tag):
    """(digest, error) for the tag's current manifest/index."""
    try:
        headers = {'Accept': ACCEPT}
        tok = _token(host, repo)
        if tok:
            headers['Authorization'] = 'Bearer ' + tok
        _, h, _ = _get('https://%s/v2/%s/manifests/%s' % (_registry_host(host), repo, tag), headers, method='HEAD')
        hl = {k.lower(): v for k, v in h.items()}
        d = hl.get('docker-content-digest')
        return (d, None) if d else (None, 'registry returned no digest')
    except urllib.error.HTTPError as e:
        return None, 'HTTP %d from %s' % (e.code, host)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, 'reason', e))


def list_tags(host, repo):
    """(tags, error) — one page (100) is enough for a release stream."""
    try:
        headers = {}
        tok = _token(host, repo)
        if tok:
            headers['Authorization'] = 'Bearer ' + tok
        _, _, body = _get('https://%s/v2/%s/tags/list?n=100' % (_registry_host(host), repo), headers)
        return (json.loads(body).get('tags') or []), None
    except urllib.error.HTTPError as e:
        return [], 'HTTP %d from %s' % (e.code, host)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return [], str(getattr(e, 'reason', e))


def newest_semver(tags):
    """Highest X.Y.Z (optionally v-prefixed) tag, or None. Pure."""
    best = None
    for t in tags or []:
        m = RE_SEMVER.match(t)
        if m:
            key = tuple(int(x) for x in m.groups())
            if best is None or key > best[0]:
                best = (key, t)
    return best[1] if best else None


def semver_newer(a, b):
    """True when tag a is a higher X.Y.Z than tag b. Pure."""
    ma, mb = RE_SEMVER.match(a or ''), RE_SEMVER.match(b or '')
    return bool(ma and mb and tuple(map(int, ma.groups())) > tuple(map(int, mb.groups())))


def compare(local_repo_digests, remote):
    """'current' | 'update' | 'unknown' from the image's RepoDigests and the
    registry's digest for the tag. Pure."""
    if not remote:
        return 'unknown'
    locals_ = {d.split('@', 1)[1] for d in (local_repo_digests or []) if '@' in d}
    if not locals_:
        return 'unknown'                                   # locally built / loaded image
    return 'current' if remote in locals_ else 'update'
