"""
execnet: distributed Python deployment and communication
========================================================

.. _execnet: http://codespeak.net/execnet

execnet_ provides carefully tested means to ad-hoc interact with Python
interpreters across version, platform and network barriers.  It provides
a minimal and fast API targetting the following uses:

* distribute tasks to local or remote processes
* write and deploy hybrid multi-process applications
* write scripts to administer multiple hosts

Features
------------------

* zero-install bootstrapping: no remote installation required!

* flexible communication: send/receive as well as
  callback/queue mechanisms supported

* simple serialization of python builtin types (no pickling)

* grouped creation and robust termination of processes

* well tested between CPython 2.6-3.X, Jython 2.5.1 and PyPy 2.2
  interpreters.

* interoperable between Windows and Unix-ish systems.

* integrates with different threading models, including standard
  os threads, eventlet and gevent based systems.

"""


def main():
    from setuptools import setup
    setup(
        name='execnet',
        description='execnet: rapid multi-Python deployment',
        long_description=__doc__,
        get_version_from_scm=True,
        url='http://codespeak.net/execnet',
        license='MIT',
        platforms=['unix', 'linux', 'osx', 'cygwin', 'win32'],
        author='holger krekel and others',
        author_email='holger at merlinux.eu',
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
        setup_requires=[
            # this is a setup/release time dependency
            # we are supposed to use wheels
            'hgdistver',
        ]
    )

if __name__ == '__main__':
    main()
