"""
execnet: rapid multi-Python deployment
========================================================

.. _execnet: http://codespeak.net/execnet

execnet_ provides carefully tested means to ad-hoc interact with Python
interpreters across version, platform and network barriers.  It provides
a minimal and fast API targetting the following uses:

* distribute tasks to local or remote CPUs
* write and deploy hybrid multi-process applications
* write scripts to administer a bunch of exec environments

Features
------------------

* zero-install bootstrapping: no remote installation required!

* flexible communication: send/receive as well as
  callback/queue mechanisms supported

* simple serialization of python builtin types (no pickling)

* grouped creation and robust termination of processes

* well tested between CPython 2.4-3.2, Jython 2.5.1 and PyPy 1.1
  interpreters.

* fully interoperable between Windows and Unix-ish systems.
"""

try:
    from setuptools import setup, Command
except ImportError:
    from distutils.core import setup, Command

def main():
    setup(
        name='execnet',
        description='execnet: rapid multi-Python deployment',
        long_description = __doc__,
        version='1.1.1dev4',
        url='http://codespeak.net/execnet',
        license='MIT',
        platforms=['unix', 'linux', 'osx', 'cygwin', 'win32'],
        author='holger krekel and others',
        author_email='holger at merlinux.eu',
        cmdclass = {'test': PyTest},
        classifiers=[
            'Development Status :: 4 - Beta',
            'Intended Audience :: Developers',
            'License :: OSI Approved :: MIT License',
            'Operating System :: POSIX',
            'Operating System :: Microsoft :: Windows',
            'Operating System :: MacOS :: MacOS X',
            'Topic :: Software Development :: Libraries',
            'Topic :: System :: Distributed Computing',
            'Topic :: System :: Networking',
            'Programming Language :: Python',
            'Programming Language :: Python :: 3'],
        packages=['execnet', 'execnet.script'],
    )

class PyTest(Command):
    user_options = []
    def initialize_options(self):
        pass
    def finalize_options(self):
        pass
    def run(self):
        import sys,subprocess
        errno = subprocess.call([sys.executable, 'testing/runtest.py'])
        raise SystemExit(errno)

if __name__ == '__main__':
    main()
