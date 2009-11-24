"""
the execnet package allows to: 

* instantiate local/remote Python Interpreters
* send code for execution to one or many Interpreters 
* send and receive data between codeInterpreters through channels

execnet performs **zero-install bootstrapping** into other interpreters; 
package installation is only required at the initiating side.  execnet enables
interoperation between CPython 2.4-3.1, Jython 2.5 and PyPy 1.1 and works
well on Windows, Linux and OSX systems.

execnet was written and is maintained by Holger Krekel with contributions from many others.  The package is licensed under the GPL Version 2 or later, at your choice.  Contributions and some parts of the package are licensed under the MIT license.
"""

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup

from execnet import __version__

def main():
    setup(
        name='execnet',
        description='execnet: elastic Python deployment',
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
