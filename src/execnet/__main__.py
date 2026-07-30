"""``python -m execnet`` -- the same CLI as the ``execnet`` console script.

Provisioning emits this form for a direct interpreter launch, where the
console script's location on the target is not knowable; under ``uv run``
the bare ``execnet`` command resolves inside the provisioned environment.
"""

from ._cli import main

if __name__ == "__main__":
    main()
