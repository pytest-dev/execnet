Connecting different Python interpreters
==========================================

.. _`dumps/loads examples`:

Dumping and loading values across interpreter versions
----------------------------------------------------------

.. versionadded:: 1.1

Execnet offers a new safe and fast :ref:`dumps/loads API` which you
can use to dump builtin python data structures and load them
later with the same or a different python interpreter (including
between Python2 and Python3).  The standard library offers
the pickle and marshal modules but they do not work safely
between different interpreter versions.  Using xml/json
requires a mapping of Python objects and is not easy to
get right.  Moreover, execnet allows to control handling
of bytecode/strings/unicode types. Here is an example::

    # using python2
    import execnet
    with open("data.py23", "wb") as f:
        f.write(execnet.dumps(["hello", "world"]))

    # using Python3
    import execnet
    with open("data.py23", "rb") as f:
        val = execnet.loads(f.read(), py2str_as_py3str=True)
    assert val == ["hello", "world"]

See the :ref:`dumps/loads API` for more details on string
conversion options.  Please note, that you can not dump
user-level instances, only builtin python types.

Connect to Python2/Numpy from Python3
----------------------------------------

Here we run a Python3 interpreter to connect to a Python2.7 interpreter
that has numpy installed. We send items to be added to an array and
receive back the remote "repr" of the array::

    import execnet
    gw = execnet.makegateway("popen//python=python2.7")
    channel = gw.remote_exec("""
        import numpy
        array = numpy.array([1,2,3])
        while 1:
            x = channel.receive()
            if x is None:
                break
            array = numpy.append(array, x)
        channel.send(repr(array))
    """)
    for x in range(10):
        channel.send(x)
    channel.send(None)
    print (channel.receive())

will print on the CPython3.1 side::

    array([1, 2, 3, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9])

A more refined real-life example of python3/python2 interaction
is the anyvc_ project which uses version-control bindings in
a Python2 subprocess in order to offer Python3-based library
functionality.

.. _anyvc: http://bitbucket.org/RonnyPfannschmidt/anyvc/overview/


Reconfiguring the string coercion between python2 and python3
-------------------------------------------------------------

Sometimes the default configuration of string coercion (2str to 3str, 3str to 2unicode)
is inconvient, thus it can be reconfigured via `gw.reconfigure` and `channel.reconfigure`. Here is an example session on a Python2 interpreter::


    >>> import execnet
    >>> execnet.makegateway("popen//python=python3.2")
    <Gateway id='gw0' receive-live, 0 active channels>
    >>> gw=execnet.makegateway("popen//python=python3.2")
    >>> gw.remote_exec("channel.send('hello')").receive()
    u'hello'
    >>> gw.reconfigure(py3str_as_py2str=True)
    >>> gw.remote_exec("channel.send('hello')").receive()
    'hello'
    >>> ch = gw.remote_exec('channel.send(type(channel.receive()).__name__)')
    >>> ch.send('a')
    >>> ch.receive()
    'str'
    >>> ch = gw.remote_exec('channel.send(type(channel.receive()).__name__)')
    >>> ch.reconfigure(py2str_as_py3str=False)
    >>> ch.send('a')
    >>> ch.receive()
    u'bytes'


Work with Java objects from CPython
----------------------------------------

Use your CPython interpreter to connect to a `Jython 2.5.1`_ interpreter
and work with Java types::

    import execnet
    gw = execnet.makegateway("popen//python=jython")
    channel = gw.remote_exec("""
        from java.util import Vector
        v = Vector()
        v.add('aaa')
        v.add('bbb')
        for val in v:
            channel.send(val)
    """)

    for item in channel:
        print (item)

will print on the CPython side::

    aaa
    bbb

.. _`Jython 2.5.1`: http://www.jython.org

Work with C# objects from CPython
----------------------------------------

(Experimental) use your CPython interpreter to connect to a IronPython_ interpreter
which can work with C# classes.  Here is an example for instantiating
a CLR Array instance and sending back its representation::

    import execnet
    gw = execnet.makegateway("popen//python=ipy")

    channel = gw.remote_exec("""
        import clr
        clr.AddReference("System")
        from System import Array
        array = Array[float]([1,2])
        channel.send(str(array))
    """)
    print (channel.receive())

using Mono 2.0 and IronPython-1.1 this will print on the CPython side::

    System.Double[](1.0, 2.0)

.. note::
   Using IronPython needs more testing, likely newer versions
   will work better.  please feedback if you have information.

.. _IronPython: http://ironpython.net
