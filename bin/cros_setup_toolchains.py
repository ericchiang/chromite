#!/usr/bin/env python
# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""This script manages the installed toolchains in the chroot.
"""

import copy
import optparse
import os
import sys


sys.path.insert(0, os.path.abspath(__file__ + '/../..'))
import lib.cros_build_lib as cros_build_lib
import buildbot.constants as constants

# Some sanity checks first.
if not cros_build_lib.IsInsideChroot():
  print '%s: This needs to be run inside the chroot' % sys.argv[0]
  sys.exit(1)
# Only import portage after we've checked that we're inside the chroot.
# Outside may not have portage, in which case the above may not happen.
import portage


EMERGE_CMD = os.path.join(os.path.dirname(__file__), 'parallel_emerge')
PACKAGE_STABLE = '[stable]'
PACKAGE_NONE = '[none]'
SRC_ROOT = os.path.realpath(constants.SOURCE_ROOT)
TOOLCHAIN_PACKAGES = ('gcc', 'glibc', 'binutils', 'linux-headers', 'gdb')


# TODO: The versions are stored here very much like in setup_board.
# The goal for future is to differentiate these using a config file.
# This is done essentially by messing with GetDesiredPackageVersions()
DEFAULT_VERSION = PACKAGE_STABLE
DEFAULT_TARGET_VERSION_MAP = {
  'linux-headers' : '3.1',
  'binutils' : '2.21-r4',
}
TARGET_VERSION_MAP = {
  'arm-none-eabi' : {
    'glibc' : PACKAGE_NONE,
    'linux-headers' : PACKAGE_NONE,
    'gdb' : PACKAGE_NONE,
  },
  'host' : {
    'binutils' : '2.21.1',
    'gdb' : PACKAGE_NONE,
  },
}
# Overrides for {gcc,binutils}-config, pick a package with particular suffix.
CONFIG_TARGET_SUFFIXES = {
  'binutils' : {
    'i686-pc-linux-gnu' : '-gold',
    'x86_64-cros-linux-gnu' : '-gold',
  },
}
# FIXME(zbehan): This is used to override the above. Before we compile
# cross-glibc, we need to set the cross-binutils to GNU ld. Ebuilds should
# handle this by themselves.
CONFIG_TARGET_SUFFIXES_nongold = {
  'binutils' : {
    'i686-pc-linux-gnu' : '',
    'x86_64-cros-linux-gnu' : '',
  },
}
# Global per-run cache that will be filled ondemand in by GetPackageMap()
# function as needed.
target_version_map = {
}


def GetPackageMap(target):
  """Compiles a package map for the given target from the constants.

  Uses a cache in target_version_map, that is dynamically filled in as needed,
  since here everything is static data and the structuring is for ease of
  configurability only.

  args:
    target - the target for which to return a version map

  returns a map between packages and desired versions in internal format
  (using the PACKAGE_* constants)
  """
  if target in target_version_map:
    return target_version_map[target]

  # Start from copy of the global defaults.
  result = copy.copy(DEFAULT_TARGET_VERSION_MAP)

  for pkg in TOOLCHAIN_PACKAGES:
  # prefer any specific overrides
    if pkg in TARGET_VERSION_MAP.get(target, {}):
      result[pkg] = TARGET_VERSION_MAP[target][pkg]
    else:
      # finally, if not already set, set a sane default
      result.setdefault(pkg, DEFAULT_VERSION)
  target_version_map[target] = result
  return result


# Helper functions:
def GetPortageCategory(target, package):
  """Creates a package name for the given target.

  The function provides simple abstraction around the confusing fact that
  cross packages all live under a single category, while host packages have
  categories varied. This lets us further identify packages solely by the
  (target, package) pair and worry less about portage names.

  args:
    target, package - the target/package to operate on eg. i686-pc-linux-gnu,gcc

  returns string with the portage category
  """
  HOST_MAP = {
    'gcc' : 'sys-devel',
    'gdb' : 'sys-devel',
    'glibc' : 'sys-libs',
    'binutils' : 'sys-devel',
    'linux-headers' : 'sys-kernel',
  }

  if target == 'host':
    return HOST_MAP[package]
  else:
    return 'cross-' + target


def GetHostTarget():
  """Returns a string for the host target tuple."""
  return portage.settings['CHOST']


# Tree interface functions. They help with retrieving data about the current
# state of the tree:
def GetAllTargets():
  """Get the complete list of targets.

  returns the list of cross targets for the current tree
  """
  cmd = ['cros_overlay_list', '--all_boards']
  overlays = cros_build_lib.RunCommand(cmd, print_cmd=False,
                                       redirect_stdout=True).output.splitlines()
  targets = set()
  for overlay in overlays:
    config = os.path.join(overlay, 'toolchain.conf')
    if os.path.exists(config):
      with open(config) as config_file:
        lines = config_file.read().splitlines()
        for line in lines:
          # Remove empty lines and commented lines.
          if line and not line.startswith('#'):
            targets.add(line)

  # Remove the host target as that is not a cross-target. Replace with 'host'.
  targets.remove(GetHostTarget())
  targets.add('host')
  return targets


def GetInstalledPackageVersions(target, package):
  """Extracts the list of current versions of a target, package pair.

  args:
    target, package - the target/package to operate on eg. i686-pc-linux-gnu,gcc

  returns the list of versions of the package currently installed.
  """
  category = GetPortageCategory(target, package)
  versions = []
  # This is the package name in terms of portage.
  atom = '%s/%s' % (category, package)
  for pkg in portage.db['/']['vartree'].dbapi.match(atom):
    version = portage.versions.cpv_getversion(pkg)
    versions.append(version)
  return versions


def GetPortageKeyword(_target):
  """Returns a portage friendly keyword for a given target."""
  # NOTE: This table is part of the one found in crossdev.
  PORTAGE_KEYWORD_MAP = {
    'x86_64-' : 'amd64',
    'i686-' : 'x86',
    'arm' : 'arm',
  }
  if _target == 'host':
    target = GetHostTarget()
  else:
    target = _target

  for prefix, arch in PORTAGE_KEYWORD_MAP.iteritems():
    if target.startswith(prefix):
      return arch
  else:
    raise RuntimeError("Unknown target: " + _target)


def GetStablePackageVersion(target, package):
  """Extracts the current stable version for a given package.

  args:
    target, package - the target/package to operate on eg. i686-pc-linux-gnu,gcc

  returns a string containing the latest version.
  """
  category = GetPortageCategory(target, package)
  keyword = GetPortageKeyword(target)
  extra_env = {'ACCEPT_KEYWORDS' : '-* ' + keyword}
  atom = '%s/%s' % (category, package)
  cpv = cros_build_lib.RunCommand(['portageq', 'best_visible', '/', atom],
                                  print_cmd=False, redirect_stdout=True,
                                  extra_env=extra_env).output.splitlines()[0]
  return portage.versions.cpv_getversion(cpv)


def VersionListToNumeric(target, package, versions):
  """Resolves keywords in a given version list for a particular package.

  Resolving means replacing PACKAGE_STABLE with the actual number.

  args:
    target, package - the target/package to operate on eg. i686-pc-linux-gnu,gcc
    versions - list of versions to resolve

  returns list of purely numeric versions equivalent to argument
  """
  resolved = []
  for version in versions:
    if version == PACKAGE_STABLE:
      resolved.append(GetStablePackageVersion(target, package))
    elif version != PACKAGE_NONE:
      resolved.append(version)
  return resolved


def GetDesiredPackageVersions(target, package):
  """Produces the list of desired versions for each target, package pair.

  The first version in the list is implicitly treated as primary, ie.
  the version that will be initialized by crossdev and selected.

  If the version is PACKAGE_STABLE, it really means the current version which
  is emerged by using the package atom with no particular version key.
  Since crossdev unmasks all packages by default, this will actually
  mean 'unstable' in most cases.

  args:
    target, package - the target/package to operate on eg. i686-pc-linux-gnu,gcc

  returns a list composed of either a version string, PACKAGE_STABLE
  """
  packagemap = GetPackageMap(target)

  versions = []
  if package in packagemap:
    versions.append(packagemap[package])

  return versions


def TargetIsInitialized(target):
  """Verifies if the given list of targets has been correctly initialized.

  This determines whether we have to call crossdev while emerging
  toolchain packages or can do it using emerge. Emerge is naturally
  preferred, because all packages can be updated in a single pass.

  args:
    targets - list of individual cross targets which are checked

  returns True if target is completely initialized
  returns False otherwise
  """
  # Check if packages for the given target all have a proper version.
  try:
    for package in TOOLCHAIN_PACKAGES:
      if not GetInstalledPackageVersions(target, package) and \
        GetDesiredPackageVersions(target, package) != [PACKAGE_NONE]:
        return False
    return True
  except cros_build_lib.RunCommandError:
    # Fails - The target has likely never been initialized before.
    return False


def RemovePackageMask(target):
  """Removes a package.mask file for the given platform.

  The pre-existing package.mask files can mess with the keywords.

  args:
    target - the target for which to remove the file
  """
  maskfile = os.path.join('/etc/portage/package.mask', 'cross-' + target)
  # Note: We have to use sudo here, in case the file is created with
  # root ownership. However, sudo in chroot is always passwordless.
  cros_build_lib.SudoRunCommand(['rm', '-f', maskfile],
      redirect_stdout=True, print_cmd=False)


def CreatePackageMask(target, masks):
  """[Re]creates a package.mask file for the given platform.

  args:
    target - the given target on which to operate
    masks - a map of package : version,
        where version is the highest permissible version (mask >)
  """
  maskfile = os.path.join('/etc/portage/package.mask', 'cross-' + target)
  assert not os.path.exists(maskfile)
  # package.mask/ isn't writable, so we need to create and
  # chown the file before we use it.
  cros_build_lib.SudoRunCommand(['touch', maskfile],
      redirect_stdout=True, print_cmd=False)
  cros_build_lib.SudoRunCommand(['chown', str(os.getuid()), maskfile],
      redirect_stdout=True, print_cmd=False)

  with open(maskfile, 'w') as f:
    for pkg, m in masks.items():
      f.write('>%s-%s\n' % (pkg, m))


def CreatePackageKeywords(target):
  """[Re]create a package.keywords file for the platform.

  This sets all package.keywords files to unmask all stable/testing packages.
  TODO: Note that this approach should be deprecated and is only done for
  compatibility reasons. In the future, we'd like to stop using keywords
  altogether, and keep just stable unmasked.

  args:
    target - target for which to recreate package.keywords
  """
  maskfile = os.path.join('/etc/portage/package.keywords', 'cross-' + target)
  cros_build_lib.SudoRunCommand(['rm', '-f', maskfile],
      redirect_stdout=True, print_cmd=False)
  cros_build_lib.SudoRunCommand(['touch', maskfile],
      redirect_stdout=True, print_cmd=False)
  cros_build_lib.SudoRunCommand(['chown', '%d' % os.getuid(), maskfile],
      redirect_stdout=True, print_cmd=False)

  keyword = GetPortageKeyword(target)

  with open(maskfile, 'w') as f:
    for pkg in TOOLCHAIN_PACKAGES:
      f.write('%s/%s %s ~%s\n' %
              (GetPortageCategory(target, pkg), pkg, keyword, keyword))


# Main functions performing the actual update steps.
def InitializeCrossdevTargets(targets, usepkg):
  """Calls crossdev to initialize a cross target.
  args:
    targets - the list of targets to initialize using crossdev
    usepkg - copies the commandline opts
  """
  print 'The following targets need to be re-initialized:'
  print targets
  CROSSDEV_FLAG_MAP = {
    'gcc' : '--gcc',
    'glibc' : '--libc',
    'linux-headers' : '--kernel',
    'binutils' : '--binutils',
  }

  for target in targets:
    cmd = ['sudo', 'FEATURES=splitdebug', 'crossdev', '-v', '-t', target]
    # Pick stable by default, and override as necessary.
    cmd.extend(['-P', '--oneshot'])
    if usepkg:
      cmd.extend(['-P', '--getbinpkg',
                  '-P', '--usepkgonly',
                  '--without-headers'])
    cmd.append('--ex-gdb')

    # HACK(zbehan): arm-none-eabi uses newlib which doesn't like headers-only.
    if target == 'arm-none-eabi':
      cmd.append('--without-headers')

    for pkg in CROSSDEV_FLAG_MAP:
      # The first of the desired versions is the "primary" one.
      version = GetDesiredPackageVersions(target, pkg)[0]
      if version != PACKAGE_NONE:
        cmd.extend([CROSSDEV_FLAG_MAP[pkg], version])
    cros_build_lib.RunCommand(cmd)


def UpdateTargets(targets, usepkg):
  """Determines which packages need update/unmerge and defers to portage.

  args:
    targets - the list of targets to update
    usepkg - copies the commandline option
  """
  # TODO(zbehan): This process is rather complex due to package.* handling.
  # With some semantic changes over the original setup_board functionality,
  # it can be considerably cleaned up.
  mergemap = {}

  # For each target, we do two things. Figure out the list of updates,
  # and figure out the appropriate keywords/masks. Crossdev will initialize
  # these, but they need to be regenerated on every update.
  print 'Determining required toolchain updates...'
  for target in targets:
    # Record the highest needed version for each target, for masking purposes.
    RemovePackageMask(target)
    CreatePackageKeywords(target)
    packagemasks = {}
    for package in TOOLCHAIN_PACKAGES:
      # Portage name for the package
      pkg = GetPortageCategory(target, package) + '/' + package
      current = GetInstalledPackageVersions(target, package)
      desired = GetDesiredPackageVersions(target, package)
      if desired != [PACKAGE_NONE]:
        desired_num = VersionListToNumeric(target, package, desired)
        mergemap[pkg] = set(desired_num).difference(current)

      # Pick the highest version for mask.
      packagemasks[pkg] = portage.versions.best(desired_num)

    CreatePackageMask(target, packagemasks)

  packages = []
  for pkg in mergemap:
    for ver in mergemap[pkg]:
      if ver == PACKAGE_STABLE:
        packages.append(pkg)
      elif ver != PACKAGE_NONE:
        packages.append('=%s-%s' % (pkg, ver))

  if not packages:
    print 'Nothing to update!'
    return

  print 'Updating packages:'
  print packages

  # FIXME(zbehan): Override gold if we're doing source builds. See comment
  # at the constant definition.
  if not usepkg:
    SelectActiveToolchains(targets, CONFIG_TARGET_SUFFIXES_nongold)

  cmd = ['sudo', '-E', 'FEATURES=splitdebug', EMERGE_CMD,
       '--oneshot', '--update']
  if usepkg:
    cmd.extend(['--getbinpkg', '--usepkgonly'])

  cmd.extend(packages)
  cros_build_lib.RunCommand(cmd)


def CleanTargets(targets):
  """Unmerges old packages that are assumed unnecessary."""
  unmergemap = {}
  for target in targets:
    for package in TOOLCHAIN_PACKAGES:
      pkg = GetPortageCategory(target, package) + '/' + package
      current = GetInstalledPackageVersions(target, package)
      desired = GetDesiredPackageVersions(target, package)
      desired_num = VersionListToNumeric(target, package, desired)
      assert set(desired).issubset(set(desired))
      unmergemap[pkg] = set(current).difference(desired_num)

  # Cleaning doesn't care about consistency and rebuilding package.* files.
  packages = []
  for pkg, vers in unmergemap.iteritems():
    packages.extend('=%s-%s' % (pkg, ver) for ver in vers if ver != '9999')

  if packages:
    print 'Cleaning packages:'
    print packages
    cmd = ['sudo', '-E', EMERGE_CMD, '--unmerge']
    cmd.extend(packages)
    cros_build_lib.RunCommand(cmd)
  else:
    print 'Nothing to clean!'


def SelectActiveToolchains(targets, suffixes):
  """Runs gcc-config and binutils-config to select the desired.

  args:
    targets - the targets to select
  """
  for package in ['gcc', 'binutils']:
    for target in targets:
      # Pick the first version in the numbered list as the selected one.
      desired = GetDesiredPackageVersions(target, package)
      desired_num = VersionListToNumeric(target, package, desired)
      desired = desired_num[0]
      # *-config does not play revisions, strip them, keep just PV.
      desired = portage.versions.pkgsplit('%s-%s' % (package, desired))[1]

      if target == 'host':
        # *-config is the only tool treating host identically (by tuple).
        target = GetHostTarget()

      # And finally, attach target to it.
      desired = '%s-%s' % (target, desired)

      # Target specific hacks
      if package in suffixes:
        if target in suffixes[package]:
          desired += suffixes[package][target]

      cmd = [ package + '-config', '-c', target ]
      current = cros_build_lib.RunCommand(cmd, print_cmd=False,
                    redirect_stdout=True).output.splitlines()[0]
      # Do not gcc-config when the current is live or nothing needs to be done.
      if current != desired and current != '9999':
        cmd = [ package + '-config', desired ]
        cros_build_lib.SudoRunCommand(cmd, print_cmd=False)


def UpdateToolchains(usepkg, deleteold, targets):
  """Performs all steps to create a synchronized toolchain enviroment.

  args:
    arguments correspond to the given commandline flags
  """
  alltargets = GetAllTargets()
  nonexistant = []
  if targets == set(['all']):
    targets = alltargets
  else:
    # Verify user input.
    for target in targets:
      if target not in alltargets:
        nonexistant.append(target)
  if nonexistant:
    raise Exception("Invalid targets: " + ','.join(nonexistant))

  # First check and initialize all cross targets that need to be.
  crossdev_targets = \
      [t for t in targets if not TargetIsInitialized(t) and not 'host' == t]
  if crossdev_targets:
    try:
      SelectActiveToolchains(crossdev_targets, CONFIG_TARGET_SUFFIXES_nongold)
    except cros_build_lib.RunCommandError:
      # This can fail if the target has never been initialized before.
      pass
    InitializeCrossdevTargets(crossdev_targets, usepkg)

  # Now update all packages, including host.
  targets.add('host')
  UpdateTargets(targets, usepkg)
  SelectActiveToolchains(targets, CONFIG_TARGET_SUFFIXES)

  if deleteold:
    CleanTargets(targets)


def main(argv):
  usage = """usage: %prog [options]

  The script installs and updates the toolchains in your chroot.
  """
  parser = optparse.OptionParser(usage)
  parser.add_option('-u', '--nousepkg',
                    action='store_false', dest='usepkg', default=True,
                    help=('Use prebuilt packages if possible.'))
  parser.add_option('-d', '--deleteold',
                    action='store_true', dest='deleteold', default=False,
                    help=('Unmerge deprecated packages.'))
  parser.add_option('-t', '--targets',
                    dest='targets', default='all',
                    help=('Comma separated list of targets. Default: all'))

  (options, _remaining_arguments) = parser.parse_args(argv)

  targets = set(options.targets.split(','))
  UpdateToolchains(options.usepkg, options.deleteold, targets)

if __name__ == '__main__':
  main(sys.argv[1:])
