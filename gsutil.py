#!/usr/bin/env python3
# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Run a pinned gsutil."""

from __future__ import print_function

import argparse
import base64
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
  import urllib2 as urllib
except ImportError:  # For Py3 compatibility
  import urllib.request as urllib

import zipfile


GSUTIL_URL = 'https://storage.googleapis.com/pub/'
API_URL = 'https://www.googleapis.com/storage/v1/b/pub/o/'

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BIN_DIR = os.path.join(THIS_DIR, 'external_bin', 'gsutil')

IS_WINDOWS = os.name == 'nt'

VERSION = '4.68'

# Environment variable to enable LUCI auth feature.
GSUTIL_ENABLE_LUCI_AUTH = 'GSUTIL_ENABLE_LUCI_AUTH'

# Google OAuth Context required by gsutil.
LUCI_AUTH_SCOPES = [
    'https://www.googleapis.com/auth/devstorage.full_control',
    'https://www.googleapis.com/auth/userinfo.email',
]


class InvalidGsutilError(Exception):
  pass


def download_gsutil(version, target_dir):
  """Downloads gsutil into the target_dir."""
  filename = 'gsutil_%s.zip' % version
  target_filename = os.path.join(target_dir, filename)

  # Check if the target exists already.
  if os.path.exists(target_filename):
    md5_calc = hashlib.md5()
    with open(target_filename, 'rb') as f:
      while True:
        buf = f.read(4096)
        if not buf:
          break
        md5_calc.update(buf)
    local_md5 = md5_calc.hexdigest()

    metadata_url = '%s%s' % (API_URL, filename)
    metadata = json.load(urllib.urlopen(metadata_url))
    remote_md5 = base64.b64decode(metadata['md5Hash']).decode('utf-8')

    if local_md5 == remote_md5:
      return target_filename
    os.remove(target_filename)

  # Do the download.
  url = '%s%s' % (GSUTIL_URL, filename)
  u = urllib.urlopen(url)
  with open(target_filename, 'wb') as f:
    while True:
      buf = u.read(4096)
      if not buf:
        break
      f.write(buf)
  return target_filename


@contextlib.contextmanager
def temporary_directory(base):
  tmpdir = tempfile.mkdtemp(prefix='t', dir=base)
  try:
    yield tmpdir
  finally:
    if os.path.isdir(tmpdir):
      shutil.rmtree(tmpdir)


def ensure_gsutil(version, target, clean):
  bin_dir = os.path.join(target, 'gsutil_%s' % version)
  gsutil_bin = os.path.join(bin_dir, 'gsutil', 'gsutil')
  gsutil_flag = os.path.join(bin_dir, 'gsutil', 'install.flag')
  # We assume that if gsutil_flag exists, then we have a good version
  # of the gsutil package.
  if not clean and os.path.isfile(gsutil_flag):
    # Everything is awesome! we're all done here.
    return gsutil_bin

  if not os.path.exists(target):
    try:
      os.makedirs(target)
    except FileExistsError:
      # Another process is prepping workspace, so let's check if gsutil_bin is
      # present.  If after several checks it's still not, continue with
      # downloading gsutil.
      delay = 2  # base delay, in seconds
      for _ in range(3):  # make N attempts
        # sleep first as it's not expected to have file ready just yet.
        time.sleep(delay)
        delay *= 1.5  # next delay increased by that factor
        if os.path.isfile(gsutil_bin):
          return gsutil_bin

  with temporary_directory(target) as instance_dir:
    # Clean up if we're redownloading a corrupted gsutil.
    cleanup_path = os.path.join(instance_dir, 'clean')
    try:
      os.rename(bin_dir, cleanup_path)
    except (OSError, IOError):
      cleanup_path = None
    if cleanup_path:
      shutil.rmtree(cleanup_path)

    download_dir = os.path.join(instance_dir, 'd')
    target_zip_filename = download_gsutil(version, instance_dir)
    with zipfile.ZipFile(target_zip_filename, 'r') as target_zip:
      target_zip.extractall(download_dir)

    shutil.move(download_dir, bin_dir)
    # Final check that the gsutil bin exists.  This should never fail.
    if not os.path.isfile(gsutil_bin):
      raise InvalidGsutilError()
    # Drop a flag file.
    with open(gsutil_flag, 'w') as f:
      f.write('This flag file is dropped by gsutil.py')

  return gsutil_bin


def _is_luci_context():
  """Returns True if the script is run within luci-context"""
  luci_context_env = os.getenv('LUCI_CONTEXT')
  if not luci_context_env:
    return False

  try:
    with open(luci_context_env) as f:
      luci_context_json = json.load(f)
      return 'local_auth' in luci_context_json
  except (ValueError, FileNotFoundError):
    return False


def luci_context(cmd):
  """Helper to call`luci-auth context`."""
  return _luci_auth_cmd('context', wrapped_cmds=cmd)


def luci_login():
  """Helper to run `luci-auth login`."""
  # luci-auth requires interactive shell.
  return _luci_auth_cmd('login', interactive=True)


def _luci_auth_cmd(luci_cmd, wrapped_cmds=None, interactive=False):
  """Helper to call luci-auth command."""
  print('Using luci-auth login since OOB is deprecated.')
  print('Override luci-auth by setting `BOTO_CONFIG` in your env.\n')

  cmd = ['luci-auth', luci_cmd, '-scopes', ' '.join(LUCI_AUTH_SCOPES)]
  if wrapped_cmds:
    cmd += ['--'] + wrapped_cmds

  if interactive:
    return _run_subprocess(cmd, interactive=True, env=_enable_luci_auth_ui())

  p = _run_subprocess(cmd, env=_enable_luci_auth_ui())

  # If luci-auth is not logged in.
  if b'Not logged in.' in p.stderr:
    print('Not logged in.\n')
    print('Login by running:')
    print('\t$ gsutil.py config')
  else:
    if p.stdout:
      print(p.stdout.decode('utf-8'))

    if p.stderr:
      print(p.stderr.decode('utf-8'), file=sys.stderr)

  return p


def _enable_luci_auth_ui():
  """Returns environment variables to enable luci-auth"""
  # TODO(aravindvasudev): clean up after luci-auth UI is released.
  return {'LUCI_AUTH_LOGIN_SESSIONS_HOST': 'ci.chromium.org'}


def _run_subprocess(cmd, interactive=False, env=None):
  """Wrapper to run the given command within a subprocess."""
  kwargs = {'shell': IS_WINDOWS}

  if env:
    kwargs['env'] = dict(os.environ, **env)

  if not interactive:
    kwargs['stdout'] = subprocess.PIPE
    kwargs['stderr'] = subprocess.PIPE

  return subprocess.run(cmd, **kwargs)


def is_boto_present():
  """Returns true if the .boto file is present in the default path."""
  return os.path.isfile(os.path.join(os.path.expanduser('~'), '.boto'))


def run_gsutil(target, args, clean=False):
  # Redirect gsutil config calls to luci-auth.
  if os.getenv(GSUTIL_ENABLE_LUCI_AUTH) == '1' and 'config' in args:
    return luci_login().returncode

  gsutil_bin = ensure_gsutil(VERSION, target, clean)
  args_opt = ['-o', 'GSUtil:software_update_check_period=0']

  if sys.platform == 'darwin':
    # We are experiencing problems with multiprocessing on MacOS where gsutil.py
    # may hang.
    # This behavior is documented in gsutil codebase, and recommendation is to
    # set GSUtil:parallel_process_count=1.
    # https://github.com/GoogleCloudPlatform/gsutil/blob/06efc9dc23719fab4fd5fadb506d252bbd3fe0dd/gslib/command.py#L1331
    # https://github.com/GoogleCloudPlatform/gsutil/issues/1100
    args_opt.extend(['-o', 'GSUtil:parallel_process_count=1'])
  if sys.platform == 'cygwin':
    # This script requires Windows Python, so invoke with depot_tools'
    # Python.
    def winpath(path):
      stdout = subprocess.check_output(['cygpath', '-w', path])
      return stdout.strip().decode('utf-8', 'replace')
    cmd = ['python.bat', winpath(__file__)]
    cmd.extend(args)
    sys.exit(subprocess.call(cmd))
  assert sys.platform != 'cygwin'

  cmd = [
      'vpython3',
      '-vpython-spec', os.path.join(THIS_DIR, 'gsutil.vpython3'),
      '--',
      gsutil_bin
  ] + args_opt + args

  # Bypass luci-auth when run within a bot or .boto file is set.
  if (os.getenv(GSUTIL_ENABLE_LUCI_AUTH) != '1' or _is_luci_context()
      or os.getenv('SWARMING_HEADLESS') == '1' or os.getenv('BOTO_CONFIG')
      or os.getenv('AWS_CREDENTIAL_FILE') or is_boto_present()):
    return _run_subprocess(cmd, interactive=True).returncode

  return luci_context(cmd).returncode


def parse_args():
  bin_dir = os.environ.get('DEPOT_TOOLS_GSUTIL_BIN_DIR', DEFAULT_BIN_DIR)

  # Help is disabled as it conflicts with gsutil -h, which controls headers.
  parser = argparse.ArgumentParser(add_help=False)

  parser.add_argument('--clean', action='store_true',
      help='Clear any existing gsutil package, forcing a new download.')
  parser.add_argument('--target', default=bin_dir,
      help='The target directory to download/store a gsutil version in. '
           '(default is %(default)s).')

  # These two args exist for backwards-compatibility but are no-ops.
  parser.add_argument('--force-version', default=VERSION,
                      help='(deprecated, this flag has no effect)')
  parser.add_argument('--fallback',
                      help='(deprecated, this flag has no effect)')

  parser.add_argument('args', nargs=argparse.REMAINDER)

  args, extras = parser.parse_known_args()
  if args.args and args.args[0] == '--':
    args.args.pop(0)
  if extras:
    args.args = extras + args.args
  return args


def main():
  args = parse_args()
  return run_gsutil(args.target, args.args, clean=args.clean)


if __name__ == '__main__':
  sys.exit(main())
