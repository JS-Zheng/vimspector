#!/usr/bin/env python3

# vimspector - A multi-language debugging system for Vim
# Copyright 2019 Ben Jackson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from urllib import request
import contextlib
import functools
import gzip
import hashlib
import io
import os
import shutil
import ssl
import string
import subprocess
import sys
import tarfile
import time
import traceback
import zipfile
import json

from vimspector import install

class Options:
  vimspector_base = None
  no_check_certificate = False


options = Options()


def Configure( **kwargs ):
  for k, v in kwargs.items():
    setattr( options, k, v )


def InstallGeneric( name, root, gadget ):
  extension = os.path.join( root, 'extension' )
  for f in gadget.get( 'make_executable', [] ):
    MakeExecutable( os.path.join( extension, f ) )

  MakeExtensionSymlink( name, root )


def InstallCppTools( name, root, gadget ):
  extension = os.path.join( root, 'extension' )

  # It's hilarious, but the execute bits aren't set in the vsix. So they
  # actually have javascript code which does this. It's just a horrible horrible
  # hack that really is not funny.
  MakeExecutable( os.path.join( extension, 'debugAdapters', 'OpenDebugAD7' ) )
  with open( os.path.join( extension, 'package.json' ) ) as f:
    package = json.load( f )
    runtime_dependencies = package[ 'runtimeDependencies' ]
    for dependency in runtime_dependencies:
      for binary in dependency.get( 'binaries' ):
        file_path = os.path.abspath( os.path.join( extension, binary ) )
        if os.path.exists( file_path ):
          MakeExecutable( os.path.join( extension, binary ) )

  MakeExtensionSymlink( name, root )


def InstallBashDebug( name, root, gadget ):
  MakeExecutable( os.path.join( root,
                                          'extension',
                                          'bashdb_dir',
                                          'bashdb' ) )
  MakeExtensionSymlink( name, root )


def InstallDebugpy( name, root, gadget ):
  wd = os.getcwd()
  root = os.path.join( root, 'debugpy-{}'.format( gadget[ 'version' ] ) )
  os.chdir( root )
  try:
    subprocess.check_call( [ sys.executable, 'setup.py', 'build' ] )
  finally:
    os.chdir( wd )

  MakeSymlink( name, root )


def InstallTclProDebug( name, root, gadget ):
  configure = [ './configure' ]

  if install.GetOS() == 'macos':
    # Apple removed the headers from system frameworks because they are
    # determined to make life difficult. And the TCL configure scripts are super
    # old so don't know about this. So we do their job for them and try and find
    # a tclConfig.sh.
    #
    # NOTE however that in Apple's infinite wisdom, installing the "headers" in
    # the other location is actually broken because the paths in the
    # tclConfig.sh are pointing at the _old_ location. You actually do have to
    # run the package installation which puts the headers back in order to work.
    # This is why the below list is does not contain stuff from
    # /Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform
    #  '/Applications/Xcode.app/Contents/Developer/Platforms'
    #    '/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System'
    #    '/Library/Frameworks/Tcl.framework',
    #  '/Applications/Xcode.app/Contents/Developer/Platforms'
    #    '/MacOSX.platform/Developer/SDKs/MacOSX.sdk/System'
    #    '/Library/Frameworks/Tcl.framework/Versions'
    #    '/Current',
    for p in [ '/usr/local/opt/tcl-tk/lib' ]:
      if os.path.exists( os.path.join( p, 'tclConfig.sh' ) ):
        configure.append( '--with-tcl=' + p )
        break


  with CurrentWorkingDir( os.path.join( root, 'lib', 'tclparser' ) ):
    subprocess.check_call( configure )
    subprocess.check_call( [ 'make' ] )

  MakeSymlink( name, root )


def InstallNodeDebug( name, root, gadget ):
  node_version = subprocess.check_output( [ 'node', '--version' ],
                                          universal_newlines=True ).strip()
  print( "Node.js version: {}".format( node_version ) )
  if list( map( int, node_version[ 1: ].split( '.' ) ) ) >= [ 12, 0, 0 ]:
    print( "Can't install vscode-debug-node2:" )
    print( "Sorry, you appear to be running node 12 or later. That's not "
           "compatible with the build system for this extension, and as far as "
           "we know, there isn't a pre-built independent package." )
    print( "My advice is to install nvm, then do:" )
    print( "  $ nvm install --lts 10" )
    print( "  $ nvm use --lts 10" )
    print( "  $ ./install_gadget.py --enable-node ..." )
    raise RuntimeError( 'Invalid node environent for node debugger' )

  with CurrentWorkingDir( root ):
    subprocess.check_call( [ 'npm', 'install' ] )
    subprocess.check_call( [ 'npm', 'run', 'build' ] )
  MakeSymlink( name, root )


def InstallGagdet( name, gadget, failed, all_adapters ):
  try:
    v = {}
    v.update( gadget.get( 'all', {} ) )
    v.update( gadget.get( install.GetOS(), {} ) )

    if 'download' in gadget:
      if 'file_name' not in v:
        raise RuntimeError( "Unsupported OS {} for gadget {}".format(
          install.GetOS(),
          name ) )

      destination = os.path.join( _GetGadgetDir(),
                                  'download',
                                  name, v[ 'version' ] )

      url = string.Template( gadget[ 'download' ][ 'url' ] ).substitute( v )

      file_path = DownloadFileTo(
        url,
        destination,
        file_name = gadget[ 'download' ].get( 'target' ),
        checksum = v.get( 'checksum' ),
        check_certificate = not options.no_check_certificate )

      root = os.path.join( destination, 'root' )
      ExtractZipTo(
        file_path,
        root,
        format = gadget[ 'download' ].get( 'format', 'zip' ) )
    elif 'repo' in gadget:
      url = string.Template( gadget[ 'repo' ][ 'url' ] ).substitute( v )
      ref = string.Template( gadget[ 'repo' ][ 'ref' ] ).substitute( v )

      destination = os.path.join( _GetGadgetDir(), 'download', name )
      CloneRepoTo( url, ref, destination )
      root = destination

    if 'do' in gadget:
      gadget[ 'do' ]( name, root, v )
    else:
      InstallGeneric( name, root, v )

    # Allow per-OS adapter overrides. v already did that for us...
    all_adapters.update( v.get( 'adapters', {} ) )
    # Add any other "all" adapters
    all_adapters.update( gadget.get( 'adapters', {} ) )

    print( "Done installing {}".format( name ) )
  except Exception as e:
    traceback.print_exc()
    failed.append( name )
    print( "FAILED installing {}: {}".format( name, e ) )


@contextlib.contextmanager
def CurrentWorkingDir( d ):
  cur_d = os.getcwd()
  try:
    os.chdir( d )
    yield
  finally:
    os.chdir( cur_d )


def MakeExecutable( file_path ):
  # TODO: import stat and use them by _just_ adding the X bit.
  print( 'Making executable: {}'.format( file_path ) )
  os.chmod( file_path, 0o755 )



def WithRetry( f ):
  retries = 5
  timeout = 1 # seconds

  @functools.wraps( f )
  def wrapper( *args, **kwargs ):
    thrown = None
    for _ in range( retries ):
      try:
        return f( *args, **kwargs )
      except Exception as e:
        thrown = e
        print( "Failed - {}, will retry in {} seconds".format( e, timeout ) )
        time.sleep( timeout )
    raise thrown

  return wrapper


@WithRetry
def UrlOpen( *args, **kwargs ):
  return request.urlopen( *args, **kwargs )


def DownloadFileTo( url,
                    destination,
                    file_name = None,
                    checksum = None,
                    check_certificate = True ):
  if not file_name:
    file_name = url.split( '/' )[ -1 ]

  file_path = os.path.abspath( os.path.join( destination, file_name ) )

  if not os.path.isdir( destination ):
    os.makedirs( destination )

  if os.path.exists( file_path ):
    if checksum:
      if ValidateCheckSumSHA256( file_path, checksum ):
        print( "Checksum matches for {}, using it".format( file_path ) )
        return file_path
      else:
        print( "Checksum doesn't match for {}, removing it".format(
          file_path ) )

    print( "Removing existing {}".format( file_path ) )
    os.remove( file_path )

  r = request.Request( url, headers = { 'User-Agent': 'Vimspector' } )

  print( "Downloading {} to {}/{}".format( url, destination, file_name ) )

  if not check_certificate:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    kwargs = { "context":  context }
  else:
    kwargs = {}

  with contextlib.closing( UrlOpen( r, **kwargs ) ) as u:
    with open( file_path, 'wb' ) as f:
      f.write( u.read() )

  if checksum:
    if not ValidateCheckSumSHA256( file_path, checksum ):
      raise RuntimeError(
        'Checksum for {} ({}) does not match expected {}'.format(
          file_path,
          GetChecksumSHA254( file_path ),
          checksum ) )
  else:
    print( "Checksum for {}: {}".format( file_path,
                                         GetChecksumSHA254( file_path ) ) )

  return file_path


def GetChecksumSHA254( file_path ):
  with open( file_path, 'rb' ) as existing_file:
    return hashlib.sha256( existing_file.read() ).hexdigest()


def ValidateCheckSumSHA256( file_path, checksum ):
  existing_sha256 = GetChecksumSHA254( file_path )
  return existing_sha256 == checksum


def RemoveIfExists( destination ):
  if os.path.islink( destination ):
    print( "Removing file {}".format( destination ) )
    os.remove( destination )
    return

  N = 1


  def BackupDir():
    return "{}.{}".format( destination, N )

  while os.path.isdir( BackupDir() ):
    print( "Removing old dir {}".format( BackupDir() ) )
    try:
      shutil.rmtree( BackupDir() )
      print ( "OK, removed it" )
      break
    except OSError:
      print ( "FAILED" )
      N = N + 1

  if os.path.exists( destination ):
    print( "Removing dir {}".format( destination ) )
    try:
      shutil.rmtree( destination )
    except OSError:
      print( "FAILED, moving {} to dir {}".format( destination, BackupDir() ) )
      os.rename( destination, BackupDir() )


# Python's ZipFile module strips execute bits from files, for no good reason
# other than crappy code. Let's do it's job for it.
class ModePreservingZipFile( zipfile.ZipFile ):
  def extract( self, member, path = None, pwd = None ):
    if not isinstance( member, zipfile.ZipInfo ):
      member = self.getinfo( member )

    if path is None:
      path = os.getcwd()

    ret_val = self._extract_member( member, path, pwd )
    attr = member.external_attr >> 16
    os.chmod( ret_val, attr )
    return ret_val


def ExtractZipTo( file_path, destination, format ):
  print( "Extracting {} to {}".format( file_path, destination ) )
  RemoveIfExists( destination )

  if format == 'zip':
    with ModePreservingZipFile( file_path ) as f:
      f.extractall( path = destination )
  elif format == 'zip.gz':
    with gzip.open( file_path, 'rb' ) as f:
      file_contents = f.read()

    with ModePreservingZipFile( io.BytesIO( file_contents ) ) as f:
      f.extractall( path = destination )

  elif format == 'tar':
    try:
      with tarfile.open( file_path ) as f:
        f.extractall( path = destination )
    except Exception:
      # There seems to a bug in python's tarfile that means it can't read some
      # windows-generated tar files
      os.makedirs( destination )
      with CurrentWorkingDir( destination ):
        subprocess.check_call( [ 'tar', 'zxvf', file_path ] )


def _GetGadgetDir():
  return install.GetGadgetDir( options.vimspector_base, install.GetOS() )


def MakeExtensionSymlink( name, root ):
  MakeSymlink( name, os.path.join( root, 'extension' ) ),


def MakeSymlink( link, pointing_to, in_folder = None ):
  if not in_folder:
    in_folder = _GetGadgetDir()

  RemoveIfExists( os.path.join( in_folder, link ) )

  in_folder = os.path.abspath( in_folder )
  pointing_to_relative = os.path.relpath( os.path.abspath( pointing_to ),
                                          in_folder )
  link_path = os.path.join( in_folder, link )

  if install.GetOS() == 'windows':
    # While symlinks do exist on Windows, they require elevated privileges, so
    # let's use a directory junction which is all we need.
    link_path = os.path.abspath( link_path )
    if os.path.isdir( link_path ):
      os.rmdir( link_path )
    subprocess.check_call( [ 'cmd.exe',
                             '/c',
                             'mklink',
                             '/J',
                             link_path,
                             pointing_to ] )
  else:
    os.symlink( pointing_to_relative, link_path )


def CloneRepoTo( url, ref, destination ):
  RemoveIfExists( destination )
  git_in_repo = [ 'git', '-C', destination ]
  subprocess.check_call( [ 'git', 'clone', url, destination ] )
  subprocess.check_call( git_in_repo + [ 'checkout', ref ] )
  subprocess.check_call( git_in_repo + [ 'submodule', 'sync', '--recursive' ] )
  subprocess.check_call( git_in_repo + [ 'submodule',
                                         'update',
                                         '--init',
                                         '--recursive' ] )


def AbortIfSUperUser( force_sudo ):
  # TODO: We should probably check the effective uid too
  is_su = False
  if 'SUDO_COMMAND' in os.environ:
    is_su = True

  if is_su:
    if force_sudo:
      print( "*** RUNNING AS SUPER USER DUE TO force_sudo! "
             "    All bets are off. ***" )
    else:
      sys.exit( "This script should *not* be run as super user. Aborting." )
