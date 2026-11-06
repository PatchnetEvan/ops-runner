#!/usr/bin/env python3
"""A git credential helper for the runner's GitHub App.

On each push, git asks it for a password. It signs a short JWT with the App's
private key and swaps it for an installation token that lasts an hour and
covers one repository, with contents:write only. Nothing is cached or written.

In the runner's git config (OPS_GIT_CONFIG):
    [credential "https://github.com"]
        helper = !python3 /opt/ops-runner/gh_app_credential.py

Environment (from /etc/ops-runner/env):
    OPS_GH_APP_ID           the App ID
    OPS_GH_INSTALLATION_ID  the installation ID on the repository
    OPS_GH_APP_KEY          path to the App's private key (.pem), readable only
                            by the runner's user
    OPS_GH_REPO             the repository name the token is limited to
                            (e.g. chamber-impact-networking)
    OPS_GH_API              default https://api.github.com (tests point it elsewhere)

Signing uses the openssl command; everything else is the standard library.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def app_jwt(app_id, key_path, now=None):
    now = int(now or time.time())
    header = b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    # iat a minute back for clock drift; GitHub allows at most 10 minutes.
    claims = b64url(json.dumps({'iat': now - 60, 'exp': now + 540, 'iss': str(app_id)},
                               separators=(',', ':')).encode())
    signing_input = f'{header}.{claims}'.encode()
    p = subprocess.run(['openssl', 'dgst', '-sha256', '-sign', key_path], input=signing_input,
                       capture_output=True, check=True)
    return f'{header}.{claims}.{b64url(p.stdout)}'


def installation_token(env=os.environ):
    api = env.get('OPS_GH_API', 'https://api.github.com').rstrip('/')
    jwt = app_jwt(env['OPS_GH_APP_ID'], env['OPS_GH_APP_KEY'])
    body = {'permissions': {'contents': 'write'}}
    if env.get('OPS_GH_REPO'):
        body['repositories'] = [env['OPS_GH_REPO']]
    req = urllib.request.Request(
        f"{api}/app/installations/{env['OPS_GH_INSTALLATION_ID']}/access_tokens",
        data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': f'Bearer {jwt}', 'Accept': 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'ops-runner'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)['token']


def main(argv, stdin=sys.stdin, stdout=sys.stdout):
    if len(argv) < 2 or argv[1] != 'get':
        return 0  # store / erase: nothing is kept
    attrs = dict(line.split('=', 1) for line in stdin.read().splitlines() if '=' in line)
    if attrs.get('protocol') != 'https' or attrs.get('host') != 'github.com':
        return 0  # not ours: say nothing, git tries the next helper or fails
    try:
        token = installation_token()
    except Exception as e:  # never print the key or the JWT
        print(f'ops-runner credential helper: could not get a token ({type(e).__name__})', file=sys.stderr)
        return 1
    stdout.write(f'username=x-access-token\npassword={token}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
