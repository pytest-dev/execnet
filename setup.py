# -*- coding: utf-8 -*-
def main():
    from setuptools import setup

    with open("README.rst") as fp:
        readme = fp.read()
    setup(
        name="execnet",
        description="execnet: rapid multi-Python deployment",
        long_description=readme,
        use_scm_version={"write_to": "execnet/_version.py"},
        url="https://execnet.readthedocs.io/en/latest/",
        license="MIT",
        platforms=["unix", "linux", "osx", "cygwin", "win32"],
        author="holger krekel and others",
        classifiers=[
            "Development Status :: 5 - Production/Stable",
            "Intended Audience :: Developers",
            "License :: OSI Approved :: MIT License",
            "Operating System :: POSIX",
            "Operating System :: Microsoft :: Windows",
            "Operating System :: MacOS :: MacOS X",
            "Topic :: Software Development :: Libraries",
            "Topic :: System :: Distributed Computing",
            "Topic :: System :: Networking",
            "Programming Language :: Python :: 2",
            "Programming Language :: Python :: 2.7",
            "Programming Language :: Python :: 3",
            "Programming Language :: Python :: 3.5",
            "Programming Language :: Python :: 3.6",
            "Programming Language :: Python :: 3.7",
            "Programming Language :: Python :: 3.8",
            "Programming Language :: Python :: 3.9",
            "Programming Language :: Python :: Implementation :: CPython",
            "Programming Language :: Python :: Implementation :: PyPy",
        ],
        packages=["execnet", "execnet.script"],
        python_requires=">=2.7, !=3.0.*, !=3.1.*, !=3.2.*, !=3.3.*, !=3.4.*",
        extras_require={"testing": ["pre-commit"]},
        setup_requires=["setuptools_scm"],
    )


if __name__ == "__main__":
    main()
