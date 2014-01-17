#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import sys
import os
import locale
import urllib2
import json
import shutil
import subprocess
import re
import string
import logging
import dulwich.repo
import dulwich.config
from curses import setupterm, tigetstr, tigetnum, tparm
from time import time
import argparse

# force utf-8 encoding
reload(sys)
sys.setdefaultencoding('utf-8')

logging.basicConfig()
logger = logging.getLogger(__name__)

# constants
HOOK_NAME = 'commit-msg'
HOOK_DIR = os.path.dirname(__file__)
ORIGINAL_HOOK_SUFFIX = '.original'

# non-editable constants
HOOK_PATH = os.path.join(HOOK_DIR, HOOK_NAME)
ORIGINAL_HOOK_NAME = HOOK_NAME + ORIGINAL_HOOK_SUFFIX

# tty colors
DEFAULT = '\x1b[39m'
BLACK, RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, LIGHT_GREY = [('\x1b[%dm' % (30 + i)) for i in range(8)]
GREY, BRIGHT_RED, BRIGHT_GREEN, BRIGHT_YELLOW, BRIGHT_BLUE, BRIGHT_MAGENTA, BRIGHT_CYAN, WHITE = [('\x1b[%dm' % (90 + i)) for i in range(8)]
RESET, NORMAL, BOLD, DIM, UNDERLINE, INVERT, HIDDEN = [('\x1b[%dm' % i) for i in (0, 22, 1, 2, 4, 7, 8)]
ATTRS = {
    'DEFAULT': DEFAULT,
    'BLACK': BLACK, 'RED': RED, 'GREEN': GREEN, 'YELLOW': YELLOW, 'BLUE': BLUE, 'MAGENTA': MAGENTA, 'CYAN': CYAN, 'LIGHT_GREY': LIGHT_GREY,
    'GREY': GREY, 'BRIGHT_RED': BRIGHT_RED, 'BRIGHT_GREEN': BRIGHT_GREEN, 'BRIGHT_YELLOW': BRIGHT_YELLOW, 'BRIGHT_BLUE': BRIGHT_BLUE, 'BRIGHT_MAGENTA': BRIGHT_MAGENTA, 'BRIGHT_CYAN': BRIGHT_CYAN, 'WHITE': WHITE,
    'RESET': RESET, 'NORMAL': NORMAL, 'BOLD': BOLD, 'DIM': DIM, 'UNDERLINE': UNDERLINE, 'INVERT': INVERT, 'HIDDEN': HIDDEN
}

ITEM_COLORS = {
    'story': 'GREEN',
    'task': 'GREY',
    'defect': 'RED',
    'test': 'CYAN'
}

ITEM_STATUSES = {
    'backlog': "Backlog",
    'in-progress': "In Progress",
    'completed': "Completed",
    'accepted': "Accepted",
}

class SprintlyTool:
    """
    A command line tool for displaying your stories, tasks, tests, and defects
    from Sprint.ly.
    """

    def __init__(self, term_stream=sys.stdout):
        """
        Initialize instance variables.
        """

        # Set up terminal
        locale.setlocale(locale.LC_ALL, '')
        self._encoding = locale.getpreferredencoding()

        self._term = term_stream or sys.__stdout__
        self._is_tty = False
        self._has_color = False
        self._cols = 80

        if hasattr(term_stream, "isatty") and term_stream.isatty():
            try:
                setupterm()
                self._is_tty = True
                self._has_color = tigetnum('colors') > 2
                self._cols = tigetnum('cols')
            except:
                pass
        else:
            try:
                (_, columns) = os.popen('stty size', 'r').read().split()
                self._cols = int(columns)
            except:
                pass

        self._cache = None
        self._sprintlyDirectoryPath = None
        self._sprintlyCachePath = None
        self._repo = None

    def getOptions(self, source=None):
        """
        Get options from the command line or other source
        """

        description = '''\
Show Sprint.ly tasks assigned to you (for the current project if in a git
repository) or install/uninstall the commit hook.
'''
        epilog = '''\
By default, your Sprint.ly items for the product associated with the current
Git repository are shown (or all items if not in a repository).

When using the commit-msg hook, you will be prompted for a Sprint.ly item
number unless you include a Sprint.ly keyword/item number in your commit
message:

  'Commit message goes here. References #54. Closes #65.'

Valid Sprint.ly keywords are:

  close, closes, closed, fix, fixed, fixes, addresses, re, ref, refs,
  references, see, breaks, unfixes, reopen, reopens, re-open, re-opens

As a shortcut, you may also include an item number as the first word in your
commit message:

  '#26 Message goes here'

The hook will automatically prepend the keyword 'references' and you won't be
asked to provide an item number. This shortcut only works at the beginning of
the message and does not support multiple item numbers.
'''
        parser = argparse.ArgumentParser(description=description, epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
        parser.add_argument('--all', '-a', dest='allProducts', help='show items for all products', action='store_true', default=False)
        parser.add_argument('--install-hook', dest='installHook', help='install commit-msg hook in current git repository', action='store_true', default=False)
        parser.add_argument('--uninstall-hook', dest='uninstallHook', help='uninstall commit-msg hook in current git repository', action='store_true', default=False)

        return parser.parse_args(source)

    def run(self, options):
        """
        Application flow.
        """

        try:
            # ensure that the ~/.sprintly/ folder exists, we have credentials, etc
            self.initialize()

            # If we have no git repository set the "all products" option
            if self._repo is None:
                options.allProducts = True

            # run the requested option
            if options.installHook:
                self.installHook()
            elif options.uninstallHook:
                self.uninstallHook()
            else:
                self.listSprintlyItems(options)

            # Write the cache
            self.writeCache()

        except KeyboardInterrupt:
            die()
        except SprintlyException as e:
            self.cprint(e.value, attr=RED)
            die()
        except Exception as e:
            die('Fatal Error: %s', e)

    def initialize(self):
        """
        Ultimate goal is to find the current git repository if any and ensure
        user and key have been configured.
        A cache file will also be created during this step if it doesn't
        already exist.
        """

        # get the users home directory
        home = os.path.expanduser('~')
        if home == '~':
            raise SprintlyException('Unable to expand home directory.')

        # set the sprintly directory path
        self._sprintlyDirectoryPath = os.path.join(home, '.sprintly')

        # set the sprintly cache path
        self._sprintlyCachePath = os.path.join(self._sprintlyDirectoryPath, 'sprintly.cache')

        # Find the root of this git repository
        root = '.'
        prev = None
        # Ascend until real path of the previously tried path is the same as
        # the real path of the current path (in which case we have hit root)
        while prev is None or os.path.realpath(root) is not os.path.realpath(prev):
            if os.path.isdir(os.path.join(root, '.git')):
                # Initialize a dulwich git repository object
                self._repo = dulwich.repo.Repo(root)
                break
            prev = root
            root = os.path.join(root, '..')

        # Check that Sprintly email address and API key are set
        unset = []
        try:
            self.getConfigValue('user')
        except KeyError:
            unset.append('user')
        try:
            self.getConfigValue('key')
        except KeyError:
            unset.append('key')
        if len(unset):
            self.cprint('%s %s not been configured.' % (' and '.join('sprintly.%s' % key for key in unset), 'has' if len(unset) is 1 else 'have'), attr=RED)
            self.cprint('To configure sprintly run')
            if 'user' in unset:
                self.cprint('  git config sprintly.user <Sprint.ly email address>')
            if 'key' in unset:
                self.cprint('  git config sprintly.key <Sprint.ly API key>')
            self.cprint('Use the --global flag to write to your user configuration rather than the repository-specific configuration.')
            self.cprint('  git config --global sprintly...')
            raise SprintlyException()

    def createSprintlyConfig(self):
        """
        Create the Sprint.ly config. Prompt user for all necessary values.
        """

        # Get git configuration
        config = self._repo.get_config()

        # used to simplify prompting user with optional default
        def getConfigItem(message, default=None):
            if default:
                item = raw_input(self.render('%s [${YELLOW}%s${RESET}]: ' % (message, default), trim=False)) or default
            else:
                item = raw_input('%s: ' % message)
            return item

        # Try and use API with current credentials to determine validity
        self.getUserId()

        # Get the list of products
        self.populateProductsCache()

        # Prompt user for default product if more than 1
        productMap = {}

        for product in self.getCache()['products'].values():
            productId = str(product['id'])
            productMap[productId] = product

        productCount = len(productMap)

        productId = None
        if productCount == 0:
            raise SprintlyException('It appears that you have no products associated with your Sprint.ly account. Add at least one and then try again.')
        elif productCount == 1:
            productId = productMap.values()[0]
        else:
            # prompt user for a product until they enter one found in the map
            productList = ', '.join(['%d - %s' % (p['id'], p['name']) for p in productMap.values()])
            while productId not in productMap.keys():
                message = 'Enter Sprint.ly product id (%s)' % productList
                try:
                    productId = getConfigItem(message, str(self.getConfigValue('product')))
                except KeyError:
                    productId = getConfigItem(message)
        config.set('sprintly', 'product', productId)

        # write config file if all is good
        config.write_to_path()

    def getConfigValue(self, key):
        """
        Get a value from the sprintly section of the git configuration
        """
        try:
            # Get the config of this git repository
            config = self._repo.get_config_stack()
        except AttributeError:
            # Get the global git config
            config = dulwich.config.StackedConfig(dulwich.config.StackedConfig.default_backends())
        return config.get('sprintly', key)

    def getUserId(self):
        """
        Get the user's ID either from the cache or from the API
        """

        cache = self.getCache()

        try:
            return cache['userId'][self.getConfigValue('user')]
        except KeyError:
            pass

        response = self.sprintlyAPICall('user/whoami.json')
        if not response or 'code' in response:
            raise SprintlyException('Invalid credentials. Unable to authenticate with Sprint.ly.')
        if response['email'] != self.getConfigValue('user'):
            raise SprintlyException('Invalid credentials. Please ensure you are using your own API Key.')

        # Associate email with user ID and store in cache
        if 'userId' not in cache:
            cache['userId'] = {}
        cache['userId'][self.getConfigValue('user')] = response['id']

        return response['id']

    def listSprintlyItems(self, options):
        """
        Lists all items for the current user from the Sprint.ly API.
        """

        # populate the cache from the API if possible (may not be possible,
        # e.g. in the case of offline access)
        self.populateProductsCache()

        products = self.getCache()['products']
        if options.allProducts:
            # Dict to list
            products = products.values()
        else:
            try:
                products = [products[self.getConfigValue('product')]]
            except KeyError:
                self.cprint('This git repository is not yet associated with a Sprint.ly product. You will now be prompted to choose one.', attr=YELLOW)
                return self.createSprintlyConfig()

        self.printList(products)

    def printList(self, products):
        """
        Print a list of Sprint.ly items.
        """

        statusTree = {
            'backlog': {},
            'in-progress': {},
            'completed': {},
            'accepted': {},
        }

        itemCount = 0

        for product in products:
            for item in product['items']:
                if not product['id'] in statusTree[item['status']]:
                    statusTree[item['status']][product['id']] = []
                    itemCount += 1

                statusTree[item['status']][product['id']].append(item)

        for key, status in iter(sorted(statusTree.items())):
            if not len(status):
                continue

            self.cprint(ITEM_STATUSES[key], attr=[BRIGHT_MAGENTA, UNDERLINE])

            for product_id in status:
                items = status[product_id]
                name = items[0]['product']['name']
                productId = str(items[0]['product']['id'])
                printProduct = '${DEFAULT}Product: ${BOLD}${BRIGHT_BLUE}' + name + '${NORMAL}${GREY} (https://sprint.ly/product/' + productId + '/)'
                self.cprint(printProduct)

                title_color = 'DEFAULT'
                for item in items:
                    attr = DIM if item['status'] in ('completed', 'accepted') else None
                    color = ITEM_COLORS.get(item['type'])

                    printItem = '${%s} #%d${DEFAULT}:${%s} %s' % (color, item['number'], title_color, item['title'])
                    self.cprint(printItem, attr=attr)

                    if 'children' in item:
                        for child in item['children']:
                            attr = DIM if child['status'] in ('completed', 'accepted') else None
                            childColor = ITEM_COLORS.get(child['type'])
                            title = child['title']
                            if child['status'] == 'in-progress':
                                title = u'${GREEN}⧁ ${%s}%s' % (title_color, title)

                            printChild = u'${%s}  #%d${DEFAULT}:${%s} %s' % (childColor, child['number'], title_color, title)
                            self.cprint(printChild, attr=attr)

            self.cprint('')

        if itemCount == 0:
            self.cprint('No assigned items', attr=GREEN)

    def populateProductsCache(self):
        """
        Populate the cache from the Sprint.ly API if possible.
        """

        cache = self.getCache()

        cache['products'] = {}

        # get products from the API
        products = self.sprintlyAPICall('products.json')
        if not products:
            raise SprintlyException('Unable to get product list.')

        # iterate over products
        for product in products:

            productName = product['name']
            productId = str(product['id'])
            productNameWithUrl = '\'' + productName + '\' (https://sprint.ly/product/' + productId + '/)'

            # get all items assigned to current user
            items = []
            offset = 0
            limit = 100
            while True:
                itemsPartial = self.sprintlyAPICall('products/' + productId + '/items.json?assigned_to=' + str(self.getUserId()) + '&children=1&limit=' + str(limit) + '&offset=' + str(offset))

                # if we get nothing, an empty list, an error, quit
                if not itemsPartial or len(itemsPartial) == 0 or 'code' in items:
                    break
                # otherwise, add on these items and increase the offset
                else:
                    items = items + itemsPartial
                    offset = offset + limit

                # if we got less than a full response, no need to check again
                if len(itemsPartial) < limit:
                    break

            # if anything went wrong, print an error message
            if 'code' in items:
                # include message if applicable
                message = ''
                if 'message' in items:
                    message = ': %s' % items['message']
                self.cprint('Warning: unable to get items for %s%s' % (productNameWithUrl, message), attr=YELLOW)
                continue
            # a 'parent' is any item without a parent key
            # a 'child' is any item with a parent key
            # sort so that all parents appear first and all children appear after ordered by their number
            items.sort(key=lambda item: item['number'] if 'parent' in item else sys.maxint, reverse=True)

            # turn flat list into tree
            itemsTree = []
            parentMapping = {} # allow parents to be looked up by number

            for item in items:
                number = str(item['number'])

                # if item is not a child
                if 'parent' not in item:
                    itemsTree.append(item)
                    parentMapping[number] = item

                # if item is a child...
                else:
                    parent = item['parent']  # get reference to parent
                    del item['parent']  # remove parent from child
                    parentNumber = str(parent['number'])

                    # if we have the parent, nest under parent
                    if parentNumber in parentMapping:

                        # we sorted items above to ensure all parents will be in map before any child is encountered
                        parent = parentMapping[parentNumber]
                        if 'children' not in parent:
                            parent['children'] = []
                        parent['children'].append(item)

                    # if we don't have the parent, add placeholder parent to preserve tree structure
                    else:
                        parent['children'] = [item]
                        parentMapping[parentNumber] = parent
                        itemsTree.append(parent)

            # sort items by (status, then first child, if it exists, else number)
            itemsTree.sort(key=lambda item: item['children'][0]['number'] if 'children' in item else item['number'], reverse=True)
            product['items'] = itemsTree
            cache['products'][productId] = product

    def writeCache(self):
        """
        Write the current cache object to disk
        """

        cache = self.getCache()
        cache['updated_at'] = time()
        serialized_cache = json.dumps(cache)

        cache_file = open(self._sprintlyCachePath, 'w')
        cache_file.write(serialized_cache)
        cache_file.close()

    def _readCache(self):
        """
        Read from the cache from disk and return it
        """

        try:
            os.mkdir(self._sprintlyDirectoryPath, 0700)
        except OSError:
            # Already exists
            pass
        except IOError:
            raise SprintlyException('Unable to create folder at %s' % self._sprintlyDirectoryPath)

        try:
            cache_file = open(self._sprintlyCachePath, 'r')
            serialized_cache = cache_file.readline()
            cache_file.close()
            try:
                cache = json.loads(serialized_cache)
            except ValueError:
                # Bad JSON; ignore and replace
                cache = {}
        except IOError:
            # File doesn't exist yet
            cache = {}

        return cache

    def getCache(self):
        """
        Get the cache from memory or from file
        """

        if self._cache is None:
            self._cache = self._readCache()
        return self._cache

    def sprintlyAPICall(self, url):
        """
        Wraps up a call to the Sprint.ly api. Returns a map representing
        the JSON response or false if the call could not be completed.
        """

        url = 'https://sprint.ly/api/%s' % url

        try:
            userData = 'Basic ' + (self.getConfigValue('user') + ':' + self.getConfigValue('key')).encode('base64').replace("\n",'')
            req = urllib2.Request(url)
            req.add_header('Accept', 'application/json')
            req.add_header('Authorization', userData)
            res = urllib2.urlopen(req)
            response = res.read()
            return json.loads(response)
        except urllib2.HTTPError, error:
            response = error.read()
            return json.loads(response)
        except Exception:
            return False

    def installHook(self):
        """
        A symlink will be created from <git repo root>/.git/hooks/commit-msg
        to the Sprintly commit message hook.
        If a commit message hook already exists it will be moved.
        """

        # Ensure we are in a git repository
        if self._repo is None:
            raise SprintlyException('This command can only be run from a git repository.')

        hooks_directory = os.path.join(self._repo.controldir(), 'hooks')

        # create a symlink to the commit-msg file
        destination = os.path.join(hooks_directory, HOOK_NAME)

        # If the destination is not our hook, move it
        if os.path.exists(destination):
            if os.path.realpath(destination) == os.path.realpath(HOOK_PATH):
                self.cprint('Hook is already installed.', attr=GREEN)
                return
            originalDestination = os.path.join(hooks_directory, ORIGINAL_HOOK_NAME)
            if os.path.exists(originalDestination):
                print HOOK_PATH
                self.cprint('A commit hook (not sprintly) already exists at %s, as does where we would normally move that file, %s. This must be resolved manually.' % (destination, originalDestination), attr=RED)
                return
            shutil.move(destination, originalDestination)
            self.cprint('Existing hook moved to %s' % originalDestination, attr=YELLOW)

        self.cprint('Creating symlink...')

        try:
            os.symlink(HOOK_PATH, destination)
        except Exception:
            raise SprintlyException('Unable to create symlink.')

        self.cprint('Hook was installed at %s' % destination, attr=GREEN)

        # check to see if the email associated with git matches the Sprint.ly email
        # if not, Sprint.ly won't be able to create comments
        try:
            process = subprocess.Popen(['git', 'config', 'user.email'], stdout=subprocess.PIPE)
            gitEmail = process.stdout.read().strip()
            if gitEmail != self.getConfigValue('user'):
                self.cprint('WARNING: Your git email (' + gitEmail + ') does not match your Sprint.ly username (' + self.getConfigValue('user') + ')', attr=YELLOW)
                self.cprint('WARNING: Don\'t worry - there is an easy fix. Simply run one of the following:', attr=YELLOW)
                self.cprint('\t\'git config --global user.email ' + self.getConfigValue('user') + '\' (all repos)')
                self.cprint('\t\'git config user.email ' + self.getConfigValue('user') + '\' (this repo only)')
        except Exception:
            self.cprint('Unable to verify that \'git config user.email\' matches your Sprint.ly account email.', attr=RED)

    def uninstallHook(self):
        """
        Remove the symlink we created, as long as it points to our hook. If an
        old hook was previously moved by us, move it back.
        """

        # Ensure we are in a git repository
        if self._repo is None:
            raise SprintlyException('This command can only be run from a git repository.')

        hooks_directory = os.path.join(self._repo.controldir(), 'hooks')

        # get path to commit-msg file
        destination = os.path.join(hooks_directory, HOOK_NAME)

        # if the destination is a file, error; if it's a symlink, delete it
        try:
            if not os.path.exists(destination):
                self.cprint('There is no commit hook installed.', attr=YELLOW)
                return
            elif not os.path.isfile(destination):
                raise SprintlyException('Commit hook is not a file.')
            elif os.path.realpath(destination) != os.path.realpath(HOOK_PATH):
                raise SprintlyException('The commit-msg hook was not installed by this tool. Please remove it manually.')
            else:
                os.unlink(destination)
        except SprintlyException as e:
            raise e

        self.cprint('Hook has been uninstalled.', attr=GREEN)

        # If it exists, move the original hook back to commit-msg
        originalDestination = os.path.join(hooks_directory, ORIGINAL_HOOK_NAME)
        if os.path.exists(originalDestination):
            shutil.move(originalDestination, destination)
            self.cprint('Moved original commit hook back to %s' % destination, attr=YELLOW)

    def cprint(self, str, attr=None, trim=True):
        self._term.write(self.render(str, attr, trim) + '\r\n')

    def render(self, str, attr=None, trim=True):
        if self._has_color:
            if attr:
                if isinstance(attr, list):
                    attr = ''.join(attr)
            else:
                attr = ''

            seq = re.sub(r'\$\$|\${\w+}', self._render_sub, str)
            if trim:
                seq = self._trim(seq)

            return attr + seq + RESET
        else:
            seq = re.sub(r'\$\$|\${\w+}', '', str)
            if trim and len(seq) > self._cols:
                return seq[0:self._cols - 1] + u'\u2026'
            return seq

    def _render_sub(self, match):
        s = match.group()
        if s == '$$': return s
        else: return ATTRS.get(s[2:-1], '')

    def _trim(self, raw):
        # TODO: >>> This could probably be much simpler if I was smarter
        seq = ''
        str_len = 0
        i = 0
        matchiter = re.finditer(r'(\x1b.*?m)', raw.strip())
        for match in matchiter:
            chunk = raw[i:match.start()]
            i = match.end()
            if str_len + len(chunk) > self._cols:
                chunk = chunk[0:self._cols - str_len - 1] + u'\u2026'
            str_len = str_len + len(chunk)
            seq = seq + chunk + match.group()

            if (str_len >= self._cols):
                break

        if str_len < self._cols:
            chunk = raw[i:]
            if str_len + len(chunk) > self._cols:
                chunk = chunk[0:self._cols - str_len - 1] + u'\u2026'
            seq = seq + chunk

        return seq

    def elipsify(self, seq):
        return seq[0:-1].strip(string.punctuation) + u'\u2026'


class SprintlyException(Exception):
    """
    Exception used to pass known exceptions throughout
    the sprintly tool.
    """
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)


def die(message=None, *args):
    """
    Prints the message, if present, and then exits.
    """

    if message:
        logger.error(message, *args, exc_info=True)
    print 'Program exiting.'
    sys.exit(1)

# vim: et ts=4 sts=4 sw=4 tw=78 fo-=w
