"""Run real Chromium checks against an isolated mock database; requires Playwright/Node."""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--artifacts', help='Optional screenshot directory; contains generated test data only')
    args = parser.parse_args()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix='gapyear-browser-') as folder:
        env = dict(os.environ, APP_ENV='development', APP_BASE_URL='',
                   APP_DATA_DIR=str(Path(folder) / 'unused-normal'), APP_MAIL_MODE='mock',
                   APP_AI_MODE='mock', ENABLE_SCHEDULER='false', GAPYEAR_BROWSER_DIR=folder,
                   GAPYEAR_BROWSER_PASSWORD=secrets.token_urlsafe(32),
                   GAPYEAR_BROWSER_URL=f'http://127.0.0.1:{port}', GAPYEAR_BROWSER_PORT=str(port))
        if args.artifacts:
            Path(args.artifacts).mkdir(parents=True, exist_ok=True)
            env['GAPYEAR_BROWSER_ARTIFACTS'] = str(Path(args.artifacts).resolve())
        code = '''import os,uvicorn
from app.demo import Demo
from app.main import create_app
d=Demo(os.environ['GAPYEAR_BROWSER_DIR'])
d.auth.create_user('browser-reviewer',os.environ['GAPYEAR_BROWSER_PASSWORD'],'admin')
uvicorn.run(create_app(service=d.service,auth=d.auth,demo=d),host='127.0.0.1',port=int(os.environ['GAPYEAR_BROWSER_PORT']),log_level='error',access_log=False)
'''
        server = subprocess.Popen([sys.executable, '-c', code], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError(server.stderr.read().decode())
                try:
                    with urllib.request.urlopen(env['GAPYEAR_BROWSER_URL'] + '/health', timeout=1):
                        break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Isolated browser test server did not start')
            result = subprocess.run(['node', str(ROOT / 'tests/browser_staff.cjs')], cwd=ROOT, env=env, timeout=180)
            return result.returncode
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    raise SystemExit(main())
