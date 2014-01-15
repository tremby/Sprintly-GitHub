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
from optparse import OptionParser

# force utf-8 encoding
reload(sys)
sys.setdefaultencoding('utf-8')

logging.basicConfig()
logger = logging.getLogger(__name__)

# constants
CONFIG_VERSION = '2.1'
HOOK_NAME = 'commit-msg'
HOOK_DIR = os.path.dirname(__file__)

# non-editable constants
HOOK_PATH = os.path.join(HOOK_DIR, HOOK_NAME)

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

        self._config = {}
        self._sprintlyDirectoryPath = None
        self._sprintlyConfigPath = None
        self._sprintlyCachePath = None

    def run(self, scr=None):
        """
        Application flow.
        """

        try:
            usage = textwrap.dedent('''\
            usage = '''\
%prog [options]

By default, your Sprint.ly items will be shown.

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
            parser = OptionParser(usage=usage)
            parser.add_option('--install-hook', dest='installHook', help='install commit-msg hook in current directory (must be a git repository)', action='store_true', default=False)
            parser.add_option('--uninstall-hook', dest='uninstallHook', help='uninstall commit-msg hook in current directory (must be a git repository)', action='store_true', default=False)

            (options, _) = parser.parse_args()

            # ensure that the ~/.sprintly/ folder exists, we have credentials, etc
            self.initialize()

            # run the requested option
            if options.installHook:
                self.installHook()
            elif options.uninstallHook:
                self.uninstallHook()
            else:
                self.listSprintlyItems()

        except KeyboardInterrupt:
            die()
        except Exception as e:
            die('Fatal Error: %s', e)

    def initialize(self):
        """
        Ultimate goal is to get the user and key from the config file.
        If the config file cannot be found, a config file will be
        created via prompts displayed to the user. A cache file will
        also be created during this step.
        """

        # get the users home directory
        home = os.path.expanduser('~')
        if home == '~':
            raise SprintlyException('Unable to expand home directory.')

        # set the sprintly directory path (create if it doesn't exist)
        self._sprintlyDirectoryPath = os.path.join(home, '.sprintly')
        if not os.path.isdir(self._sprintlyDirectoryPath):
            os.mkdir(self._sprintlyDirectoryPath, 0700)
            if not os.path.isdir(self._sprintlyDirectoryPath):
                raise SprintlyException('Unable to create folder at %s' % self._sprintlyDirectoryPath)

        # set the sprintly config path (create if it doesn't exist)
        self._sprintlyConfigPath = os.path.join(self._sprintlyDirectoryPath, 'sprintly.config')
        if not os.path.isfile(self._sprintlyConfigPath):
            self.createSprintlyConfig()

        # set the sprintly cache path (create if it doesn't exist)
        self._sprintlyCachePath = os.path.join(self._sprintlyDirectoryPath, 'sprintly.cache')
        if not os.path.isfile(self._sprintlyCachePath):
            try:
                # "touch" cache file
                open(self._sprintlyCachePath, 'w').close()
            except Exception:
                raise SprintlyException('Unable to create file at %s' % self._sprintlyCachePath)

        # load config values
        self.loadFromConfig()

    def createSprintlyConfig(self):
        """
        Create the Sprint.ly config. Prompt user for all necessary values.
        """

        # set version
        self._config['version'] = CONFIG_VERSION

        # used to simplify prompting user with optional default
        def getConfigItem(message, default=None):
            if default:
                item = raw_input(self.render('%s [${YELLOW}%s${RESET}]: ' % (message, default), trim=False)) or default
            else:
                item = raw_input('%s: ' % message)
            return item

        # prompt for user
        name = 'user'
        message = 'Enter Sprint.ly username (email)'
        if name in self._config:
            self._config[name] = getConfigItem(message, self._config[name])
        else:
            self._config[name] = getConfigItem(message)

        # prompt for key
        name = 'key'
        message = 'Enter Sprint.ly API Key'
        if name in self._config:
            self._config[name] = getConfigItem(message, self._config[name])
        else:
            self._config[name] = getConfigItem(message)

        # try and use API with these values to determine validity
        response = self.sprintlyAPICall('user/whoami.json')
        if not response or 'code' in response:
            raise SprintlyException('Invalid credentials. Unable to authenticate with Sprint.ly.')
        if response['email'] != self._config['user']:
            raise SprintlyException('Invalid credentials. Please ensure you are using your own API Key.')

        # add user id to config
        self._config['id'] = response['id']

        # get a list of products and prompt user for default product if more than 1
        products = self.sprintlyAPICall('products.json')
        if not products:
            raise SprintlyException('Unable to get product list.')
        productMap = {}

        for product in products:
            productId = str(product['id'])
            productMap[productId] = product

        productCount = len(productMap)

        if productCount == 0:
            raise SprintlyException('It appears that you have no products associated with your Sprint.ly account. Add at least one and then try again.')
        elif productCount == 1:
            self._config['product'] = productMap.values()[0]
        else:
            # prompt user for a product until they enter one found in the map
            productList = ', '.join(['%d - %s' % (p['id'], p['name']) for p in productMap.values()])
            defaultProductId = '0'
            while defaultProductId not in productMap.keys():
                message = 'Enter default Sprint.ly product id (%s)' % productList
                if 'product' in self._config:
                    defaultProductId = getConfigItem(message, str(self._config['product']['id']))
                else:
                    defaultProductId = getConfigItem(message)
            self._config['product'] = productMap[defaultProductId]

        # write config file if all is good
        serialized_config = json.dumps(self._config)

        try:
            config_file = open(self._sprintlyConfigPath, 'w')
            config_file.write(serialized_config)
            config_file.close()
            self.cprint('Configuration successfully created.', attr=GREEN)
        except:
            raise SprintlyException('Unable to write configuration to disk at %s' % self._sprintlyConfigPath)

    def loadFromConfig(self):
        """
        Load user and key from the config file. Validate here that the version
        of this config is readable by this version of the tool.
        """

        try:
            config_file = open(self._sprintlyConfigPath, 'r')
            serialized_config = config_file.readline()
            config_file.close()
            self._config = json.loads(serialized_config)
        except:
            raise SprintlyException('Unable to read credentials from disk at %s' % self._sprintlyConfigPath)

        # validate version
        if 'version' not in self._config or self._config['version'] != CONFIG_VERSION:
            self.cprint('Your configuration needs to be updated. You will now be prompted to update it.', attr=YELLOW)
            self.createSprintlyConfig()

    def listSprintlyItems(self):
        """
        Lists all items for the current user from the Sprint.ly API.
        """

        # populate the cache from the API if possible (may not be possible,
        # e.g. in the case of offline access)
        self.populateCache()
        products = self.readCache()
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
            print 'No assigned items'

    def populateCache(self):
        """
        Populate the cache from the Sprint.ly API if possible.
        """

        try:
            cache = {}

            cache['updated_at'] = time()
            cache['products'] = []

            products = []

            # use product from config file
            products.append(self._config['product'])

            # get products from the API
            # products = self.sprintlyAPICall('products.json')
            # if not products:
            #   raise SprintlyException('Unable to get product list.')

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
                    itemsPartial = self.sprintlyAPICall('products/' + productId + '/items.json?assigned_to=' + str(self._config['id']) + '&children=1&limit=' + str(limit) + '&offset=' + str(offset))

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
                    print 'Warning: unable to get items for %s%s' % (productNameWithUrl, message)
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
                cache['products'].append(product)

            serialized_cache = json.dumps(cache)

            cache_file = open(self._sprintlyCachePath, 'w')
            cache_file.write(serialized_cache)
            cache_file.close()
        except Exception:
            print '\033[91m'
            print 'Unable to populate cache. List may not be up to date.'
            print '\033[0m'

    def readCache(self):
        """
        Read from the cache and return a list of Sprint.ly items.
        """

        cache_file = open(self._sprintlyCachePath, 'r')
        serialized_cache = cache_file.readline()
        cache_file.close()

        try:
            cache = json.loads(serialized_cache)
        except Exception:
            raise SprintlyException('Cache is empty or invalid. Please try running the tool again.')

        return cache['products']

    def sprintlyAPICall(self, url):
        """
        Wraps up a call to the Sprint.ly api. Returns a map representing
        the JSON response or false if the call could not be completed.
        """

        url = 'https://sprint.ly/api/%s' % url

        try:
            userData = 'Basic ' + (self._config['user'] + ':' + self._config['key']).encode('base64').replace("\n",'')
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
        A symlink will be created between the <current directory>/.git/hooks/commit-msg
        and ~/.sprintly/commit-msg
        """

        # ensure the current directory is a git repository
        directory = os.getcwd()
        hooks_directory = os.path.join(directory, '.git', 'hooks')
        if not os.path.isdir(hooks_directory):
            raise SprintlyException('This command can only be run from the root of a git repository.')

        # create a symlink to the commit-msg file
        destination = os.path.join(hooks_directory, HOOK_NAME)

        # if the destination is a file, move it; if it's a symlink, delete it
        try:
            if os.path.isfile(destination) and not os.path.islink(destination):
                shutil.move(destination, destination + '.original')
            elif os.path.islink(destination):
                os.unlink(destination)
        except Exception:
            raise SprintlyException('File already exists at %s. Please delete it before proceeding.' % destination)

        print 'Creating symlink...'

        try:
            os.symlink(HOOK_PATH, destination)
        except Exception:
            raise SprintlyException('Unable to create symlink.')

        print 'Hook was installed at %s' % destination

        # check to see if the email associated with git matches the Sprint.ly email
        # if not, Sprint.ly won't be able to create comments
        try:
            process = subprocess.Popen(['git', 'config', 'user.email'], stdout=subprocess.PIPE)
            gitEmail = process.stdout.read().strip()
            if gitEmail != self._config['user']:
                print 'WARNING: Your git email (' + gitEmail + ') does not match your Sprint.ly username (' + self._config['user'] + ')'
                print 'WARNING: Don\'t worry - there is an easy fix. Simply run one of the following:'
                print '\t\'git config --global user.email ' + self._config['user'] + '\' (all repos)'
                print '\t\'git config user.email ' + self._config['user'] + '\' (this repo only)'
        except Exception:
            print 'Unable to verify that \'git config user.email\' matches your Sprint.ly account email.'

    def uninstallHook(self):
        """
        Remove the symlink we created. If the hook is not a symlink, don't remove it.
        """

        # ensure the current directory is a git repository
        directory = os.getcwd()
        hooks_directory = os.path.join(directory, '.git', 'hooks')
        if not os.path.isdir(hooks_directory):
            raise SprintlyException('This command can only be run from the root of a git repository.')

        # get path to commit-msg file
        destination = os.path.join(hooks_directory, HOOK_NAME)

        # if the destination is a file, error; if it's a symlink, delete it
        try:
            if os.path.isfile(destination) and not os.path.islink(destination):
                raise SprintlyException('The commit-msg hook was not installed by this tool. Please remove it manually.')
            elif os.path.islink(destination):
                os.unlink(destination)
            else:
                print 'Hook is already uninstalled.'
                return
        except SprintlyException as e:
            raise e
        except Exception:
            raise SprintlyException('File already exists at %s. Please delete it before proceeding.' % destination)

        print 'Hook has been uninstalled.'

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
