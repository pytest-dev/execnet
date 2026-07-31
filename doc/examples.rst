==============================================================================
examples
==============================================================================

.. _`execnet-dev`: http://mail.python.org/mailman/listinfo/execnet-dev
.. _`execnet-commit`: http://mail.python.org/mailman/listinfo/execnet-commit

Note: the examples with ``>>>`` prompts are run as doctests by ``tox -e
docs``, except for the few marked ``# doctest: +SKIP``, which need a remote
account to talk to.

.. toctree::
   :maxdepth: 2

   example/test_info
   example/test_group
   example/test_proxy
   example/test_multi
   example/test_debug

.. toctree::
   :hidden:

   example/test_ssh_fileserver
