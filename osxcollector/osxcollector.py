# -*- coding: utf-8 -*-
#
#  OS X Collector
#  This work is licensed under the GNU General Public License
#  This work is a derivation of https://github.com/jipegit/OSXAuditor
#
#  Gathers information from plists, sqlite DBs, and the local filesystem to
#  get information for analyzing a malware infection.
#
#  Output to stdout is JSON.  Each line contains a key 'osxcollector_section' which
#  helps identify the line.  Many lines contain a key 'osxcollector_subsection' to
#  further filter output lines.
#
#  Fatal errors are written to stderr.  They can also be found in the JSON output as lines
#  with a key 'osxcollector_error'.
#
#  Non-fatal errors are only written to stderr when the --debug flag is passed to the script.
#  They can also be found in the JSON output as lines with a key 'osxcollector_warn'
#
# TODO:
# * Try to kill off processes like chrome and firefox
# * Normalize/sanitize timezones
# * Process NSDates in plists
#

import Foundation
import calendar
import os
import sys
import shutil

from collections import namedtuple
from datetime import datetime
from datetime import timedelta
from functools import partial
from hashlib import md5
from hashlib import sha1
from hashlib import sha256
from json import dumps
from numbers import Number
from optparse import OptionParser
from sqlite3 import connect
from sqlite3 import OperationalError
from traceback import extract_tb


ROOT_PATH = '/'
"""Global root path to build all further paths off of"""

DEBUG_MODE = False
"""Global debug mode flag for whether to enable breaking into pdb"""

def debugbreak():
    """Break in debugger if global DEBUG_MODE is set"""
    global DEBUG_MODE

    if DEBUG_MODE:
        import pdb; pdb.set_trace()

HomeDir = namedtuple('HomeDir', ['user_name', 'path'])
"""A simple tuple for storing info about a user"""

def _get_homedirs():
    """Return a list of HomeDir objects

    Takes care of filtering out '.'

    :return: list of HomeDir
    """
    homedirs = []
    users_dir_path = pathjoin(ROOT_PATH, 'Users')
    for user_name in listdir(users_dir_path):
        if not user_name.startswith('.'):
            homedirs.append(HomeDir(user_name, pathjoin(ROOT_PATH, 'Users', user_name)))
    return homedirs

def listdir(dir_path):
    """Safe version of os.listdir will always return an enumerable value

    Takes care of filtering out known useless dot files.

    :param dir_path: str path of directory to list
    :return: list of str
    """
    if not os.path.isdir(dir_path):
        return []

    ignored_files = ['.DS_Store', '.localized']
    return [val for val in os.listdir(dir_path) if val not in ignored_files]

def _relative_path(path):
    if path.startswith('/'):
        return path[1:]
    return path

def pathjoin(path, *args):
    """Version of os.path.join that assumes every argument after the first is a relative path

    :param path: The first path part
    :param args: A list of further paths
    :return" string of joined paths
    """
    if args:
        normed_args = [_relative_path(arg) for arg in args]
        return os.path.join(path, *normed_args)
    else:
        return os.path.join(path)

def _hash_file(file_path):
    """Return the md5, sha1, sha256 hash of a file.

    :param file_path: str path of file to hash
    :return: list of 3 hex strings.  Empty strings on failure.
    """
    hashers = [
        md5(),
        sha1(),
        sha256()
    ]

    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(partial(f.read, 1024*1024), ''):
                for hasher in hashers:
                    hasher.update(chunk)

            return [hasher.hexdigest() for hasher in hashers]
    except:
        debugbreak()
        return ['', '', '']

DATETIME_2001 = datetime(2001, 1, 1)
"""Constant to use for converting timestamps to strings"""
DATETIME_1970 = datetime(1970, 1, 1)
"""Constant to use for converting timestamps to strings"""
DATETIME_1601 = datetime(1601, 1, 1)
"""Constant to use for converting timestamps to strings"""
MIN_YEAR = 2004

def _timestamp_errorhandling(func):
    """Timestamps that are less than MIN_YEAR or after the current date are invalid"""
    def wrapper(*args, **kwargs):
        try:
            dt = func(*args, **kwargs)
            tomorrow = datetime.now() + timedelta(days=1) # just in case of some timezone issues
            if dt.year < MIN_YEAR or dt > tomorrow:
                return None
            return dt
        except:
            return None

    return wrapper

def _convert_to_local(func):
    '''UTC to local time conversion
    source: http://feihonghsu.blogspot.com/2008/02/converting-from-local-time-to-utc.html
    '''
    def wrapper(*args, **kwargs):
        dt = func(*args, **kwargs)
        return datetime.fromtimestamp(calendar.timegm(dt.timetuple()))

    return wrapper

@_timestamp_errorhandling
@_convert_to_local
def _seconds_since_2001_to_datetime(seconds):
    return DATETIME_2001 + timedelta(seconds=seconds)

@_timestamp_errorhandling
@_convert_to_local
def _seconds_since_epoch_to_datetime(seconds):
    """Converts timestamp to datetime assuming the timestamp is expressed in seconds since epoch"""
    return DATETIME_1970 + timedelta(seconds=seconds)

@_timestamp_errorhandling
@_convert_to_local
def _microseconds_since_epoch_to_datetime(microseconds):
    return DATETIME_1970 + timedelta(microseconds=microseconds)

@_timestamp_errorhandling
@_convert_to_local
def _microseconds_since_1601_to_datetime(microseconds):
    return DATETIME_1601 + timedelta(microseconds=microseconds)

def _value_to_datetime(val):
    # Try various versions of converting a number to a datetime.
    # Ordering is important as a timestamp may be "valid" with multiple different conversion algorithms
    # but it won't necessarilly be the correct timestamp
    if (isinstance(val, basestring)):
        try:
            val = float(val)
        except:
            return None

    return (_microseconds_since_epoch_to_datetime(val) or _microseconds_since_1601_to_datetime(val) or
            _seconds_since_epoch_to_datetime(val) or _seconds_since_2001_to_datetime(val))

def _datetime_to_string(dt):
    try:
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        debugbreak()
        return None

def _get_file_info(file_path):
    """Gather info about a file including hash and dates

    :param file_path: str path of file to hash
    :return: dict with key ['md5', 'sha1', 'sha2', file_path', 'mtime', 'ctime']
    """
    md5_hash, sha1_hash, sha2_hash = '', '', ''
    mtime = ''
    ctime = ''

    if os.path.isfile(file_path):
        mtime = _datetime_to_string(datetime.fromtimestamp(os.path.getmtime(file_path)))
        ctime = _datetime_to_string(datetime.fromtimestamp(os.path.getctime(file_path)))
        md5_hash, sha1_hash, sha2_hash = _hash_file(file_path)

    return {
        'md5':       md5_hash,
        'sha1':      sha1_hash,
        'sha2':      sha2_hash,
        'file_path': file_path,
        'mtime':     mtime,
        'ctime':     ctime
    }

def _normalize_val(val, key=None):
    """Transform a value read from SqlLite or a plist into a string
    Special case handling deals with things derived from basestring, buffer, or numbers.Number

    :param val: A value of any type
    :param key: The key associated with the value.  Will attempt to convert timestamps to a date
        based on the key name
    :returns: A string
    """
    # If the key hints this is a timestamp, try to use some popular formats
    if key and any([-1 != key.lower().find(hint) for hint in ['time', 'utc', 'date', 'accessed']]):
        ts = _value_to_datetime(val)
        if ts:
            return _datetime_to_string(ts)

    try:
        if isinstance(val, basestring):
            try:
                return unicode(val).decode(encoding='utf-8', errors='ignore')
            except UnicodeEncodeError:
                return val
        elif isinstance(val, buffer):
            # Not all buffers will contain text so this is expected to fail
            try:
                return unicode(val).decode(encoding='utf-16le', errors='ignore')
            except:
                return repr(val)
        elif isinstance(val, Number):
            return val
        elif isinstance(val, Foundation.NSData):
            return '<NSData bytes:{0}>'.format(val.length())
        elif isinstance(val, Foundation.NSArray):
            return [_normalize_val(stuff) for stuff in val]
        elif isinstance(val, Foundation.NSDictionary) or isinstance(val, dict):
            return dict([(k, _normalize_val(val.get(k), k)) for k in val.keys()])
        elif isinstance(val, Foundation.NSDate):
            # NSDate could have special case handling
             return repr(val)
        elif not val:
            return ''
        else:
            debugbreak()
            return repr(val)
    except Exception as normalize_val_e:
        to_print = '[ERROR] _normalize_val {0}\n'.format(repr(normalize_val_e))
        sys.stderr.write(to_print)

        debugbreak()
        return repr(val)

class DictUtils(object):
    """A set of method for manipulating dictionaries."""

    @classmethod
    def _link_path_to_chain(cls, path):
        """Helper method for get_deep

        :param path: A str representing a chain of keys seperated '.' or an enumerable set of strings
        :return: an enumerable set of strings
        """
        if path == '':
            return []
        elif type(path) in (list, tuple, set):
            return path
        else:
            return path.split('.')

    @classmethod
    def _get_deep_by_chain(cls, x, chain, default=None):
        """Grab data from a dict using a ['key1', 'key2', 'key3'] chain param to do deep traversal.

        :param x: A dict
        :param chain: an enumerable set of strings
        :param default: A value to return if the path can not be found
        :return: The value of the key or default
        """
        if chain == []:
            return default
        try:
            for link in chain:
                try:
                    x = x[link]
                except (KeyError, TypeError):
                    x = x[int(link)]
        except (KeyError, TypeError, ValueError):
            x = default
        return x

    @classmethod
    def get_deep(cls, x, path='', default=None):
        """Grab data from a dict using a 'key1.key2.key3' path param to do deep traversal.

        :param x: A dict
        :param path: A 'deep path' to retreive in the dict
        :param default: A value to return if the path can not be found
        :return: The value of the key or default
        """
        chain = cls._link_path_to_chain(path)
        return cls._get_deep_by_chain(x, chain, default=default)


class Logger(object):
    """Logging class writes JSON to stdout and stderr

    Additionally, the Logger allows for "extra" key/value pairs to be set.  These will then
    be tacked onto each line logged.  Use the Logger.Extra context manager to set an "extra".

    .. code-block:: python
        with Logger.Extra(extra_key, val):
            # Everything logged in this context will have {'extra_key': val} inserted into output
    """

    output_file = sys.stdout
    ## File to write standard output to

    lines_written = 0
    ## Counter of lines of standard output written

    @classmethod
    def set_output_file(cls, output_file):
        cls.output_file = output_file

    @classmethod
    def log_dict(cls, record):
        """Splats out a JSON blob to stdout.

        :param record: a dict of data
        """
        record.update(Logger.Extra.extras)
        try:
            cls.output_file.write(dumps(record))
            cls.output_file.write('\n')
            cls.output_file.flush()
            cls.lines_written += 1
        except Exception as e:
            debugbreak()
            cls.log_exception(e)

    @classmethod
    def log_warning(cls, message):
        """Writes a warning message to JSON output and optionally splats a string to stderr if DEBUG_MODE.

        :param message: String with a warning message
        """
        global DEBUG_MODE

        cls.log_dict({'osxcollector_warn': message})
        if DEBUG_MODE:
            sys.stderr.write('[WARN] ')
            sys.stderr.write(message)
            sys.stderr.write(' - {0}\n'.format(repr(Logger.Extra.extras)))

    @classmethod
    def log_error(cls, message):
        """Writes a warning message to JSON output and to stderr.

        :param message: String with a warning message
        """
        cls.log_dict({'osxcollector_error': message})
        sys.stderr.write('[ERROR] ')
        sys.stderr.write(message)
        sys.stderr.write(' - {0}\n'.format(repr(Logger.Extra.extras)))

    @classmethod
    def log_exception(cls, e, message=''):
        """Splat out an Exception instance as a warning

        :param e: An instance of an Exception
        :param message: a str message to log with the Exception
        """
        exc_type, _, exc_traceback = sys.exc_info()

        to_print = '{0} {1} {2}'.format(message, exc_type, extract_tb(exc_traceback))
        cls.log_error(to_print)

    class Extra(object):
        """A context class for adding additional params to be logged with every line written by Logger"""

        extras = {}
        ## Class level dict for storing extras

        def __init__(self, key, val):
            self.key = key
            self.val = val

        def __enter__(self):
            global DEBUG_MODE
            Logger.Extra.extras[self.key] = self.val

            if DEBUG_MODE:
                sys.stderr.write(dumps({self.key:self.val}))
                sys.stderr.write('\n')


        def __exit__(self, type, value, traceback):
            del Logger.Extra.extras[self.key]

class Collector(object):
    """Examines plists, sqlite dbs, and hashes files to gather info useful for analyzing a malware infection"""

    def __init__(self):
        """Constructor

        :param logger: An instance of Logger
        """
        # A list of the names of accounts with admin priveleges
        self.admins = []

        # A list of HomeDir used when finding per-user data
        self.homedirs = _get_homedirs()

    def collect(self, section_list=None):
        """The primary public method for collecting data.

        :param section_list: OPTIONAL A list of strings with names of sections to collect.
        """

        sections = [
            ('system_info',     self._collect_system_info),
            ('kext',            self._collect_kext),
            ('startup',         self._collect_startup),
            ('applications',    self._collect_applications),
            ('quarantines',     self._collect_quarantines),
            ('downloads',       self._collect_downloads),
            ('chrome',          self._collect_chrome),
            ('firefox',         self._collect_firefox),
            ('safari',          self._collect_safari),
            ('accounts',        self._collect_accounts),
            ('mail',            self._collect_mail),
        ]

        for section_name, collection_method in sections:
            with Logger.Extra('osxcollector_section', section_name):
                if section_list and section_name not in section_list:
                    continue

                try:
                    collection_method()
                except Exception as section_e:
                    debugbreak()
                    Logger.log_exception(section_e, message='failed section')

    def _foreach_homedir(func):
        """A decorator to ensure a method is called for each user's homedir.


        As a side-effect, this adds the 'osxcollector_username' key to Logger output.
        """
        def wrapper(self, *args, **kwargs):
            for homedir in self.homedirs:
                with Logger.Extra('osxcollector_username', homedir.user_name):
                    try:
                        func(self, *args, homedir=homedir, **kwargs)
                    except Exception as e:
                        Logger.log_exception(e)

        return wrapper

    def _read_plist(self, plist_path):
        """Read a plist file and return a dict representing it.

        The return should be suitable for JSON serialization.

        :param plist_path: The path to the file to read.
        :return: a dict. Empty dict on failure.
        """
        if not os.path.isfile(plist_path):
            # TODO(ivanlei): Explore adding a warning here for missing plist files.  At the very least it might help
            # find some unnecessary attempts to read a directory as a plist
            return {}

        try:
            plist_nsdata, error_message = Foundation.NSData.dataWithContentsOfFile_options_error_(plist_path, Foundation.NSUncachedRead, None)
            plist_dictionary, plist_format, error_message = Foundation.NSPropertyListSerialization.propertyListFromData_mutabilityOption_format_errorDescription_(plist_nsdata, Foundation.NSPropertyListMutableContainers, None, None)
            return _normalize_val(plist_dictionary)
        except Exception as read_plist_e:
            Logger.log_exception(read_plist_e, message='_read_plist failed on {0}'.format(plist_path))

        return {}

    def _log_items_in_plist(self, plist, path, transform=None):
        """Dive into the dict representation of a plist and log all items under a specific path

        :param plist: A dict representation of a plist.
        :param path: A str which will be passed to get_deep()
        :param transform: An optional method for transforming each item before logging.
        """
        for item in DictUtils.get_deep(plist, path=path, default=[]):
            try:
                if transform:
                    item = transform(item)
                Logger.log_dict(item)
            except Exception as log_items_in_plist_e:
                Logger.log_exception(log_items_in_plist_e)

    def _log_file_info_for_directory(self, dir_path, recurse=False):
        """Logs file information for every file in a directory"""
        if not os.path.isdir(dir_path):
            Logger.log_warning('Directory not found {0}'.format(dir_path))
            return

        for root, _, file_names in os.walk(dir_path):
            for file_name in file_names:
                try:
                    file_path = pathjoin(root, file_name)
                    file_info = _get_file_info(file_path)
                    Logger.log_dict(file_info)
                except Exception as log_file_info_for_directory_e:
                    Logger.log_exception(log_file_info_for_directory_e)

    @_foreach_homedir
    def _log_user_quarantines(self, homedir):
        """Log the quarantines for a user

        Quarantines is basically the info necessary to show the 'Are you sure you wanna run this?' when
        a user is trying to open a file downloaded from the internet.  For some more details, checkout the
        Apple Support explanation of Quarantines: http://support.apple.com/kb/HT3662
        """

        # OS X >= 10.7
        db_path = pathjoin(homedir.path, 'Library/Preferences/com.apple.LaunchServices.QuarantineEventsV2')
        if not os.path.isfile(db_path):
            # OS X <= 10.6
            db_path = pathjoin(homedir.path, 'Library/Preferences/com.apple.LaunchServices.QuarantineEvents')

        self._log_sqlite_db(db_path)

    def _log_xprotect(self):
        """XProtect adds hash-based malware checking to quarantine files. The plist for XProtect is at:
        /System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/XProtect.plist

        XProtect also add minimum versions for Internet Plugins. That plist is at:
        /System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/XProtect.meta.plist
        """
        xprotect_files = [
            'System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/XProtect.plist',
            'System/Library/CoreServices/CoreTypes.bundle/Contents/Resources/XProtect.meta.plist',
        ]

        for file_path in xprotect_files:
            file_info = _get_file_info(pathjoin(ROOT_PATH, file_path))
            Logger.log_dict(file_info)

    def _log_packages_in_dir(self, dir_path):
        """Log the packages in a directory"""
        plist_file = 'Info.plist'

        walk = [(sub_dir_path, file_names) for sub_dir_path, _, file_names in os.walk(dir_path) if any([sub_dir_path.endswith(extension) for extension in ['.app', '.kext', '.osax', 'Contents']])]
        for sub_dir_path, file_names in walk:
            if plist_file in file_names:
                if sub_dir_path.endswith('Contents'):
                    cfbundle_executable_path = 'MacOS'
                else:
                    cfbundle_executable_path = ''

            plist_path = pathjoin(sub_dir_path, plist_file)
            plist = self._read_plist(plist_path)
            cfbundle_executable = plist.get('CFBundleExecutable')
            if cfbundle_executable:
                file_path = pathjoin(sub_dir_path, cfbundle_executable_path, cfbundle_executable)
                file_info = _get_file_info(file_path)
                file_info['osxcollector_plist_path'] = plist_path
                Logger.log_dict(file_info)

    def _log_startup_items(self, dir_path):
        """Log the startup_item plist and hash its program argument

        Startup items are launched in the final phase of boot.  See more at:
        https://developer.apple.com/library/mac/documentation/macosx/conceptual/bpsystemstartup/chapters/StartupItems.html

        The 'Provides' element of the plist is an array of services provided by the startup item.
        _log_startup_items treats each element of 'Provides' as a the name of a file and attempts to hash it.
        """
        if not os.path.isdir(dir_path):
            Logger.log_warning('Directory not found {0}'.format(dir_path))
            return

        for entry in listdir(dir_path):
            plist_path = pathjoin(dir_path, entry, 'StartupParameters.plist')
            plist = self._read_plist(plist_path)

            try:
                self._log_items_in_plist(plist, 'Provides',
                    transform=lambda x: _get_file_info(pathjoin(dir_path, entry, x)))
            except Exception as log_startup_items_e:
                Logger.log_exception(log_startup_items_e)

    def _log_launch_agents(self, dir_path):
        """Log a LaunchAgent plist and hash the program it runs.

        The plist for a launch agent is described at:
        https://developer.apple.com/library/mac/documentation/Darwin/Reference/ManPages/man5/launchd.plist.5.html

        In addition to hashing the program, _log_launch_agents will attempt to look for suspicious program arguments in
        the launch agent.  Check the 'suspicious' key in the output to identify suspicious launch agents.
        """
        if not os.path.isdir(dir_path):
            Logger.log_warning('Directory not found {0}'.format(dir_path))
            return

        for entry in listdir(dir_path):
            plist_path = pathjoin(dir_path, entry)
            plist = self._read_plist(plist_path)

            try:
                program = plist.get('Program', '')
                program_with_arguments = plist.get('ProgramArguments', [])
                if program or len(program_with_arguments):
                    file_path = pathjoin(ROOT_PATH, program or program_with_arguments[0])

                    file_info = _get_file_info(file_path)
                    file_info['label'] = plist.get('Label')
                    file_info['program'] = file_path
                    file_info['osxcollector_plist'] = plist_path
                    if len(program_with_arguments) > 1:
                        file_info['arguments'] = list(program_with_arguments)[1:]
                    Logger.log_dict(file_info)
            except Exception as log_launch_agents_e:
                Logger.log_exception(log_launch_agents_e)

    @_foreach_homedir
    def _log_user_launch_agents(self, homedir):
        path = pathjoin(homedir.path, 'Library/LaunchAgents/')
        self._log_launch_agents(path)

    @_foreach_homedir
    def _log_user_login_items(self, homedir):
        """Log the login items for a user

        Login items are startup items that open automatically when a user logs in.
        They are visible in 'System Preferences'->'Users & Groups'->'Login Items'

        The name of the item is in 'SessionItems.CustomListItems.Name'
        The application to launch is in 'SessionItems.CustomListItems.Alias' but this appears to be a binary structure which is hard to read.
        """

        plist_path = pathjoin(homedir.path, 'Library/Preferences/com.apple.loginitems.plist')
        plist = self._read_plist(plist_path)
        self._log_items_in_plist(plist, 'SessionItems.CustomListItems')

    def _collect_system_info(self):
        """Collect basic info about the system and system logs"""

        # Basic OS info
        sysname, nodename, release, version, machine = os.uname()
        record = {
            'sysname': sysname,
            'nodename': nodename,
            'release': release,
            'version': version,
            'machine': machine
        }
        Logger.log_dict(record)

    def _collect_startup(self):
        """Log the different LauchAgents and LaunchDaemons"""

        # http://www.malicious-streams.com/article/Mac_OSX_Startup.pdf
        launch_agents = [
            'System/Library/LaunchAgents',
            'System/Library/LaunchDaemons',
            'Library/LaunchAgents',
            'Library/LaunchDaemons',
        ]
        with Logger.Extra('osxcollector_subsection', 'launch_agents'):
            for dir_path in launch_agents:
                self._log_launch_agents(pathjoin(ROOT_PATH, dir_path))
            self._log_user_launch_agents()

        packages = [
            'System/Library/ScriptingAdditions',
            'Library/ScriptingAdditions',
        ]
        with Logger.Extra('osxcollector_subsection', 'scripting_additions'):
            for dir_path in packages:
                self._log_packages_in_dir(pathjoin(ROOT_PATH, dir_path))

        startup_items = [
            'System/Library/StartupItems',
            'Library/StartupItems',
        ]
        with Logger.Extra('osxcollector_subsection', 'startup_items'):
            for dir_path in startup_items:
                self._log_startup_items(pathjoin(ROOT_PATH, dir_path))

        with Logger.Extra('osxcollector_subsection', 'login_items'):
            self._log_user_login_items()

    def _collect_quarantines(self):
        """Log quarantines and XProtect hash-based malware checking definitions
        """
        self._log_user_quarantines()
        self._log_xprotect()

    @_foreach_homedir
    def _collect_downloads(self, homedir):
        """Hash all users's downloaded files"""

        directories_to_hash = [
            ('downloads',           'Downloads'),
            ('email_downloads',     'Library/Mail Downloads'),
            ('old_email_downloads', 'Library/Containers/com.apple.mail/Data/Library/Mail Downloads')
        ]

        for subsection_name, path_to_dir in directories_to_hash:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                dir_path = pathjoin(homedir.path, path_to_dir)
                self._log_file_info_for_directory(dir_path)

    def _log_sqlite_table(self, table_name, cursor):
        """Dump a SQLite table

        :param table_name: The name of the table to dump
        :param cursor: sqlite3 cursor object
        """
        with Logger.Extra('osxcollector_table_name', table_name):

            try:
                # Grab the whole table
                cursor.execute('SELECT * from {0}'.format(table_name))
                rows = cursor.fetchall()
                if not len(rows):
                    return

                # Grab the column descriptions
                column_descriptions = [col[0] for col in cursor.description]

                # Splat out each record
                for row in rows:
                    record = dict([(key, _normalize_val(val, key)) for key, val in zip(column_descriptions, row)])
                    Logger.log_dict(record)

            except Exception as per_table_e:
                Logger.log_exception(per_table_e,
                    message='failed _log_sqlite_table')

    def _log_sqlite_db(self, sqlite_db_path):
        """Dump a SQLite database file as JSON.

        :param sqlite_db_path: The path to the SqlLite file
        """
        if not os.path.isfile(sqlite_db_path):
            Logger.log_warning('File not found {0}'.format(sqlite_db_path))
            return

        with Logger.Extra('osxcollector_db_path', sqlite_db_path):

            # Connect and get all table names
            try:
                with connect(sqlite_db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT * from sqlite_master WHERE type = "table"')
                    tables =  cursor.fetchall()
                    table_names = [table[2] for table in tables]

                    for table_name in table_names:
                        self._log_sqlite_table(table_name, cursor)

            except Exception as connection_e:
                if isinstance(connection_e, OperationalError) and -1 != connection_e.message.find('locked'):
                    Logger.log_error('!!LOCKED DB!! DID YOU FORGET TO CLOSE CHROME?')

                Logger.log_exception(connection_e,
                    message='failed _log_sqlite_db')

    @_foreach_homedir
    def _collect_firefox(self, homedir):
        """Log the different SQLite databases in a Firefox profile"""
        #Most useful See http://kb.mozillazine.org/Profile_folder_-_Firefox

        all_profiles_path = pathjoin(homedir.path, 'Library/Application Support/Firefox/Profiles')
        if not os.path.isdir(all_profiles_path):
            Logger.log_warning('Directory not found {0}'.format(all_profiles_path))
            return

        for profile_name in listdir(all_profiles_path):
            profile_path = pathjoin(all_profiles_path, profile_name)

            sqlite_dbs = [
                ('cookies',       'cookies.sqlite'),
                ('downloads',     'downloads.sqlite'),
                ('formhistory',   'formhistory.sqlite'),
                ('history',       'places.sqlite'),
                ('signons',       'signons.sqlite'),
                ('permissions',   'permissions.sqlite'),
                ('addons',        'addons.sqlite'),
                ('extension',     'extensions.sqlite'),
                ('content_prefs', 'content-prefs.sqlite'),
                ('health_report', 'healthreport.sqlite'),
                ('webapps_store', 'webappsstore.sqlite'),
            ]

            for subsection_name, db_name in sqlite_dbs:
                with Logger.Extra('osxcollector_subsection', subsection_name):
                    self._log_sqlite_db(pathjoin(profile_path, db_name))

    @_foreach_homedir
    def _collect_safari(self, homedir):
        """Log the different plist and SQLite databases in a Safari profile"""

        profile_path = pathjoin(homedir.path, 'Library/Safari')
        if not os.path.isdir(profile_path):
            Logger.log_warning('Directory not found {0}'.format(profile_path))
            return

        plists = [
            ('downloads',    'Downloads.plist', 'DownloadHistory'),
            ('history',      'History.plist',   'WebHistoryDates'),
        ]

        for subsection_name, plist_name, key_to_log in plists:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                plist_path = pathjoin(profile_path, plist_name)
                plist = self._read_plist(plist_path)
                self._log_items_in_plist(plist, key_to_log)

        directories_of_dbs = [
            ('databases',    'Databases'),
            ('localstorage', 'LocalStorage')
        ]
        for subsection_name, dir_name in directories_of_dbs:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                dir_path = pathjoin(profile_path, dir_name)
                for db in listdir(dir_path):
                    self._log_sqlite_db(pathjoin(dir_path, db))

    @_foreach_homedir
    def _collect_chrome(self, homedir):
        """Log the different files in a Chrome profile"""

        chrome_path = pathjoin(homedir.path, 'Library/Application Support/Google/Chrome/Default')
        if not os.path.isdir(chrome_path):
            Logger.log_warning('Directory not found {0}'.format(chrome_path))
            return

        sqlite_dbs = [
            ('history',          'History'),
            ('archived_history', 'Archived History'),
            ('cookies',          'Cookies'),
            ('login_data',       'Login Data'),
            ('top_sites',        'Top Sites'),
            ('web_data',         'Web Data')
        ]
        for subsection_name, db_name in sqlite_dbs:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                self._log_sqlite_db(pathjoin(chrome_path, db_name))

        directories_of_dbs = [
            ('databases',     'databases'),
            ('local_storage', 'Local Storage')
        ]
        for subsection_name, dir_name in directories_of_dbs:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                dir_path = pathjoin(chrome_path, dir_name)
                for db in listdir(dir_path):
                    db_path = pathjoin(dir_path, db)
                    # Files ending in '-journal' are encrypted
                    if not db_path.endswith('-journal') and not os.path.isdir(db_path):
                        self._log_sqlite_db(db_path)

    def _collect_kext(self):
        """Log the Kernel extensions"""
        self._log_packages_in_dir(pathjoin(ROOT_PATH, 'System/Library/Extensions/'))

    def _collect_accounts(self):
        """Log users's accounts"""
        accounts = [
            ('system_admins',   self._collect_accounts_system_admins),
            ('system_users',    self._collect_accounts_system_users),
            ('social_accounts', self._collect_accounts_social_accounts),
            ('recent_items',    self._collect_accounts_recent_items)
        ]
        for subsection_name, collector in accounts:
            with Logger.Extra('osxcollector_subsection', subsection_name):
                collector()

    def _collect_accounts_system_admins(self):
        """Log the system admins group db"""
        sys_admin_plist_path = pathjoin(ROOT_PATH, 'private/var/db/dslocal/nodes/Default/groups/admin.plist')
        sys_admin_plist = self._read_plist(sys_admin_plist_path)

        for admin in sys_admin_plist.get('groupmembers', []):
            self.admins.append(admin)
        for admin in sys_admin_plist.get('users', []):
            self.admins.append(admin)

        Logger.log_dict({'admins': self.admins})

    def _collect_accounts_system_users(self):
        """Log the system users db"""
        for user_name in listdir(pathjoin(ROOT_PATH, 'private/var/db/dslocal/nodes/Default/users')):
            if user_name[0].startswith('.'):
                continue

            user_details = {}

            sys_user_plist_path = pathjoin(ROOT_PATH, 'private/var/db/dslocal/nodes/Default/users', user_name)
            sys_user_plist = self._read_plist(sys_user_plist_path)

            user_details['names'] = [{'name': val, 'is_admin': (val in self.admins)} for val in sys_user_plist.get('name', [])]
            user_details['realname'] = [val for val in sys_user_plist.get('realname', [])]
            user_details['shell'] = [val for val in sys_user_plist.get('shell', [])]
            user_details['home'] = [val for val in sys_user_plist.get('home', [])]
            user_details['uid'] = [val for val in sys_user_plist.get('uid', [])]
            user_details['gid'] = [val for val in sys_user_plist.get('gid', [])]
            user_details['generateduid'] = [{'name': val, 'is_admin': (val in self.admins)} for val in sys_user_plist.get('generateduid', [])]

            Logger.log_dict(user_details)

    @_foreach_homedir
    def _collect_accounts_social_accounts(self, homedir):
        user_accounts_path = pathjoin(homedir.path, 'Library/Accounts/Accounts3.sqlite')
        self._log_sqlite_db(user_accounts_path)

    @_foreach_homedir
    def _collect_accounts_recent_items(self, homedir):
        """Log users' recents items"""

        recent_items_account_plist_path = pathjoin(homedir.path, 'Library/Preferences/com.apple.recentitems.plist')

        recents_plist = self._read_plist(recent_items_account_plist_path)

        recents = [
            ('server',      'RecentServers'),
            ('document',    'RecentDocuments'),
            ('application', 'RecentApplications'),
            ('host',        'Hosts')
        ]

        for recent_type, recent_key in recents:
            with Logger.Extra('recent_type', recent_type):
                for recent in DictUtils.get_deep(recents_plist, '{0}.CustomListItems'.format(recent_key), []):
                    recent_details = {'{0}_name'.format(recent_type): recent['Name']}
                    if recent_type == 'host':
                        recent_details['host_url'] = recent['URL']
                    Logger.log_dict(recent_details)

    @_foreach_homedir
    def _collect_user_applications(self, homedir):
        """Hashes installed apps in the user's ~/Applications directory"""
        self._log_packages_in_dir(pathjoin(homedir.path, 'Applications'))

    def _collect_applications(self):
        """Hashes installed apps in and gathers install history"""

        with Logger.Extra('osxcollector_subsection', 'applications'):
            # Hash all files in /Applications
            self._log_packages_in_dir(pathjoin(ROOT_PATH, 'Applications'))
            # Hash all files in ~/Applications
            self._collect_user_applications()

        # Read the installed applications history
        with Logger.Extra('osxcollector_subsection', 'install_history'):
            plist = self._read_plist(pathjoin(ROOT_PATH, 'Library/Receipts/InstallHistory.plist'))
            for installed_app in plist:
                Logger.log_dict(installed_app)

    @_foreach_homedir
    def _collect_mail(self, homedir):
        """Hashes file in the mail app directories"""
        mail_paths = [
            'Library/Mail',
            'Library/Mail Downloads'
        ]
        for mail_path in mail_paths:
            self._log_file_info_for_directory(pathjoin(homedir.path, mail_path))

class LogFileArchiver(object):

    def archive_logs(self, target_dir_path):
        """Main method for archiving files

        :param target_dir_path: Path the directory files should be archived to
        """
        log_dir_path = pathjoin(ROOT_PATH, 'private/var/log')

        system_log_file_names = [file_name for file_name in listdir(log_dir_path) if file_name.startswith('system.')]
        for file_name in system_log_file_names:
            src = pathjoin(log_dir_path, file_name)
            dst = pathjoin(target_dir_path, file_name)
            try:
                shutil.copyfile(src, dst)
            except Exception as archive_e:
                debugbreak()
                Logger.log_exception(archive_e)

    def compress_directory(self, file_name, output_dir_path, target_dir_path):
        """Compress a directory into a .tar.gz

        :param file_name: The name of the .tar.gz to file to create.  Do not include the extension.
        :param output_dir_path: The directory to place the output file in.
        :param target_dir_path: The directory to compress
        """
        try:
            # Zip the whole thing up
            shutil.make_archive(file_name, format='gztar', root_dir=output_dir_path, base_dir=target_dir_path)
        except Exception as compress_directory_e:
            debugbreak()
            Logger.log_exception(compress_directory_e)

def main():

    global DEBUG_MODE
    global ROOT_PATH

    euid = os.geteuid()
    egid = os.getegid()

    parser = OptionParser(usage='usage: %prog [options]')
    parser.add_option('-i', '--id', dest='incident_prefix', default='osxcollect', help='[OPTIONAL] An identifier which will be added as a prefix to the output file name.')
    parser.add_option('-o', '--outputfile', dest='output_file_name', default=None, help='[OPTIONAL] Name of the output file. Default name uses the timestamp. Try \'/dev/stdout\' for fun!')
    parser.add_option('-p', '--path', dest='rootpath', default='/', help='[OPTIONAL] Path to the OS X system to audit (e.g. /mnt/xxx). The running system will be audited if not specified.')
    parser.add_option('-s', '--section', dest='section_list', default=[], action='append', help='[OPTIONAL] Just run the named section.  May be specified more than once.')
    parser.add_option('-d', '--debug', action='store_true', default=False, help='[OPTIONAL] Enable verbose output and python breakpoints.')
    options, _ = parser.parse_args()

    DEBUG_MODE = options.debug
    ROOT_PATH = options.rootpath

    if ROOT_PATH == '/' and (euid != 0 and egid != 0):
        Logger.log_error('Must run as root!\n')
        return

    # Create an incident ID
    prefix = options.incident_prefix
    incident_id = '{0}-{1}'.format(prefix, datetime.now().strftime('%Y_%m_%d-%H_%M_%S'))

    # Make a directory named for the output
    output_directory = './{0}'.format(incident_id)
    os.makedirs(output_directory)

    # Create an output file name
    output_file_name = options.output_file_name or pathjoin(output_directory, '{0}.json'.format(incident_id))

    # Collect information from plists and sqlite dbs and such
    with open(output_file_name, 'w') as output_file:
        Logger.set_output_file(output_file)
        with Logger.Extra('osxcollector_incident_id', incident_id):
            Collector().collect(section_list=options.section_list)

        # Archive log files
        log_file_archiver = LogFileArchiver()
        log_file_archiver.archive_logs(output_directory)
        log_file_archiver.compress_directory(incident_id, '.', output_directory)

        if not DEBUG_MODE:
            try:
                shutil.rmtree(output_directory)
            except Exception as e:
                Logger.log_exception(e)

    # Output message to the user
    sys.stderr.write('Wrote {0} lines.\nOutput in {1}.tar.gz\n'.format(Logger.lines_written, incident_id))


if __name__ == '__main__':
    main()
