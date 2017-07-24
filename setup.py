def main():
    from setuptools import setup
    with open("README.txt") as fp:
        readme = fp.read()
    setup(
        name='execnet',
        description='execnet: rapid multi-Python deployment',
        long_description=readme,
        use_scm_version={'write_to': 'execnet/_version.py'},
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
        install_requires=['apipkg>=1.4'],
        setup_requires=['setuptools_scm'],
    )


if __name__ == '__main__':
    main()
