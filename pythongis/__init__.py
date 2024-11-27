"""
PythonGIS

...
"""

import warnings

try:
    from . import app
except:
    warnings.warn("Error importing the 'app' module, only available on systems with Tkinter and tk2 installed")
    
from . import raster
from . import vector
from . import renderer
from . import tiler

# Some object imports
from .vector.data import VectorData
from .raster.data import RasterData

__version__ = "0.2.0"


