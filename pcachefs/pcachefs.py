#!/usr/bin/env python

"""
   Persistent caching FUSE filesystem

   Copyright 2012 Jonny Tyers

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

"""

import fuse
import stat
import os
import time
import sys
import pickle
import types
import factory

from datetime import datetime
from ranges import (Ranges, Range)
from optparse import OptionGroup
from pcachefsutil import debug

# We explicitly refer to __builtin__ here so it can be mocked
import __builtin__

import vfs

fuse.fuse_python_api = (0, 2)


class FuseStat(fuse.Stat):
    """
    Convenient class for Stat objects.
    Set up the stat object based on values from the given
    stat object (which should come from os.stat()).
    """
    def __init__(self, stat):
        fuse.Stat.__init__(self)

        self.st_mode = stat.st_mode
        self.st_nlink = stat.st_nlink
        self.st_size = stat.st_size
        self.st_atime = stat.st_atime
        self.st_mtime = stat.st_mtime
        self.st_ctime = stat.st_ctime

        self.st_dev = stat.st_dev
        self.st_gid = stat.st_gid
        self.st_ino = stat.st_ino
        self.st_uid = stat.st_uid

        #self.st_rdev = stat.st_rdev
        #self.st_blksize = stat.st_blksize

"""
 Main FUSE class - this just delegates operations to a Cacher instance
"""
class PersistentCacheFs(fuse.Fuse):
    # All 'special' (virtual) files begin with this prefix
    SPECIAL_FILE_PREFIX = '/.pcache'

    # Name of the file containing the 'cache mode only' flag
    CACHE_ONLY_MODE_PATH = '/.pcache.cache_only_mode'

    def __init__(self, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)

        # Currently we have to run in single-threaded mode to prevent the cache becoming corrupted
        fuse_opts = self.parse(['-s'])

        self.parser.add_option('-c', '--cache-dir', dest='cache_dir', help="Specifies the directory where cached data should be stored. This will be created if it does not exist.")
        self.parser.add_option('-t', '--target-dir', dest='target_dir', help="The directory which we are caching. The content of this directory will be mirrored and all reads cached.")

    def main(self, args=None):
        options = self.cmdline[0]

        try:
            if options.cache_dir == None:
                raise ValueError('Need to specify --cache-dir')
            if options.target_dir == None:
                raise ValueError('Need to specify --target-dir')
        except Exception, e:
            debug('PersistentCacheFs Exception', e)
            sys.exit(1)

        self.cache_dir = options.cache_dir
        self.target_dir = options.target_dir

        self.cacher = Cacher(self.cache_dir, UnderlyingFs(self.target_dir))

        # Initialise the VirtualFileFS, which contains 'virtual' files which
        # can be used by user apps to read and change internal pcachefs state
        self.vfs = vfs.VirtualFileFS('.pcachefs.')
        self.vfs.add_file(
            vfs.BooleanVirtualFile('cache_only',
                callback_on_true = self.cacher.cache_only_mode_enable,
                callback_on_false = self.cacher.cache_only_mode_disable)
        )

        fuse.Fuse.main(self, args)

    def getattr(self, path):
        if self.vfs.contains(path):
            return self.vfs.getattr(path)

        return self.cacher.getattr(path)

    def readdir(self, path, offset):
        for f in self.vfs.readdir(path, offset):
            yield f

        for f in self.cacher.readdir(path, offset):
            yield f

    def open(self, path, flags):
        if self.vfs.contains(path):
            return self.vfs.open(path, flags)

        # Only support for 'READ ONLY' flag
        access_flags = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if flags & access_flags != os.O_RDONLY:
            return E_PERM_DENIED
        else:
            return 0

    def read(self, path, size, offset):
        if self.vfs.contains(path):
            return self.vfs.read(path, size, offset)

        return self.cacher.read(path, size, offset)

    def write(self, path, buf, offset):
        if self.vfs.contains(path):
            return self.vfs.write(path, buf, offset)

        return E_NOT_IMPL

    def flush(self, path):
        if self.vfs.contains(path):
            return self.vfs.flush(path)

        return 0 # success

    def release(self, path, what):
        debug('release ' + str(path) + ', ' + str(what))
        if self.vfs.contains(path):
            return self.vfs.release(path)

        return 0 # success

#    def _getattr_special(self, path):
#        return FuseStat(os.stat('/proc/version')) # FIXME stat of the FUSE mountpoint
#
#    def _read_special(self, path, size, offset):
#        debug("_read_special", path, size, offset)
#        content = None
#
#        if path == self.CACHE_ONLY_MODE_PATH:
#            debug("_read_special com", path, size, offset)
#            if self.cacher.cache_only_mode == True:
#                debug(" return 1")
#                return '111111111111111111111111111\n'[offset:offset+size]
#            else:
#                debug(" return 0")
#                return '000000000000000000000000000\n'[offset:offset+size]
#
#        else:
#            debug(" return NSF")
#            return E_NO_SUCH_FILE
#
#    def _write_special(self, path, buf, offset):
#        content = buf.strip()
#        debug("_write_special", path, buf, offset)
#
#        if path == self.CACHE_ONLY_MODE_PATH:
#            if content == '0':
#                self.cacher.cache_only_mode = False
#                return len(buf) # wrote one byte
#
#            elif content == '1':
#                self.cacher.cache_only_mode = True
#                return len(buf) # wrote one byte
#
#            else:
#                return self.E_INVAL
#
#        else:
#            return E_NO_SUCH_FILE

""" Implementation of FUSE operations that fetches data from the underlying FS """
class UnderlyingFs:
    def __init__(self, real_path):
        self.real_path = real_path

    def _get_real_path(self, path):
        if path[0] != '/':
            raise ValueError("Expected leading slash")

        return os.path.join(self.real_path, path[1:])

    def _create_direntry(self, path):
        dtype = 0

        return fuse.Direntry(os.path.basename(r))

    def getattr(self, path):
        debug('UFS GETATTR C_F_S')
        return factory.create(FuseStat, os.stat(self._get_real_path(path)))

    def readdir(self, path, offset):
        real_path = self._get_real_path(path)

        dirents = []
        if os.path.isdir(real_path):
            dirents.extend([ '.', '..' ])

        dirents.extend(os.listdir(real_path))

        # return a generator over the entries in the directory
        return (fuse.Direntry(r) for r in dirents)

    def read(self, path, size, offset):
        debug('ufs.read', path, str(size), str(offset))
        real_path = self._get_real_path(path)

        with __builtin__.open(real_path, 'rb') as f:
            f.seek(offset)
            result = f.read(size)

        return result
"""
# Represents a cache, which caches entire files and their content. This class mimics
 the interface of a python Fuse object.
#
# The cache is a standard filesystem directory.
#
# Initially the implementation will copy *entire* files (incl metadata)
# down into the cache when they are read.
#
# The cached files are stored as follows in the cache directory:
#   /cache/dir/filename.ext/cache.data   # copy of file data
#   /cache/dir/filename.ext/cache.stat  # pickle'd stat object (from os.stat())
#   /cache/dir/cache.list # pickle'd directory listing (from os.listdir())
#
#
# For writes to files in the cache, these are passed through to the
# underlying filesystem without any caching.
#"""
class Cacher:

    """
    # Initialise a new Cacher.
    #
    # cachedir the directory in which to store cached files and metadata (this will
    #   created automatically if it does not exist)
    # underlying_fs an object supporting the read(), readdir() and getattr() FUSE
    #   operations. For any files/dirs not in the cache, this object's methods will
    #   be called to retrieve the real data and populate the cache.
    """
    def __init__(self, cachedir, underlying_fs):
        self.cachedir = cachedir
        self.underlying_fs = underlying_fs

        # If this is set to True, the cacher will fail if any
        # requests are made for data that does not exist in the cache
        self.cache_only_mode = False

        debug('cdir: ' + self.cachedir)
        debug('os: ' + str(type(os)))
        debug('pathexists: ' + str(os.path.exists(self.cachedir)))

        if not os.path.exists(self.cachedir):
            self._mkdir(self.cachedir)

    def cache_only_mode_enable(self):
        debug('cacher cache_only_mode enabled')
        self.cache_only_mode = True

    def cache_only_mode_disable(self):
        debug('cacher cache_only_mode disabled')
        self.cache_only_mode = False

    """
    Read the given data from the given path on the filesystem.

    Any parts which are requested and are not in the cache are read
    from the underlying filesystem
    """
    def read(self, path, size, offset):
        debug('cacher.read', path, str(size), str(offset))
        cache_data = self._get_cache_dir(path, 'cache.data')
        data_cache_range = self._get_cache_dir(path, 'cache.data.range')

        # list of Range objects indicating which chunks of the requested data
        # we have not yet cached and will need to get from the underlying fs
        blocks_to_read = []

        # Ranges object indicating which chunks of the file we have cached
        cached_blocks = None
        if os.path.exists(data_cache_range):
            with __builtin__.open(data_cache_range, 'rb') as f:
                debug('  loading cached_blocks from file')
                cached_blocks = pickle.load(f)
        else:
            cached_blocks = Ranges()

        requested_range = Range(offset, offset+size)

        debug('   read', 'path=' + path, 'size=' + str(size), 'offset=' + str(offset))
        debug('   requested_range', requested_range)
        debug('   cached_blocks', cached_blocks)

        blocks_to_read = cached_blocks.get_uncovered_portions(requested_range)

        debug('   blocks_to_read', blocks_to_read)

        # First, create the cache file if it does not exist already
        if not os.path.exists(cache_data):
            # We create a file full of zeroes the same size as the real file
            file_stat = self.getattr(path)
            self._create_cache_dir(path)

            with __builtin__.open(cache_data, 'wb') as f:
                debug('  creating blank file, size', str(file_stat.st_size))
                f.seek(file_stat.st_size - 1)
                f.write('\0')

                #for i in range(1, file_stat.st_size):
                #    f.write('\0')

        # If there are no blocks_to_read, then don't bother opening
        # the cache_data file for updates or dumping our cached_blocks.
        # This will slightly improve performance when getting data which
        # is already in the cache.
        if len(blocks_to_read) > 0:

            # Now open it up in update mode so we can add data to it as
            # we read the data from the underlying filesystem
            with __builtin__.open(cache_data, 'r+b') as cache_data_file:

                # Now loop through all the blocks we need to get
                # and append them to the cached file as we go
                for block in blocks_to_read:
                    block_data = self.underlying_fs.read(path, block.size, block.start)

                    cached_blocks.add_range(block)

                    cache_data_file.seek(block.start)
                    cache_data_file.write(block_data) # overwrites existing data in the file

            # update our cached_blocks file
            with __builtin__.open(data_cache_range, 'wb') as f:
                pickle.dump(cached_blocks, f)

        # Now we have loaded all the data we need to into the cache, we do the read
        # from the cached file
        result = None
        with __builtin__.open(cache_data, 'rb') as f:
            f.seek(offset)
            result = f.read(size)

        debug('  returning result from cache', type(result), len(result))
        return result

    """
    List the given directory, from the cache
    """
    def readdir(self, path, offset):
        cache_dir = self._get_cache_dir(path, 'cache.list')

        result = None
        if os.path.exists(cache_dir):
            debug('cacher.readdir getting from cache', path)
            with __builtin__.open(cache_dir, 'rb') as list_cache_file:
                result = pickle.load(list_cache_file)

        else:
            debug('cacher.readdir asking ufs for listing', path)
            result_generator = self.underlying_fs.readdir(path, offset)
            result = list(result_generator)

            self._create_cache_dir(path)
            with __builtin__.open(cache_dir, 'wb') as list_cache_file:
                pickle.dump(result, list_cache_file)

        # Return a new generator over our list of items
        return (x for x in result)

    """
    Retrieve stat information for a particular file from the cache
    """
    def getattr(self, path):
        cache_dir = self._get_cache_dir(path, 'cache.stat')

        result = None
        if os.path.exists(cache_dir):
            with __builtin__.open(cache_dir, 'rb') as stat_cache_file:
                result = pickle.load(stat_cache_file)
                debug('cacher.getattr', 'fetching from cache', path)

        else:
            result = self.underlying_fs.getattr(path)
            debug('cacher.getattr getting from filesystem', path)

            self._create_cache_dir(path)
            with __builtin__.open(cache_dir, 'wb') as stat_cache_file:
                pickle.dump(result, stat_cache_file)

        return result

    def write(self, path, buf, offset):
        return -errno.ENOSYS

    """
    # For a given path, return the name of the directory used to cache data for that path
    """
    def _get_cache_dir(self, path, file = None):
        if path[0] != '/':
            raise ValueError("Expected leading slash")

        if file == None:
            return os.path.join(self.cachedir, path[1:])
        else:
            return os.path.join(self.cachedir, path[1:], file)

    """
    # Create the cache path for the given directory if it does not already exist
    """
    def _create_cache_dir(self, path):
        cache_dir = self._get_cache_dir(path)
        self._mkdir(cache_dir)

    """
    # Create the given directory if it does not already exist
    """
    def _mkdir(self, path):
        debug('mkdir "' + path + '", os: ' + str(type(os)))
        if not os.path.exists(path):
            os.makedirs(path)

def main():
    usage="""
    pCacheFS: A persistently caching filesystem.
    """ + fuse.Fuse.fusage

    version = "%prog " + fuse.__version__

    server = PersistentCacheFs(version=version, usage=usage, dash_s_do='setsingle')

    #server.parser.set_conflict_handler('resolve') # enable overriding the --help message.
    #server.parser.add_option('-h', '--help', action='help', help="Display help")

    server.parse(errex=1)
    server.main()

if __name__ == '__main__':
    main()
