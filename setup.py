"""
execnet: connect your execution environments 
========================================================

.. image:: _static/pythonring.png
   :align: right

Execnet allows to ad-hoc connect to Python interpreters across version, platform and network barriers.  It provides a minimal, fast and robust API for the following uses:

* distribute tasks to multiple CPUs
* deploy hybrid applications 
* manage local and remote execution environments

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
    from setuptools import setup
except ImportError:
    from distutils.core import setup

from execnet import __version__

def main():
    setup(
        name='execnet',
        description='execnet: connect your execution environments',
        long_description = __doc__,
        version= __version__,
        url='http://codespeak.net/execnet',
        license='GPL V2 or later',
        platforms=['unix', 'linux', 'osx', 'cygwin', 'win32'],
        author='holger krekel and others',
        author_email='holger at merlinux.eu',
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

if __name__ == '__main__':
    main()
