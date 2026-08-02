"""Pre-import NumPy/SciPy modules before coverage.py sysmon starts.

Coverage.py's ``sysmon`` core on Python 3.14 can prevent NumPy/SciPy C
extensions from loading if they are first imported while the tracer is active.
Importing the modules that ``scipy.optimize`` needs before pytest-cov starts
coverage allows the full test suite (including MILP optimizer and ML predictor
tests) to run.

This module is loaded automatically by Python because of its name, provided the
containing directory is on ``PYTHONPATH``.  The imports are intentionally bare
(side-effect) imports, so each is marked with ``# noqa: F401``.
"""

import importlib

# Import the NumPy/SciPy C extensions before coverage.py's sysmon tracer starts.
# The import has a side effect of loading the extension modules; the names are
# intentionally unused here.
importlib.import_module("numpy")  # noqa: F401
importlib.import_module("numpy.fft")  # noqa: F401
importlib.import_module("numpy.linalg")  # noqa: F401
importlib.import_module("scipy.optimize")  # noqa: F401
