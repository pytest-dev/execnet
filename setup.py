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

* well tested between CPython 2.4-3.1, Jython 2.5.1 and PyPy 1.1
  interpreters.

* fully interoperable between Windows and Unix-ish systems. 
"""

try:
    from setuptools import setup, Command
except ImportError:
    from distutils.core import setup, Command

from execnet import __version__

def main():
    setup(
        name='execnet',
        description='execnet: rapid multi-Python deployment',
        long_description = __doc__,
        version= __version__,
        url='http://codespeak.net/execnet',
        license='GPL V2 or later',
        platforms=['unix', 'linux', 'osx', 'cygwin', 'win32'],
        author='holger krekel and others',
        author_email='holger at merlinux.eu',
        cmdclass = {'test': PyTest},
        classifiers=[
            'Development Status :: 5 - Production/Stable',
            'Intended Audience :: Developers',
            'License :: OSI Approved :: GNU General Public License (GPL)',
            'Operating System :: POSIX',
            'Operating System :: Microsoft :: Windows',
            'Operating System :: MacOS :: MacOS X',
            'Topic :: Software Development :: Testing',
            'Topic :: Software Development :: Libraries',
            'Topic :: System :: Distributed Computing',
            'Topic :: System :: Networking',
            'Programming Language :: Python'],
        packages=['execnet', 'execnet.script'],
    )

class PyTest(Command):
    user_options = []
    def initialize_options(self):
        pass
    def finalize_options(self):
        pass
    def run(self):
        import py
        py.cmdline.pytest(py.std.sys.argv[2:])

if __name__ == '__main__':
    main()
