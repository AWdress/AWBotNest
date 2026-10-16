"""Transparent compatibility entry for ``python -m awbotnest.main``."""

import sys

from awbotnest import _image_main as _implementation


if __name__ == "__main__":
    _implementation.main()
elif __name__ != "__mp_main__":
    # Preserve the implementation's globals for ordinary imports and patching.
    # Spawn workers re-run this launcher as __mp_main__ without starting a server.
    sys.modules[__name__] = _implementation
