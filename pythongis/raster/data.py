"""
Module containing the data structures and interfaces for operating with raster datasets.
"""

# import builtins
import sys, os, itertools, operator, math, warnings

# import internals
from . import loader
from . import saver

# import PIL as the data container
import PIL.Image, PIL.ImageMath, PIL.ImageStat

# import other
import pycrs

# PY3 fix
try: 
    basestring
except NameError:
    basestring = (bytes,str) # PY3

# TODO:
# not sure if setting mask should paste nodatavals, or if only as a temporary overlay so that underlying values be kept (and only changed via compute etc)...
# also if conditional should query valid values only or all raw values...
# ie a strategy of convenience behind-the-scene handling or give direct raw handling of values and masks... 
# compute should only affect valid values, not nullvalues. 
# ...

class _ModuleFuncsAsClassMethods(object):
    "Helps access this module's functions as rasterdata class methods by automatically inserting self as the first arg"
    def __init__(self, data, module):
        from functools import wraps
        self.data = data

        for k,v in module.__dict__.items():
            if hasattr(v, "__call__") and not v.__name__.startswith("_"):
                func = v
                def as_method(func):
                    @wraps(func)
                    def firstarg_inserted(*args, **kwargs):
                        # wrap method to insert self data as the first arg
                        args = [self.data] + list(args)
                        return func(*args, **kwargs)
                    return firstarg_inserted
                self.__dict__[k] = as_method(func)

##########

class Cell(object):
    """
    Cell class representing a particular pixel/cell in a raster band instance. 
    """
    def __init__(self, band, col, row):
        """
        Cell is instantiated by referencing the paremt raster band to which it belongs, and the cell's
        column and row location within that grid.
        Usually created by the parent raster band class, the user should not have to create this. 
        
        Args:
            band: Parent band instance.
            col: Column number location of the cell (zero-indexed).
            row: Row number location of the cell (zero-indexed).

        Attributes:
            band: Parent band instance.
            col: Column number location of the cell (zero-indexed).
            row: Row number location of the cell (zero-indexed).
            x: X coordinate of the cell's midpoint.
            y: Y coordinate of the cell's midpoint.
            bbox: Bounding box of the cell in the form [leftx,uppery,rightx,lowery]
            point: GeoJSON representation of the cell's midpoint.
            polygon: GeoJSON representation of the cell as a rectangular polygon.
            value: The cell's value. Setting this value saves the change to the parent raster band.
            neighbours: Returns all 8 neighbours as a list of cell instances. 
        """
        
        self.band = band
        self.col, self.row = col, row
        if not (0 <= col < self.band.width and 0 <= row < self.band.height):
            raise Exception('Cell index {} out of bounds'.format((col,row)))
        self._x = None
        self._y = None

    def __repr__(self):
        return "Cell(col=%s, row=%s, value=%s)" %(self.col, self.row, self.value)

    def __geo_interface__(self):
        return self.poly
    
    @property
    def point(self):
        return {"type":"Point",
                "coordinates": (self.x, self.y)}

    @property
    def poly(self):
        trans = self.band._rast.cell_to_geo
        return {"type":"Polygon",
                "coordinates": [[trans(self.col, self.row),
                                 trans(self.col, self.row+1),
                                 trans(self.col+1, self.row+1),
                                 trans(self.col+1, self.row),
                                 ]]}

    @property
    def bbox(self):
        trans = self.band._rast.cell_to_geo
        corners = self.poly["coordinates"][0]
        xs,ys = zip(*corners)
        return min(xs),min(ys),max(xs),max(ys)

    @property
    def x(self):
        if None in (self._x,self._y):
            self._x, self._y = self.band._rast.cell_to_geo(self.col, self.row)
        return self._x

    @property
    def y(self):
        if None in (self._x,self._y):
            self._x, self._y = self.band._rast.cell_to_geo(self.col, self.row)
        return self._y

    @property
    def value(self):
        if not self.band._pixelaccess:
            self.band._pixelaccess = self.band.img.load()
        return self.band._pixelaccess[self.col, self.row]

    @value.setter
    def value(self, newval):
        if not self.band._pixelaccess:
            self.band._pixelaccess = self.band.img.load()
        self.band._pixelaccess[self.col, self.row] = newval

    @property
    def neighbours(self):
        nw = Cell(self.band, self.col - 1, self.row + 1)
        n = Cell(self.band, self.col, self.row + 1)
        ne = Cell(self.band, self.col + 1, self.row + 1)
        e = Cell(self.band, self.col + 1, self.row)
        se = Cell(self.band, self.col + 1, self.row - 1)
        s = Cell(self.band, self.col, self.row - 1)
        sw = Cell(self.band, self.col - 1, self.row - 1)
        w = Cell(self.band, self.col - 1, self.row)
        return [nw,n,ne,e,se,s,sw,w]
        

class Band(object):
    """
    Band class representing a raster band or grid containing values,
    either standalone or as part of a raster instance.
    """
    def __init__(self, img=None, mode=None, width=None, height=None, nodataval=-9999):
        """Initiate empty band (filled with "nodataval", defaults to -9999) by setting
        data type "mode", "width", and "height".
        
        Alternatively, load existing data structure from a PIL image given in "img".

        Band instances supports all of Python's math operators, so that "band1 + band2" returns
        a new band instance where the corresponding cell values have been added together.
        This requires that all band instances are of the same dimensions.
        Using constant numbers in the expression is interpreted as a band where
        all cells have that value. For example, "band1 + 3" adds 3 to all cells in band1. 
        Comparison operations like == or > are also supported and will return 1bit bands.
        Logical operators are also supported. Especially useful is the & operator which returns
        a binary band where cells from both bands are valid (intersection), the | operator which returns a binary
        band where cell's from any of the two bands are valid (union), and the ^ operator which
        returns a binary band where each band is different from the other band (symmetrical difference).

        TODO: Add tests to make sure the math and logical operators work correctly.

        TODO: Fix so that right math operators work with numbers, now it just tries to call ._operator in reverse.

        Iterating over the band loops over each individual cell instance of the band, in order to
        manipulate the band data on a cell-by-cell level. 

        Args:
            img: Existing PIL image instance to load data from. 
            mode: Sets the data type. Only required when creating empty band.
                - float32
                - float16
                - int32
                - int16
                - int8
                - 1bit
            width/height: The width/height of the band data. Only required when creating empty band.
            nodataval: The value to be interpreted as nodata, defaults to -9999. 

        Attributes:
            nodataval: The data value that represents nodata/missing data. This attribute can be changed
                manually, which will update the mask. 
            width/height: References the width/height of the band. 
            mode: References the data type of the band. 
            mask: Returns the nodata mask of the band, in the form of a binary PIL image with 255
                for the pixels that are considered nodata.

                The user can also set the mask attribute by supplying another band's mask (or any binary PIL image),
                which overwrites pixel values in the masked areas with nodataval. 
                
                NOTE: This mask is dynamically calculated from the location of nodata values.
                Repeated references use a cached version of the mask ("_cached_mask"). 
                The class is designed so that any changes to the band's values or nodataval
                also clears the cached mask so the correct mask can be calculated on next call.

                TODO: Add tests to make sure this is implemented systematically. 
            img: Reference to the underlaying data storage, which is stored in a PIL image.
                Should not be used by the user, mostly used internally.
            _rast: On rare occasions, this hidden attribute might be useful to get a reference to
                the raster instance that owns this band. Returns None if standalone band. 
        """

        if not img:
            if all((mode,width,height)):
                pilmode = rastmode_to_pilmode(mode)
                img = PIL.Image.new(pilmode, (width, height))
            else:
                raise Exception("Mode, width, and height must be specified when creating a new empty band from scratch")

        if isinstance(img, basestring):
            img = PIL.Image.open(img)
        self.img = img

        self._pixelaccess = None
        self._cached_mask = None
        self._rast = None  # reference to owning raster, internal only

        self.nodataval = nodataval # calls on the setter method

    def __iter__(self):
        width,height = self.img.size
        for row in range(height):
            for col in range(width):
                yield Cell(self,col,row)

    def __repr__(self):
        import pprint
        metadict = dict(img=self.img,
                        nodataval=self.nodataval)
        return "Band object:\n" + pprint.pformat(metadict, indent=4)

    def _operator(self, other, op, reversed=False): 
        # calculate math
        # basic math + - * / ** %
        # note: logical ops ~ & | ^ makes binary mask and return the pixel value where mask is valid
        # note: relational ops < > == != return only binary mask
        # note: other useful is min() and max(), equiv to (r1 < r2) | r2
        # import numpy as np

        # self_data = np.array(self.img)
        # if isinstance(other, Band):
        #     other_data = np.array(other.img)
        # else:
        #     other_data = other # float or int
        
        # datas = {"d1": self_data, "d2": other_data}
        # if reversed:
        #     result = eval("d2 %s d1" % op, None, datas)
        # else:
        #     result = eval("d1 %s d2" % op, None, datas)
        # del self_data, other_data, datas

        # # should maybe create a combined mask of nullvalues for all rasters
        # # and filter away those nullcells from math result
        # # ...
        # mask = np.array(self.mask)
        # if isinstance(other, Band):
        #     mask = mask | np.array(other.mask)
        # else:
        #     # other is float or int, only self has a mask
        #     pass

        # # return result
        # outband = Band(img=PIL.Image.fromarray(result))
        # del result
        # outband.mask = PIL.Image.fromarray(mask)
        # del mask
        # return outband

        if isinstance(other, (float,int)):
            if isinstance(other, int):
                md = "I"
            elif isinstance(other, float):
                md = "F"
            _oimg = PIL.Image.new(md, (self.width,self.height), other)
            other = Band(img=_oimg)
        
        if reversed:
            bands = {"b1": other.img, "b2": self.img}
        else:
            bands = {"b1": self.img, "b2": other.img}
        if any((sym in op for sym in "&|^=><!")):
            img = PIL.ImageMath.eval("convert((b1 %s b2)*255, '1')" % op, **bands)
        else:
            img = PIL.ImageMath.eval("b1 %s b2" % op, **bands)

        # should maybe create a combined mask of nullvalues for all rasters
        # and filter away those nullcells from math result
        # ...
        masks = {"m1": self.mask, "m2": other.mask}
        mask = PIL.ImageMath.eval("convert(m1 | m2, '1')", **masks) # union of all masks

        # return result
        outband = Band(img=img)
        outband.mask = mask
        return outband

    def _roperator(self, other, op): 
        # same as operator but self is to the right, eg "100 - self"
        return self._operator(other, op, reversed=True)

        # # convert other to band if necessary
        # if isinstance(other, (float,int)):
        #     if isinstance(other, int):
        #         md = "I"
        #     elif isinstance(other, float):
        #         md = "F"
        #     _oimg = PIL.Image.new(md, (self.width,self.height), other)
        #     other = Band(img=_oimg)
        
        # # apply the operator from the perspective of the other band
        # return other._operator(self, op)



    def __add__(self, other):
        return self._operator(other, "+")

    def __sub__(self, other):
        return self._operator(other, "-")

    def __mul__(self, other):
        return self._operator(other, "*")

    def __div__(self, other):
        return self._operator(other, "/")

    def __truediv__(self, other):
        return self._operator(other, "/")

    def __pow__(self, other):
        return self._operator(other, "**")



    def __radd__(self, other):
        return self._roperator(other, "+")

    def __rsub__(self, other):
        return self._roperator(other, "-")

    def __rmul__(self, other):
        return self._roperator(other, "*")

    def __rdiv__(self, other):
        return self._roperator(other, "/")

    def __rtruediv__(self, other):
        return self._roperator(other, "/")

    def __rpow__(self, other):
        return self._roperator(other, "**")

    


    def __and__(self, other):
        return self._operator(other, "&")

    def __or__(self, other):
        return self._operator(other, "|")

    def __xor__(self, other):
        return self._operator(other, "^")



    def __lt__(self, other):
        return self._operator(other, "<")

    def __le__(self, other):
        return self._operator(other, "<=")

    def __eq__(self, other):
        return self._operator(other, "==")

    def __ne__(self, other):
        return self._operator(other, "!=")

    def __gt__(self, other):
        return self._operator(other, ">")

    def __ge__(self, other):
        return self._operator(other, ">=")
    


    @property
    def width(self):
        return self.img.size[0]

    @property
    def height(self):
        return self.img.size[1]

    @property
    def mode(self):
        return pilmode_to_rastmode(self.img.mode)

    @property
    def nodataval(self):
        return self._nodataval

    @nodataval.setter
    def nodataval(self, nodataval):
        if nodataval != None:
            if self.mode == "1bit":
                self._nodataval = int(nodataval)
            elif self.mode.startswith("int"):
                self._nodataval = int(nodataval)
            elif self.mode.startswith("float"):
                self._nodataval = float(nodataval)
        else:
            self._nodataval = nodataval
            
        # reset mask cache
        self._cached_mask = None

        # also reset the mask cache of the parent raster
        if self._rast:
            self._rast._cached_mask = None

    def get(self, col, row):
        """Returns the cell instance located in the specified column and row numbers."""
        return Cell(self, col, row)

    def set(self, col, row, value):
        """Sets the value of the cell located in the specified column and row numbers."""
        if not (0 <= col < self.width and 0 <= row < self.height):
            raise Exception('Cell index {} out of bounds'.format((col,row)))
        if not self._pixelaccess:
            self._pixelaccess = self.img.load()
        self._pixelaccess[col,row] = value

    @property
    def mask(self):
        if self._cached_mask:
            return self._cached_mask

        else:
            nodata = self.nodataval
            if nodata != None:
                # mask out nodata
                mask = self._conditional("val == %s" %nodata)
                
            else: 
                # EVEN IF NO NODATA, NEED TO CREATE ORIGINAL MASK,
                # TO PREVENT INFINITE OUTSIDE BORDER AFTER GEOTRANSFORM
                mask = PIL.Image.new("1", self.img.size, 0)
                
            self._cached_mask = mask
            return self._cached_mask

    @mask.setter
    def mask(self, newmask):
        """Note, newmask must be PIL image and match band dimensions"""
        
        # paste nodatavals where mask is true
        self.img.paste(self.nodataval, mask=newmask)
        
        # cache it
        self._cached_mask = newmask

        # also reset the mask cache of the parent raster
        if self._rast:
            self._rast._cached_mask = None

    def is_empty(self):
        """Returns True if the grid is empty, ie only contains nodata values."""
        if self.nodataval and self.summarystats("count")["count"] == 0:
            return True
        else:
            return False

    def compute(self, expr, condition=None):
        """Apply the given expression to recompute all cell values, or limited to a subset
        of cells that meet a particular condition. This method changes the band data in place.
        Nodata values will remain unchanged. 

        Args:
            expr: A string expression using Python math syntax, using the keyword "val" to
                reference the value of each cell. For instance, "val * 3" changes the band
                so that all cell values are timed by 3.
            condition (optional): If given, the expression will only be computed for those cells
                that meet the given criteria. This can be either a preexisting binary band, or
                a conditional string expression for calculating such a binary band. 
        """
        # get the mask before changing values
        mask = None
        if self.nodataval != None:
            mask = self.mask

        if condition:
            if isinstance(condition, basestring):
                condition = self.conditional(condition)
            condition = condition.img

        # change values
        self._compute(expr, condition)

        # use the original nodatamask to set null values again
        if mask:
            self.img.paste(self.nodataval, (0,0), mask)

        return self

    def _compute(self, expr, condition=None):
        """Internal only"""
        # import numpy as np
        # if "val" in expr:
        #     arr = np.array(self.img)
        #     result_arr = eval(expr, {}, {"val":arr})
        #     del arr
        #     result = PIL.Image.fromarray(result_arr) # img
        #     del result_arr
        # else:
        #     result = eval(expr) # constant value
        # if condition:
        #     self.img.paste(result, None, condition)
        # else:
        #     self.img.paste(result)
        # del result

        try:
            if "val" in expr:
                expr = "convert(%s, '%s')" % (expr, self.img.mode)
                result = PIL.ImageMath.eval(expr, val=self.img)
            else:
                val = eval(expr)
                result = PIL.Image.new(self.img.mode, self.img.size, val)
            if condition:
                self.img.paste(result, (0,0), condition)
            else:
                self.img.paste(result, (0,0))
        
        except MemoryError:
            raise
            # if not self._pixelaccess:
            #     self._pixelaccess = self.img.load()

            # # force mode
            # if self.mode.startswith(("int","1bit")):
            #     forcetype = int
            # elif self.mode.startswith("float"):
            #     forcetype = float
                                    
            # # force value range
            # if self.mode == "1bit":
            #     forcerange = lambda v: min(max(v,0),1)
            # elif self.mode.endswith("8"):
            #     forcerange = lambda v: min(max(v,0),255)
            # else:
            #     forcerange = lambda v: v

            # if condition:
            #     condpx = condition.load()
            #     for y in range(self.width):
            #         for x in range(self.height):
            #             if condpx[x,y]:
            #                 val = self._pixelaccess[x,y]
            #                 newval = eval(expr, {}, {"val":val})
            #                 self._pixelaccess[x,y] = forcetype(forcerange(newval))
            # else:
            #     for y in range(self.width):
            #         for x in range(self.height):
            #             val = self._pixelaccess[x,y]
            #             newval = eval(expr, {}, {"val":val})
            #             self._pixelaccess[x,y] = forcetype(forcerange(newval))

    def recode(self, condition, newval):
        """Change to a new value for those pixels that meet a condition.

        Similar to compute, but specifically for recoding only a subset of cell values or
        when you wish to "burn" or "stamp" some of the cell values from another band.
        This method changes the band data in place.

        TODO: Does this change nodata values? 

        Args:
            condition: A string expression for specifying which cell values will be recoded.
            newval: The new value that will be set where the condition is true.
                Either a single numeric value (int or float, no expression),
                or a band instance of the same dimensions and mode whose values will be copied
                into the current band where the condition is true. 
        """
        if isinstance(condition, basestring):
            condition = self._conditional(condition)
        if isinstance(newval, Band):
            newval = newval.img
        self.img.paste(newval, (0,0), condition)

        return self

    def conditional(self, condition):
        """Return a binary band showing where a condition is true.

        TODO: Does this, and should it, evaluate to True for nodata values?

        TODO> This returns a binary 1bit mode image, where true is 255. Should that be the case, or 1?

        Args:
            condition: A string expression whose evaluation will determine which cell
                values in the returned binary band will be set to true. 
        """
        
        result = self._conditional(condition)

        # set conditional to false for pixels covered by nodatamask
        #if self.nodataval != None:
        #    # TODO: should conditional really ignore nodatavals??? Prob not actually, should query actual raw values
        #    result.paste(0, (0,0), self.mask)

        band = Band(result, self.nodataval)
        return band

    def _conditional(self, condition):
        """Optimized algorithms for testing condition depending on raster type,
        avoids recursion when calling mask"""

        # import numpy as np
        # arr = np.array(self.img)
        # result_arr = eval(condition, {}, {"val":arr})
        # del arr
        # result = PIL.Image.fromarray(result_arr) # img
        # del result_arr
        # return result
        
        try: 
            # note: relational ops < > == != should return only binary mask, but not sure
            _condition = "convert((%s)*255, '1')" % condition
            result = PIL.ImageMath.eval(_condition, val=self.img)
            
            # times binary results with 255 to make a valid mask
            ####result = PIL.ImageMath.eval("convert(val * 255, '1')", val=result)

        except MemoryError:
            raise
            # result = PIL.Image.new("1", self.img.size, 0)
            # resultpixels = result.load()
            
            # if not self._pixelaccess:
            #     self._pixelaccess = self.img.load()

            # # Note: eval on many pixels is very slow
            # for y in range(self.width):
            #     for x in range(self.height):
            #         val = self._pixelaccess[x,y]
            #         if eval(condition, {}, {"val":val}) != False:
            #             resultpixels[x,y] = 255
            #         else:
            #             resultpixels[x,y] = 0

        return result

    def summarystats(self, *stattypes):
        """Calculates and returns a dictionary of statistics of all values in the
        band. 

        Args:
            *stattypes (optional): If given, only calculates the requested statistics,
                otherwise calculates all statistics.
                    - count
                    - sum
                    - mean
                    - min
                    - max
                    - median
                    - majority
                    - minority
        """
        # get all stattypes unless specified

        statsdict = dict()

        try:
            # convert img and mask to numpy arrays
            import numpy as np
            img_arr = np.array(self.img)
            # get flat arr of valid values
            if self.nodataval is None:
                values = img_arr.flatten()
            else:
                values = img_arr[ img_arr != self.nodataval ]
            
            # retrieve stats
            any_values = len(values) > 0
            if not stattypes or "count" in stattypes:
                statsdict['count'] = len(values) if any_values else None
            if not stattypes or "sum" in stattypes:
                statsdict["sum"] = float(values.sum()) if any_values else None
            if not stattypes or "mean" in stattypes:
                try: statsdict["mean"] = float(values.mean()) if any_values else None
                except ZeroDivisionError: statsdict["mean"] = None
            if not stattypes or "min" in stattypes:
                statsdict["min"] = float(values.min()) if any_values else None
            if not stattypes or "max" in stattypes:
                statsdict["max"] = float(values.max()) if any_values else None
            if not stattypes or "median" in stattypes:
                statsdict["median"] = float(np.median(values)) if any_values else None
            # non-default stats (slower, so have to be explicitly requested)
            if "majority" in stattypes or 'minority' in stattypes:
                vals,counts = np.unique(values, return_counts=True)
                if "majority" in stattypes:
                    i = np.argmax(counts)
                    statsdict["majority"] = float(vals[i]) if any_values else None
                if "minority" in stattypes:
                    i = np.argmin(counts)
                    statsdict["minority"] = float(vals[i]) if any_values else None
                
        except MemoryError:

            # PIL.ImageStat or getcolors() resulted in too many values at once
            # so fallback on manual iteration one pixel at a time

            # TODO: Doesnt work yet, cus previous ImageStat and getcolors results in load() being called
            # TODO: Right now only works if band has parent raster
            # WARNING: Median here is only median of medians

            warnings.warn('Encountered MemoryError during summarystats, trying a slower tiled approach instead')

            import gc
            try:
                del valid
                del stats
            except:
                pass
            gc.collect()

            tilestats = []
            i = self._rast.bands.index(self)
            #print self
            for tile in self._rast.manage.tiled(tilesize=(3000,3000)):
                #print 'tile',tile
                s = tile.bands[i].summarystats(*stattypes)
                tilestats.append(s)
                del tile
                gc.collect()

            def _count():
                counts = [s['count'] for s in tilestats if s['count'] != None]
                return sum(counts) if counts else None
            def _sum():
                sums = [s['sum'] for s in tilestats if s['sum'] != None]
                return sum(sums) if sums else None
            def _mean():
                try: return _sum()/float(_count())
                except: return None
            def _min():
                mins = [s['min'] for s in tilestats if s['min'] != None]
                return min(mins) if mins else None
            def _max():
                maxs = [s['max'] for s in tilestats if s['max'] != None]
                return max(maxs) if maxs else None
            
            if not stattypes or "count" in stattypes:
                statsdict["count"] = _count()
            if not stattypes or "sum" in stattypes:
                statsdict["sum"] = _sum()
            if not stattypes or "mean" in stattypes:
                statsdict["mean"] = _mean()
            if not stattypes or "max" in stattypes:
                statsdict["max"] = _max()
            if not stattypes or "min" in stattypes:
                statsdict["min"] = _min()

            # some more stats
            nodata = self.nodataval
            if not stattypes or "majority" in stattypes or "minority" in stattypes or "median" in stattypes:
                majors = dict()
                minors = dict()
                medians = dict()
                # pass1, get majority for each tile
                for tile in self._rast.manage.tiled(tilesize=(3000,3000)):
                    #print 'pass1',tile
                    freqs = [(cnt,val) for cnt,val in tile.bands[i].img.getcolors(tile.width*tile.height) if val != nodata]
                    sortedfreqs = sorted(freqs, key=lambda e: e[0])
                    if sortedfreqs:
                        if not stattypes or "majority" in stattypes:
                            cnt,val = sortedfreqs[-1]
                            majors[val] = cnt
                        if not stattypes or "minority" in stattypes:
                            cnt,val = sortedfreqs[0]
                            minors[val] = cnt
                        if not stattypes or "median" in stattypes:
                            cnt,val = sortedfreqs[len(sortedfreqs)//2]
                            medians[val] = cnt
                    del freqs,sortedfreqs,tile
                    gc.collect()
                # pass2, collect counts for each majority group
                for tile in self._rast.manage.tiled(tilesize=(3000,3000)):
                    #print 'pass2',tile
                    freqs = [(cnt,val) for cnt,val in tile.bands[i].img.getcolors(tile.width*tile.height) if val != nodata]
                    for cnt,val in freqs:
                        if val in majors:
                            majors[val] += cnt
                        if val in minors:
                            minors[val] += cnt
                    del freqs,tile
                    gc.collect()
                # choose min/majority w lowest/largest count
                if not stattypes or "majority" in stattypes:
                    statsdict["majority"] = sorted(majors.items(), key=lambda e: e[1])[-1][0]
                if not stattypes or "minority" in stattypes:
                    statsdict["minority"] = sorted(minors.items(), key=lambda e: e[1])[0][0]
                if not stattypes or "median" in stattypes:
                    sortedmeds = sorted(medians.keys())
                    statsdict["median"] = sortedmeds[len(sortedmeds)//2]
                
        return statsdict

    def clear(self):
        """Resets all the cell values of the band, setting them to the specified nodataval,
        otherwise setting them to a value of 0.

        TODO: Not fully tested yet. Is 0 a correct value for bands without nodataval? 
        """
        na = self.nodataval
        if na is None: na = 0
        self.img.paste(na)

        return self

    def convert(self, mode):
        """Converts the band and its values to a new data type.
        Changes the band in place. 

        Args:
            mode: The data type mode to be converted to.
                - float32
                - float16
                - int32
                - int16
                - int8
                - 1bit
        """
        pilmode = rastmode_to_pilmode(mode)
        self.img = self.img.convert(pilmode)
        self._pixelaccess = None

        return self

    def copy(self):
        """Copies the band and its data to a new identical band."""
        img = self.img.copy()
        band = Band(img=img, nodataval=self.nodataval)
        if self._cached_mask:
            band._cached_mask = self._cached_mask.copy()
        return band

##    def render(self, width, height, **options):
##        if self._rast:
##            rast = self._rast
##            options.update(bandnum=rast.bands.index(self))
##        else:
##            rast = RasterData(mode=self.mode, width=width, height=height, xoffset=0, yoffset=0, xscale=1, yscale=1)
##            rast.add_band()
##        return rast.render(width, height, **options)
##
##    def view(self, width, height, **options):
##        lyr = self.render(width, height, **options)
##
##        import Tkinter as tk
##        import PIL.ImageTk
##        
##        app = tk.Tk()
##        tkimg = PIL.ImageTk.PhotoImage(lyr.img)
##        lbl = tk.Label(image=tkimg)
##        lbl.tkimg = tkimg
##        lbl.pack()
##        app.mainloop()

    def histogram(self, width=None, height=None, bins=10):
        """Calculates the histogram of the band's data values and draws it on
        a PyAgg canvas which the user can use to view, modify, or save.

        TODO: Is returning it as a PyAgg canvas the best option?

        Args:
            width/height: Desired width/height of the histogram canvas drawing. 
            bins: Number of bins in the histogram. 
        """
        import pyagg
        stats = self.summarystats("min","max")
        binsize = (stats["max"] - stats["min"]) / float(bins)
        bars = []
        cur = stats["min"]
        while cur < stats["max"]:
            below = self.conditional("%s < val" % cur)
            above = self.conditional("val < %s" % (cur+binsize))
            maskimg = PIL.ImageMath.eval("b & a", b=below.img, a=above.img)
            #maskimg.show()
            mask = Band(img=maskimg)
            label = "%s to %s" % (cur, cur+binsize)
            count = mask.summarystats("sum")["sum"]
            bars.append((label,count))
            cur += binsize
        c = pyagg.graph.BarChart()
        c.add_category(name="Title...", baritems=bars)
        return c.draw() # draw returns the canvas

    def render(self, width=None, height=None, bbox=None, title="", background=None, **styleoptions):
        """Shortcut for easily rendering and returning the band data on a Map instance.

        Note: If the band belongs to a raster instance, the band data will be rendered in the coordinate
        system defined in the raster. Otherwise, the coordinate system will be set to match the pixel
        coordinates.

        TODO: Check that works correctly. Have experienced that adding additional layers on top
        of this results in mismatch and layers jumping around. 

        Args:
            width/height: Desired width/height of the rendered map.
            bbox (optional): If given, only renders the given bbox, specified as (xmin,ymin,xmax,ymax).
            title (optional): Title text to print on the map.
            background (optional): Background color, defaults to transparent.
            **styleoptions (optional): How to style the band values, as documented in "renderer.RasterLayer".
        """
        # WARNING: CANNOT USE TO GET A MAP AND THEN ADDING OTHER LAYERS,
        # GETS ALL MISMATCHED AND JUMPS AROUND
        from .. import renderer
        
        rast = self._rast
        if rast:
            styleoptions.update(bandnum=rast.bands.index(self))
        else:
            #raise Exception("Cannot render a freestanding band without a parent raster which is needed for georeferencing")
            rast = RasterData(mode=pilmode_to_rastmode(self.img.mode),
                              bbox=[0,0,self.width,self.height], width=self.width, height=self.height)
            rast.add_band(self)
            styleoptions.update(bandnum=0)
        
        mapp = renderer.Map(width, height, title=title, background=background)
        mapp.add_layer(rast, **styleoptions)
        if bbox:
            mapp.zoom_bbox(*bbox)
        else:
            mapp.zoom_auto()
        mapp.render_all()
        return mapp

    def view(self, width=None, height=None, bbox=None, title="", background=None, **styleoptions):
        """Renders and opens a Tkinter window for viewing and interacting with the map.

        Args are same as for "render()".
        """
        from .. import app
        mapp = self.render(width, height, bbox, title=title, background=background, **styleoptions)
        # make gui
        win = app.builder.SimpleMapViewerGUI(mapp)
        win.mainloop()

    def save(self, filepath):
        """Saves the raw band data to an image file, devoid of any geographic metadata.
        Image format extension can be any of those supported by PIL. 
        """
        self.img.save(filepath)



def Name_generator():
    """Used internally for ensuring default data names are unique for each Python session.

    TODO: Maybe make private. 
    """
    i = 1
    while True:
        yield "Untitled%s" % i
        i += 1


NAMEGEN = Name_generator()



class RasterData(object):
    """The main raster data class."""
    def __init__(self, filepath=None, name=None,
                 data=None,
                 image=None,
                 mode=None, tilesize=None, tiles=None,
                 crs=None,
                 **kwargs):
        """To load from a file simply supply the filepath, and all metadata such as crs and
        affine transform is loaded directly from the file.

        To load from data (list of lists representing a grid), or from an in-memory PIL image,
        you must additionally define the geotransform. 

        To create a new empty raster, supply the data type mode, and define the geotransform.

        Defining the geotransform can be done in several ways:
            1. Set the width/height arguments, and supply an "affine" transform that converts pixel
                coordinates to geographic coordinates, a list of [xscale,xskew,xoffset, yskew,yscale,yoffset].
            2. Set the width/height arguments, and supply each item of the affine transform directly as
                keywords. Only the xscale, yscale, xoffset, and yoffset are required.
                
                The keywords xscale and yscale can also be written as "cellwidth" and "cellheight".

                The keywords xoffset and yoffset can be omitted by instead specifying xy_cell (any cell
                coordinate) along with xy_geo (its equivalent geographic coordinate). This will calculate
                the offsets for you. 
            3. Set the width/height arguments, and define the bbox of the raster. This will calculate the
                remaining affine coefficients for you.
            4. Set the cellwidth/xscale and cellheight/yscale, and define the bbox of the raster. This will
                calculate the necessary width/height to fit inside the bbox.

        TODO: Thoroughly test that all of these combinations will create the geotransform without error.

        TODO: Also figure out if the affine points to the middle of each cell or the upperleft. 

        The raster has a length equal to the number of bands it contains, and iterating over the raster
        loops through each of the bands.

        Args:
            filepath: Filepath of a raster file to load data from.
            name: Specifies the name of the data, otherwise set to "Untitled". 
            data: List of lists representing the rows of the grid from top to bottom.
            image: Existing PIL image instance to load data from.
            mode: Sets the data type. Only required when creating empty band.
                - float32
                - float16
                - int32
                - int16
                - int8
                - 1bit
            **kwargs: Define the geotransform:
                - width/height: The width/height of the raster data. Only required when creating empty raster.
                    Not required if both bbox and affine transform are given, which will calculate the
                    necessary width/height to contain such a coordinate system. 
                - affine
                - xscale
                - xskew
                - xoffset
                - yscale
                - yskew
                - yoffset
                - cellwidth
                - cellheight
                - xy_geo
                - xy_cell

        Attributes:
            filepath: Filepath from which the raster was loaded, otherwise None. 
            name: Name of the raster. 
            width/height: Width/height of this raster. 
            mode: Data type mode of this raster. 
            bands: List of band instances belonging to this raster. 
            crs: Coordinate reference system. Not currently used.
            meta: A dictionary of the raster's metadata.
            rasterdef: A dictionary that defines the raster, its width/height and affine transform.
                TODO: Consider changing the name of rasterdef to geotransform, and returning more than just affine. 
            bbox: Bounding box extents of the raster in the form [xleft,ytop, xright,ybottom].
                TODO: Also figure out how bbox should be specified and stored internally (as the centerpoint of the cells,
                or offsetting by half a pixel to get the corners).
            mask: Returns a nodata mask that represents the shared area of all the band masks, the cells with nodata
                value in all the bands. The mask is a binary PIL image with 255 for the pixels that are considered nodata. 

                The user can also set the mask attribute by supplying another band's mask (or any binary PIL image).
                Setting the raster mask will only serve as a temporary mask but will not change any of the band values. 
                
                NOTE: This mask is dynamically calculated from the location of nodata values.
                Repeated references use a cached version of the mask ("_cached_mask"). 
                The class is designed so that any changes to the band's values or nodataval
                also clears the cached mask so the correct mask can be calculated on next call.
            manager: Accesses the manager subclass and all of its functions, supplying itself as the first argument.
            analyzer: Accesses the analyzer subclass and all of its functions, supplying itself as the first argument. 
            
        """
        self.filepath = filepath
        self.name = name or filepath
        if not self.name:
            self.name = next(NAMEGEN)

        # load
        if filepath:
            georef, nodataval, bands, crs = loader.from_file(filepath, crs=crs, **kwargs)
        elif data:
            georef, nodataval, bands, crs = loader.from_lists(data, crs=crs, **kwargs)
        elif image:
            georef, nodataval, bands, crs = loader.from_image(image, crs=crs, **kwargs)
        else:
            georef, nodataval, bands, crs = loader.new(crs=crs, **kwargs)

        # determine dimensions
        if any((filepath,data,image)):
            self.width, self.height = bands[0].size
            self.mode = pilmode_to_rastmode(bands[0].mode)

        else:            
            # auto set width of new raster based on georef if needed
            if "width" not in georef:
                if "cellwidth" in georef:
                    georef["xscale"] = georef["cellwidth"]
                if "bbox" in georef and "xscale" in georef:
                    xwidth = georef["bbox"][2] - georef["bbox"][0]
                    georef["width"] = abs(int(round( xwidth / float(georef["xscale"]) )))
                    # adjust bbox based on rounded width
                    x1,y1,x2,y2 = georef["bbox"]
                    xwidth = georef["xscale"] * georef["width"]
                    georef["bbox"] = x1,y1,x1+xwidth,y2
                else:
                    raise Exception("Either the raster width or a bbox and xscale must be set")

            # auto set height of new raster based on georef if needed
            if "height" not in georef:
                if "cellheight" in georef:
                    georef["yscale"] = georef["cellheight"]
                if "bbox" in georef and "yscale" in georef:
                    yheight = georef["bbox"][3] - georef["bbox"][1]
                    georef["height"] = abs(int(round( yheight / float(georef["yscale"]) )))
                    # adjust bbox based on rounded width
                    x1,y1,x2,y2 = georef["bbox"]
                    yheight = georef["yscale"] * georef["height"]
                    georef["bbox"] = x1,y1,x2,y1+yheight
                else:
                    raise Exception("Either the raster height or a bbox and yscale must be set")

            # set the rest
            self.width = georef["width"]
            self.height = georef["height"]
            if mode:
                self.mode = mode
            else:
                raise Exception("A mode must be specified when creating a new empty raster from scratch")

        self.bands = [Band(img, nodataval=nodataval) for img in bands]
        self._cached_mask = None

        # standalone mask? 
        # TODO: make this more robust, read from various inputs above (filepath,img,data,etc)
        if len(self.bands) == 4:
            # rgba
            a = self.bands[-1]
            a.nodataval = 0
            self._cached_mask = a.mask

        # store reference to raster to enable pixel convenience methods
        for b in self.bands:
            b._rast = self

        # set metadata
        self.set_geotransform(**georef)

        # crs
        defaultcrs = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"
        crs = crs or defaultcrs
        if not isinstance(crs, pycrs.CS):
            try:
                crs = pycrs.parse.from_unknown_text(crs)
            except:
                warnings.warn('Failed to parse the given crs format, falling back to unprojected lat/long WGS84: \n {}'.format(crs))
                crs = pycrs.parse.from_proj4(defaultcrs)
        self.crs = crs

    def __len__(self):
        return len(self.bands)
    
    def __iter__(self):
        for band in self.bands:
            yield band

    def __repr__(self):
        return "<Raster data: mode={mode} bands={bands} size={size} bbox={bbox}>".format(mode=self.mode,
                                                                                         bands=len(self),
                                                                                         size=(self.width,self.height),
                                                                                         bbox=self.bbox)

    @property
    def meta(self):
        metadict = dict(bands=len(self),
                        mode=self.mode,
                        width=self.width,
                        height=self.height,
                        bbox=self.bbox,
                        affine=self.affine,
                        crs=self.crs,
                        )
        return metadict

    @property
    def rasterdef(self):
        metadict = dict(width=self.width,
                        height=self.height,
                        affine=self.affine,
                        #crs=self.crs # needed?
                        )
        return metadict

    @property
    def bbox(self):
        # get corner coordinates of raster
        xleft_coord,ytop_coord = self.cell_to_geo(0, 0)
        xright_coord,ybottom_coord = self.cell_to_geo(self.width, self.height) # width and height is actually top left corner of one pixel beyond last
        return [xleft_coord,ytop_coord,xright_coord,ybottom_coord]

    def copy(self, shallow=False):
        """Returns a copy of the raster data along with copies of all of its band data,
        unless shallow is set to True."""
        new = RasterData(**self.meta)
        if shallow:
            new.bands = []
        else:
            for band in self.bands:
                new.add_band(band.copy())
            new._cached_mask = self._cached_mask
        return new

    def get(self, x, y, band):
        """Given a geographic xy coordinate, return the cell belonging to the given band index."""
        if not isinstance(band, Band):
            band = self.bands[band]

        col,row = self.geo_to_cell(x, y)
        return band.get(col,row)

    def set(self, x, y, value, band):
        """Given a geographic xy coordinate, set the cell value belonging to the given band index."""
        if not isinstance(band, Band):
            band = self.bands[band]

        col,row = self.geo_to_cell(x, y)
        band.set(col,row,value)

    def add_band(self, band=None, **kwargs):
        """Adds a band to the raster, and associates it with the raster.
        Either supply an existing Band class, or use kwargs as init args to be used to
        construct a new Band class. 
        """
        if band:
            pass
        else:
            defaultargs = dict(mode=self.mode,
                               width=self.width,
                               height=self.height)
            defaultargs.update(kwargs)
            band = Band(**defaultargs)
            
        # check constraints
        if not band.width == self.width or not band.height == self.height:
            raise Exception("Added band must have the same dimensions as the raster dataset")

        elif band.mode != self.mode:
            raise Exception("Added band must have the same mode as the raster dataset")

        # store reference to raster to enable pixel convenience methods
        band._rast = self

        self.bands.append(band)

        return band

    def set_geotransform(self, **georef):
        """Updates the geotransform of the raster.

        Set it by supplying any of the affine coefficients as expected when creating a new Raster class.
        Any changes to the geotransform must be set with this method, since it will calculate the affine
        and the inverse affine coefficients behind the scenes. 
        """
        
        # get coefficients needed to convert from raster to geographic space
        if "affine" in georef:
            # directly from affine coefficients
            [xscale, xskew, xoffset,
             yskew, yscale, yoffset] = georef["affine"]
        else:
            # more intuitive way
            current = dict(zip("xscale xskew xoffset yskew yscale yoffset".split(), self.meta["affine"]))
            current.update(georef)
            georef = current
            [xscale, xskew, xoffset, yskew, yscale, yoffset] = loader.compute_affine(**georef)

        self.affine = [xscale, xskew, xoffset, yskew, yscale, yoffset]

        # and the inverse coefficients to go from geographic space to raster
        # taken from Sean Gillies' "affine.py"
        a,b,c,d,e,f = self.affine
        det = a*e - b*d
        if det != 0:
            idet = 1 / float(det)
            ra = e * idet
            rb = -b * idet
            rd = -d * idet
            re = a * idet
            a,b,c,d,e,f = (ra, rb, -c*ra - f*rb,
                           rd, re, -c*rd - f*re)
            self.inv_affine = a,b,c,d,e,f
        else:
            raise Exception("Error with the transform matrix, \
                            a raster should not collapse upon itself")

    def cell_to_geo(self, column, row):
        """Returns the geographic coordinate for the given column-row."""
        [xscale, xskew, xoffset, yskew, yscale, yoffset] = self.affine
        x, y = column, row
        x_coord = x*xscale + y*xskew + xoffset
        y_coord = x*yskew + y*yscale + yoffset
        return x_coord, y_coord

    def geo_to_cell(self, x, y, fraction=False):
        """Returns the column-row for the given geographic coordinate."""
        [xscale, xskew, xoffset, yskew, yscale, yoffset] = self.inv_affine
        column = x*xscale + y*xskew + xoffset
        row = x*yskew + y*yscale + yoffset
        if not fraction:
            # round to nearest cell
            column,row = math.floor(column), math.floor(row)
            column,row = int(column),int(row)
        return column,row

    @property
    def mask(self):
        if self._cached_mask:
            return self._cached_mask

        else:
            if len(self) == 1:
                mask = self.bands[0].mask
            elif len(self) > 1:
                # mask out where all band masks are true
                masks_namedict = dict([("mask%i"%i, band.mask) for i,band in enumerate(self.bands) ])
                expr = " & ".join(masks_namedict.keys())
                mask = PIL.ImageMath.eval(expr, **masks_namedict).convert("1")

            self._cached_mask = mask
            return self._cached_mask

    @mask.setter
    def mask(self, value):
        self._cached_mask = value

    def convert(self, mode):
        """Converts all the bands to a new data type mode."""
        for band in self:
            band.convert(mode)
        self.mode = mode

    def save(self, filepath, **kwargs):
        """Saves the raster data as a geographic file."""
        saver.to_file(self.bands, self.meta, filepath, **kwargs)


    ### ACCESS TO ADVANCED METHODS FROM INTERNAL MODULES ###

    def resample(self, **kwargs):
        """TODO: drop, switch this everywhere to .manage.resample()."""
        from . import manager
        kwargs["raster"] = self
        return manager.resample(**kwargs)

    @property
    def manage(self):
        from . import manager
        return _ModuleFuncsAsClassMethods(self, manager)

    @property
    def analyze(self):
        from . import analyzer
        return _ModuleFuncsAsClassMethods(self, analyzer)
    

    ##############################
    # Rendering

    def map(self, width=None, height=None, bbox=None, title="", background=None, crs=None, **styleoptions):
        """Shortcut for easily creating a Map instance containing this dataset as a layer.

        TODO: Check that works correctly. Have experienced that adding additional layers on top
        of this results in mismatch and layers jumping around. 

        Args:
            width/height: Desired width/height of the rendered map.
            bbox (optional): If given, only renders the given bbox, specified as (xmin,ymin,xmax,ymax).
            title (optional): Title text to print on the map.
            background (optional): Background color, defaults to transparent.
            **styleoptions (optional): How to style the raster values, as documented in "renderer.RasterLayer".
        """
        from .. import renderer
        crs = crs or self.crs
        mapp = renderer.Map(width, height, title=title, background=background, crs=crs)
        mapp.add_layer(self, **styleoptions)
        if bbox:
            mapp.zoom_bbox(*bbox)
        else:
            mapp.zoom_auto()
        return mapp

    def view(self, width=None, height=None, bbox=None, title="", background=None, crs=None, **styleoptions):
        """Opens a Tkinter window for viewing and interacting with the map.

        Args are same as for "map()".
        """
        from .. import app
        mapp = self.map(width, height, bbox, title=title, background=background, crs=crs, **styleoptions)
        # make gui
        mapp.view()
        #win = app.builder.SimpleMapViewerGUI(mapp)
        #win.mainloop()


def pilmode_to_rastmode(mode):
    rastmode = {"1":"1bit",
                
                "L":"int8",
                "P":"int8",
                
                "I;16":"int16",
                "I":"int32",
                
                "F;16":"float16",
                "F":"float32"}[mode]
    
    return rastmode

def rastmode_to_pilmode(mode):
    pilmode = {"1bit":"1",
                
                "int8":"L",
                "int8":"L",
                
                "int16":"I;16",
                "int32":"I",
                
                "float16":"F;16",
                "float32":"F"}[mode]
    
    return pilmode
