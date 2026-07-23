# Intentionally insecure test SSH keys

These ed25519 keypairs are **committed on purpose** and are **not secret**. They
exist only so the local ssh-connect tests (`testing/test_ssh_local.py`) can run
an in-process asyncssh server that the system `ssh` client authenticates against,
without generating keys at runtime.

- `insecure_host_ed25519[.pub]` — the test SSH **server** host key.
- `insecure_client_ed25519[.pub]` — the test **client** identity; its public key
  is the server's sole authorized key.

**Never** use these anywhere real. They grant nothing beyond a throwaway server
bound to `127.0.0.1` on an ephemeral port during a test run.

Note: git does not preserve `0600` permissions, and OpenSSH refuses a
world-readable private key, so the tests copy the client key to a temp dir and
`chmod 0600` it before use.
