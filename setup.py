# -*- coding: UTF-8 -*-
from setuptools import setup, find_packages

setup(
        name='sprintly-github',
        description="A command line tool to view your Sprint.ly tasks and a hook to facilitate Sprint.ly's Github integration",
        version='3.0.0',
        author='Walter Blaurock <walter@nextbigsound.com>, Bart Nagel <bart@tremby.net>, et al',
        url='https://github.com/tremby/sprintly-github',
        py_modules=['sprintly'],
        data_files=[('', ['commit-msg'])],
        scripts=['sprintly'],
        license='MIT',
        install_requires=['dulwich>=0.9.4'],
        )

# vim: et ts=4 sts=4 sw=4 tw=78 fo-=w
