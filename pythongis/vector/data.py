"""
Module containing the data structures and interfaces for operating with vector datasets.
"""

# import builtins
import sys, os, itertools, operator, math
from collections import OrderedDict
import datetime
import warnings

# import shapely geometry compatibility functions
# ...and rename them for clarity
import shapely
from shapely.geometry import shape as geojson2shapely

# import pycrs
import pycrs

# import internal modules
from . import loader
from . import saver
from . import spindex

# PY3 fix
try: 
    basestring
except NameError:
    basestring = (bytes,str) # PY3



DEFAULT_SPATIAL_INDEX = 'rtree'



class _ModuleFuncsAsClassMethods(object):
    "Helps access this module's functions as vectordata class methods by automatically inserting self as the first arg"
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





def is_missing(val):
    return val is None or (isinstance(val, float) and math.isnan(val))






class Feature:
    """
    Class representing a vector data feature. 
    A feature object contains attributes/properties describing the feature, 
    equivalent to a row in a table, and a geometry describing its spatial representation. 
    
    Supports getting and setting feature properties/attributes via indexing, e.g. feat["name"] 
    gets or sets the feature's value for the name field. Raises an error if no such field name exists. 
    
    Supports the __geo_interface__ protocol, returning a GeoJSON feature object dictionary. 
    
    TODO: 
    - Change the geometry property, represented as a Geometry class instead of a pure GeoJSON dictinoary. 
    - Move all geometry calculations such as length, area, etc., to the geometry class. 
    
    Attributes:
        row: A list of values describing the feature properties as listed in the parent dataset's fields. 
        geometry: A GeoJSON dictionary describing the feature geometry, or None for features without geometry. 
        bbox: The bounding box of the feature, as a list of [xmin,ymin,xmax,ymax]. 
        _cached_bbox: A cached version of the feature's bounding box, to avoid having to repeat the calculation each time. 
            All methods that change the feature's geometry should reset this cache to None in order to recalculate the bbox. 
            In case of errors, the user may reset this themselves by setting it to None. 
            
            TODO:
            - Make sure all methods that alter the geometry indeed does reset the cache. 
        
        length: Returns the cartesian length of the feature geometry, expressed in units of the coordinate system. 
            See Shapely docs for more. 
        geodetic_length: Returns the geodetic length of the feature geometry, expressed as km distance as calculated by the 
            vincenty algorithm. 
        area: Returns the cartesian area of the feature geometry, expressed in units of the coordinate system. 
            See Shapely docs for more. 
        
        id: The feature's ID in the parent vector dataset. 
        _data: The parent vector dataset to which the feature belongs. 
    
    """
    def __init__(self, data, row=None, geometry=None, id=None):
        """
        Creates new feature class.
        Mostly used internally by the VectorData class. 
        The user should instead use the VectorData's add_feature() method. 
        
        Args:
            data: The parent vector dataset to which the feature belongs. Necessary in order to access the feature row's field names. 
            row (optional): A list or dictionary of values describing the feature properties as listed in the parent dataset's fields. 
                Lists must be of the same sequence and length as the dataset fields. 
                Dictionaries sets only the specified fields, the rest defaulting to None. 
            geometry (optional): A GeoJSON dictionary describing the feature geometry, or None. 
            id (optional): If given, manually sets the feature's ID in the parent vector dataset. Otherwise, automatically assigned. 
        """
        self._data = data
        if row:
            if isinstance(row, list):
                if len(row) != len(self._data.fields):
                    raise Exception("Row list must be of same length as parent dataset's field list")
                row = list(row)
            elif isinstance(row, dict):
                for fn in row.keys():
                    if fn not in self._data.fields:
                        raise Exception("Field name '%s' does not exist" % fn)
                row = [row.get(fn, None) for fn in self._data.fields]
        else:
            row = [None for _ in self._data.fields]
        self.row  = row

        if geometry:
            geometry = geometry.copy()
            bbox = geometry.get("bbox")
            self._cached_bbox = bbox
        else:
            self._cached_bbox = None

        self.geometry = geometry

        # ensure it is same geometry type as parent
        if self.geometry:
            geotype = geometry["type"]
            if self._data.type: 
                if "Point" in geotype and self._data.type == "Point": pass
                elif "LineString" in geotype and self._data.type == "LineString": pass
                elif "Polygon" in geotype and self._data.type == "Polygon": pass
                else:
                    raise TypeError("Each feature geometry must be of the same type as the file it is attached to")
            else: self._data.type = self.geometry["type"].replace("Multi", "")
        
        if id == None: id = next(self._data._id_generator)
        self.id = id

    def __getitem__(self, i):
        if isinstance(i, basestring):
            i = self._data.fields.index(i)
        return self.row[i]

    def __setitem__(self, i, setvalue):
        if isinstance(i, basestring):
            i = self._data.fields.index(i)
        self.row[i] = setvalue

    @property
    def __geo_interface__(self):
        return dict(type="Feature",
                    geometry=self.geometry,
                    properties=dict(zip(self._data.fields,self.row))
                    )

    @property
    def bbox(self):
        if not self.geometry:
            raise Exception("Cannot get bbox of null geometry")
        if not self._cached_bbox:
            geotype = self.geometry["type"]
            coords = self.geometry["coordinates"]

            if geotype == "Point":
                x,y = coords
                bbox = [x,y,x,y]
            elif geotype in ("MultiPoint","LineString"):
                xs, ys = zip(*coords)
                bbox = [min(xs),min(ys),max(xs),max(ys)]
            elif geotype == "MultiLineString":
                xs = [x for line in coords for x,y in line]
                ys = [y for line in coords for x,y in line]
                bbox = [min(xs),min(ys),max(xs),max(ys)]
            elif geotype == "Polygon":
                exterior = coords[0]
                xs, ys = zip(*exterior)
                bbox = [min(xs),min(ys),max(xs),max(ys)]
            elif geotype == "MultiPolygon":
                xs = [x for poly in coords for x,y in poly[0]]
                ys = [y for poly in coords for x,y in poly[0]]
                bbox = [min(xs),min(ys),max(xs),max(ys)]
            self._cached_bbox = bbox
        return self._cached_bbox

    def get_shapely(self):
        """Creates and returns the shapely object of the feature geometry. 
        
        NOTE: 
        - Repeated calling of this method can cause considerable overhead.
        - Will be depreceated once all geometry operations are outsourced to a Geometry class. 
        """
        if not self.geometry:
            raise Exception("Cannot get shapely object of null geometry")
        return geojson2shapely(self.geometry)                

    def copy(self):
        """Copies the feature and returns a new instance."""
        geoj = self.geometry
        if self.geometry and self._cached_bbox: geoj["bbox"] = self._cached_bbox
        return Feature(self._data, self.row, geoj)

    def iter_points(self):
        """Yields every point in the geometry as a flat generator,
        useful for quick inspecting of dimensions so don't have to
        worry about nested coordinates and geometry types. For polygons
        this includes the coordinates of both exterior and holes."""
        geoj = self.geometry
        if not geoj:
            yield None

        geotype = self.geometry["type"]
        coords = self.geometry["coordinates"]
        
        if geotype == "Point":
            yield geoj["coordinates"]
        elif geotype in ("MultiPoint","LineString"):
            for p in coords:
                yield p
        elif geotype == "MultiLineString":
            for line in coords:
                for p in line:
                    yield p
        elif geotype == "Polygon":
            for ext_or_hole in coords:
                for p in ext_or_hole:
                    yield p
        elif geotype == "MultiPolygon":
            for poly in coords:
                for ext_or_hole in poly:
                    for p in ext_or_hole:
                        yield p

    def transform(self, func):
        """
        Transforms the feature geometry in place. 
        Func is a function that takes a flat list of coordinates, does something, and returns them.
        For points, the function is applied to a list containing a single coordinate, separately for each multipart. 
        For linestrings, the function is applied to each multipart. 
        For polygons, the function is applied to the exterior, and to each hole if any. 
        """
        geoj = self.geometry
        if not geoj:
            return None

        geotype = geoj["type"]
        coords = geoj["coordinates"]

        def wrapfunc(coords):
            # transform coords using func
            coords = func(coords)
            # only keep points that are not inf or nan
            def isvalid(p):
                x,y = p
                return not (math.isinf(x) or math.isnan(x) or math.isinf(y) or math.isnan(y))
            coords = [p for p in coords if isvalid(p)]
            return coords
        
        if geotype == "Point":
            coords = wrapfunc([coords])
            if coords:
                geoj["coordinates"] = coords[0]
            else:
                self.geometry = None
        elif geotype in ("MultiPoint","LineString"):
            geoj["coordinates"] = wrapfunc(coords)
            if not geoj["coordinates"]:
                self.geometry = None
        elif geotype == "MultiLineString":
            coords = [wrapfunc(line)
                     for line in coords]
            geoj["coordinates"] = [line for line in coords if line]
            if not any(geoj["coordinates"]):
                self.geometry = None
        elif geotype == "Polygon":
            coords = [wrapfunc(ext_or_hole)
                     for ext_or_hole in coords]
            geoj["coordinates"] = [ext_or_hole for ext_or_hole in coords if ext_or_hole]
            if not any(geoj["coordinates"]):
                self.geometry = None
        elif geotype == "MultiPolygon":
            coords = [[wrapfunc(ext_or_hole)
                        for ext_or_hole in poly]
                       for poly in coords]
            coords = [[ext_or_hole
                        for ext_or_hole in poly if ext_or_hole]
                       for poly in coords]
            geoj["coordinates"] = [poly
                                   for poly in coords if poly]
            if not any(geoj["coordinates"]):
                self.geometry = None
            
        self._cached_bbox = None
        
        return True

    # Easy access to shapely methods

    @property
    def length(self):
        return self.get_shapely().length

    @property
    def area(self):
        return self.get_shapely().area

    @property
    def geodetic_length(self):
        from ._helpers import geodetic_length
        return geodetic_length(self.geometry)

    # convenient visualizing

    def dataset(self):
        singledata = VectorData()
        if self._data:
            singledata.fields = self._data.fields
            singledata.add_feature(self.row, self.geometry)
        else:
            singledata.fields = []
            singledata.add_feature([], self.geometry)
        return singledata

    def map(self, width=None, height=None, bbox=None, title="", background=None, **styleoptions):
        """Shortcut for easily creating a Map instance containing this feature as a layer.
        
        Args:
            width/height (optional): Desired width/height of the map. This can be changed again later. 
            bbox (optional): If given, only renders the given bbox, specified as (xmin,ymin,xmax,ymax).
            title (optional): Title to be shown on the map.
            background (optional): Background color of the map.
            **styleoptions (optional): How to style the feature geometry, as documented in "renderer.VectorLayer".
        """
        from .. import renderer
        mapp = renderer.Map(width, height, title=title, background=background)
        
        singledata = self.dataset()
        mapp.add_layer(singledata, **styleoptions)
        
        if bbox:
            mapp.zoom_bbox(*bbox)
        else:
            if singledata.has_geometry():
                mapp.zoom_bbox(*mapp.layers.bbox)
        return mapp

    def view(self, bbox=None, title="", background=None, **styleoptions):
        """Opens a Tkinter window for viewing and interacting with the feature on a map.
        
        Args are same as for "map()".
        """
        from .. import app
        mapp = self.map(None, None, bbox, title=title, background=background, **styleoptions)
        # make gui
        win = app.builder.SimpleMapViewerGUI(mapp)
        win.mainloop()




def ID_generator():
    """Used internally for ensuring default feature IDs are unique for each VectorData instance.
    TODO: Maybe make private. 
    """
    i = 0
    while True:
        yield i
        i += 1


def Name_generator():
    """Used internally for ensuring default data names are unique for each Python session.
    TODO: Maybe make private. 
    """
    i = 1
    while True:
        yield "Untitled%s" % i
        i += 1


NAMEGEN = Name_generator()



class VectorData:
    """
    Class representing a vector dataset. 
    
    Calling len() returns the number of features in the dataset, and iterating over the dataset
    loops through the dataset features one by one. 
    
    Supports getting and setting feature instances via indexing, e.g. feat[13] gets or sets the feature 
    located at the given index position. 
    
    Supports the __geo_interface__ protocol, returning a GeoJSON feature collection object dictionary. 
    
    TODO:
    - Currently, loads all features into memory. Maybe outsource to format-specific classes that iterate over each feature
        without loading into memory (or allow via streaming option or a separate Streaming class). 
    
    Attributes:
        filepath: 
        name: 
        type: 
        fields: 
        features: 
        crs: 
        bbox: 
        
        manage: Access all methods from the manager module, passing self as first arg.
        analyze: Access all methods from the analyzer module, passing self as first arg.
        convert: Access all methods from the converter module, passing self as first arg.
    """
    def __init__(self, filepath=None, type=None, name=None, fields=None, rows=None, geometries=None, features=None, crs=None, **kwargs):
        """A vector dataset can be created in several ways. 
        
        To create an empty dataset, simply initiate the class with no args. A list of field names can be set with the fields arg, 
        or set after creation. 
        
        To load from a file, specify the filepath argument. The "select" option can be used to only populate the data with a subsample of 
        features instead of the entire dataset. When loading non-spatial file formats, the "x/yfield" and "geokey" args can be set to calculate
        the geometry based on the attributes of each row. Optional **kwargs can be used to pass on format-specific loading options. 
        Supported reading fileformats include: 
        - fdsfds...
        
        TODO: 
        - Ensure that select applies to all file formats, not just the non-spatial ones. 
        
        To initiate from a list of existing Feature instances, pass in to the features arg. 
        Alternatively, to load a dataset from separate lists of row values and geometry GeoJSON dictionaries, pass in the rows and geometries args 
        as lists of equal length. 

        Optional metadata can also be specified with args such as name, type, and crs. 
        
        Args:
            fields: List of field names. 
            
            rows: List of row lists to load from, of equal length and sequence as geometries. 
            geometries: List of GeoJSON dictionaries to load from, of equal length and sequence as rows. 
            
            features: List of Feature instances to load from. 
        
            filepath: Filepath of the dataset to load. 
            
            name (optional): Gives the dataset a name, which is used mostly for esthetic reasons and identification in visual lists. 
                TODO: Make this more meaningful, so that various functions can make use of the names to reference specific datasets. 
            type (optional): Geometry type of the dataset. If set, will make the features ensure that all geometries are of the
                specified type. Otherwise, type enforcement will be based on first geometry found. 
            crs (optional): The coordinate system specified as a Proj4 string, defaults to unprojected WGS84. 
                TODO: Currently holds no meaning, makes no difference for any methods or functions. Maybe add on-the-fly reprojection? 
            
            select (optional): Function that takes a fieldname-value dictionary mapping and returns True for features that should be loaded. 
            x/yfield (optional): Specifies the field name containing the x/y coordinates of each feature, used for creating the feature 
                geometries of non-spatial fileformat point data. 
            geokey (optional): Function for creating more advanced types of geometries of non-spatial fileformats. The function takes
                a fieldname-value dictionary mapping and returns a GeoJSON dictionary, or None for null-geometries. 
            
            **kwargs: File-format specific loading options. See `vector.loader` for details.
        """
        self.filepath = filepath
        self.name = name or filepath
        if not self.name:
            self.name = next(NAMEGEN)

        # type is optional and will make the features ensure that all geometries are of that type
        # if None, type enforcement will be based on first geometry found
        self.type = type
        
        if filepath:
            fields,rows,geometries,crs = loader.from_file(filepath, crs=crs, **kwargs)
        else:
            if features:
                rows,geometries = zip(*((feat.row,feat.geometry) for feat in features))
            else:
                rows = rows or []
                geometries = geometries or []
            fields = fields or []
            crs = crs

        self.fields = fields

        self._id_generator = ID_generator()
        
        ids_rows_geoms = zip(self._id_generator,rows,geometries)
        featureobjs = (Feature(self,row,geom,id=id) for id,row,geom in ids_rows_geoms )
        self.features = OrderedDict([ (feat.id,feat) for feat in featureobjs ])

        defaultcrs = "+proj=longlat +ellps=WGS84 +datum=WGS84 +no_defs"
        crs = crs or defaultcrs
        if not isinstance(crs, pycrs.CS):
            try:
                crs = pycrs.parse.from_unknown_text(crs)
            except:
                warnings.warn('Failed to parse the given crs format, falling back to unprojected lat/long WGS84: \n {}'.format(crs))
                crs = pycrs.parse.from_proj4(defaultcrs)
        self.crs = crs

    def __repr__(self):
        attrs = dict(filepath=self.filepath,
                     type=self.type,
                     length=len(self),
                     )
        if any((f.geometry for f in self)):
            attrs["bbox"] = self.bbox
        else:
            attrs["bbox"] = None
        return "<Vector data: type={type} length={length} bbox={bbox} filepath='{filepath}'>".format(**attrs)

    def __len__(self):
        """
        How many features in data.
        """
        return len(self.features)

    def __iter__(self):
        """
        Loop through features in order.
        """
        for feat in self.features.values():
            yield feat

    def __getitem__(self, i):
        """
        Get a Feature based on its feature id.
        """
        if isinstance(i, slice):
            raise Exception("Can only get one feature at a time")
        else:
            return self.features[i]

    def __setitem__(self, i, feature):
        """
        Set a Feature based on its feature id.
        """
        if isinstance(i, slice):
            raise Exception("Can only set one feature at a time")
        else:
            self.features[i] = feature

    def __geo_interface__(self):
        """
        Returns geojson as feature collection, with crs, features, properties, etc. 
        """
        raise NotImplementedError()

    def has_geometry(self):
        """Returns True if at least one feature has non-null geometry."""
        return any((feat.geometry for feat in self))

    @property
    def bbox(self):
        if self.has_geometry():
            xmins, ymins, xmaxs, ymaxs = zip(*(feat.bbox for feat in self if feat.geometry))
            bbox = min(xmins),min(ymins),max(xmaxs),max(ymaxs)
            return bbox
        else:
            raise Exception("Cannot get bbox since there are no features with geometries")

    ### DATA ###

    def sort(self, key, reverse=False):
        """Sorts the feature order in-place using a key function and optional reverse flag."""
        self.features = OrderedDict([ (feat.id,feat) for feat in sorted(self.features.values(), key=key, reverse=reverse) ])
        return self

    def add_feature(self, row=None, geometry=None):
        """Adds and returns a new feature, given a row list or dict, and a geometry GeoJSON dictionary.
        If neither are set, populates row with None values, and empty geometry.
        """
        feature = Feature(self, row, geometry)
        self[feature.id] = feature
        return feature

    def add_field(self, field, index=None):
        """Adds a new field by the name of 'field', optionally at the specified index position.
        All existing feature rows are updated accordingly.
        """
        if index is None:
            self.fields.append(field)
            for feat in self:
                feat.row.append(None)
        else:
            self.fields.insert(index, field)
            for feat in self:
                feat.row.insert(index, None)

    def compute(self, field, value, by=None, stat=None):
        """Loops through all features and sets the row field to the given value.
        If the value is a function, it will take each Feature object as input and uses it to calculate and return a new value.
        If the field name does not already exist, one will be created.
        If by and stat is given, 'by' will be used to group features, and for each group, 'stat' will be used to calculate a 
        statistic and write the results to the members of the group. 
        
        Arguments:
            field: Name of the field to compute. Existing name will overwrite all values, new name will create new field. 
            value: Any value or object to be written to the field, or a callable that expects a Feature as its input and outputs 
                the value to write. 
            by: A field name by which to group, or a callable that expects a Feature as its input and outputs the group-by value.
            stat: The name of a summary statistic to calculate and write for each by-group, or a callable that expects a list of
                Feature instances as input and returns the aggregated value to write. 
                Valid stat values include: 
                - fdsf...
        """
        if field not in self.fields:
            self.add_field(field)

        if hasattr(value, "__call__"):
            valfunc = value
        else:
            valfunc = lambda f: value

        if by:
            from . import sql
            fieldmapping = [(field, valfunc, stat)]
            for feats in sql.groupby(self, key=by):
                feats = list(feats)
                # aggregate stat for each bygroup
                aggval = sql.aggreg(feats, aggregfuncs=fieldmapping)[0]
                # then write to every group member
                for feat in feats:
                    feat[field] = aggval
        else:
            for feat in self:
                feat[field] = valfunc(feat)

    def interpolate(self, step):
        """Interpolates missing values between known values.
        
        NOT YET IMPLEMENTED.
        """
        # maybe one for inserting new rows in between gaps
        # another for just filling missing values in between gaps
        # maybe it does both
        # and finally a separate for interpolating one value based on values in another...
        # ...
        raise NotImplementedError

    def moving_window(self, n, fieldmapping, groupby=None):
        """Loops through the features in the dataset, and calculates one or more new values based
        on aggregate statistics of a moving window of previously visited rows.
        
        Arguments:
            n: Size of the moving window specified as number of rows.
            fieldmapping: Specifies a set of aggregation rules used to calculate the new values based on the moving
                window. Specified as a list of (outfield,valuefield,stat) tuples, where outfield is the field name 
                of a new or existing field to write, valuefield is the field name or function that retrieves the value
                to calculate statistics on, and stat is the name of the statistic to calculate or a function that takes
                the list of values from the moving window as defined by valuefield. 
                Valid stat values include: 
                - fdsf...
            groupby (optional): If specified, the moving window will run separately for each group of features as defined
                by the groupby field name or grouping function. 
        """
        for name,valfunc,statfunc in fieldmapping:
            if not name in self.fields:
                self.add_field(name)
        prevs = []

        from . import sql

        def calc(f, prevs, fieldmapping):
            prevs.append(f)
            if len(prevs) > n:
                prevs.pop(0)
            row = sql.aggreg(prevs, fieldmapping)
            for (name,_,_),val in zip(fieldmapping,row):
                f[name] = val

        if groupby:
            for _,feats in self.group(groupby):
                prevs = []
                for f in feats:
                    calc(f, prevs, fieldmapping)

        else:
            prevs = []
            for f in self:
                calc(f, prevs, fieldmapping)
        
        return self

    def drop_field(self, field):
        """Drops the specified field, changing the dataset in-place."""
        fieldindex = self.fields.index(field)
        del self.fields[fieldindex]
        for feat in self:
            del feat.row[fieldindex]

    def drop_fields(self, fields):
        """Drops all of the specified fields, changing the dataset in-place."""
        for f in fields:
            self.drop_field(f)

    def keep_fields(self, fields):
        """Keeps only the fields specified, changing the dataset in-place."""
        for kf in fields:
            if kf not in self.fields:
                raise Exception("%s is not a field" % kf)
        for f in reversed(self.fields):
            if f not in fields:
                self.drop_field(f)

    def rename_field(self, oldname, newname):
        """Changes the name of a field from oldname to newname."""
        self.fields[self.fields.index(oldname)] = newname

    def convert_field(self, field, valfunc):
        """Applies the given valfunc function to force convert all values in a field."""
        fieldindex = self.fields.index(field)
        for feat in self:
            val = feat.row[fieldindex]
            feat.row[fieldindex] = valfunc(val)

    # INSPECTING

    def describe(self):
        """Prints a description of the dataset, such as geometry type, length, bbox, and lists each
        field along with their name, type, valid, and missing.
        """

        printfields = ["", "type", "valid", "missing"]
        printrows = []

        from . import sql
        
        for field in self.fields:
            
            values = [feat[field] for feat in self]
            typ = self.field_type(field)
            getval = lambda v: v
            
            def getmissing(v):
                """Sets valid values to none so that only missing values are counted in stats"""
                v = is_missing(getval(v))
                v = True if v else None 
                return v

            missing = sum((1 for v in values if getmissing(v)))
            valid = len(self) - missing
            missing = "%s (%.2f%%)" % (missing, missing/float(len(self))*100 )
            printrow = [field, typ, valid, missing]
                
            printrows.append(printrow)

        # format outputstring
        outstring = "Describing vector dataset:" + "\n"
        outstring += "filepath: %s \n" % self.filepath
        outstring += "type: %s \n" % self.type
        outstring += "length: %s \n" % len(self) 
        outstring += "bbox: %s \n" % repr(self.bbox) if self.has_geometry() else None       
        outstring += "fields:" + "\n"
        
        row_format = "{:>15}" * (len(printfields))
        outstring += row_format.format(*printfields) + "\n"
        for row in printrows:
            outstring += row_format.format(*row) + "\n"
            
        print(outstring)

    def summarystats(self, *fields):
        """
        Prints summary statistics for all fields. 
        If specified, only calculates for the fields listed in *fields.

        TODO: maybe also return a dict?
        """

        fields = fields or self.fields
        
        def getmissing(v):
            """Sets valid values to none so that only missing values are counted in stats"""
            v = is_missing(getval(v))
            v = True if v else None 
            return v

        from . import sql

        printfields = ["", "type", "obs", "min", "max", "mean", "stdev"]
        printrows = []
        getval = lambda v: v
        fieldmapping = [("min",getval,"min"),
                        ("max",getval,"max"),
                        ("mean",getval,"mean"),
                        #("stdev",getval,"stddev"),
                        ("missing",getmissing,"count"),]

        for field in fields:
            typ = self.field_type(field)
            if typ in ("text",):
                printrow = [field, typ] + [None for _ in fieldmapping] + [None]
                
            elif typ in ("int","float"):
                values = [feat[field] for feat in self]
                _min,_max,mean,missing = sql.aggreg(values, aggregfuncs=fieldmapping)
                missing = missing if missing else 0
                valid = len(self) - missing
                printrow = [field, typ, valid, _min, _max, mean, None]

            printrows.append(printrow)

        # format outputstring
        outstring = "Summary statistics: \n"

        row_format = "{:>15}" * (len(printfields))
        outstring += row_format.format(*printfields) + "\n"
        for row in printrows:
            outstring += row_format.format(*row) + "\n"

        print(outstring)

    def field_values(self, field):
        """Returns sorted list of all the unique values in this field."""
        return sorted(set(f[field] for f in self))

    def field_type(self, field):
        """Determines and returns field type of field based on its values (ignoring missing values).
        For now, only detects int, float, and text.
        
        TODO: also detect other types eg datetime, etc.
        """
        values = (f[field] for f in self)
        values = (v for v in values if not is_missing(v))
        # approach: at first assume int, if fails then assume float,
        # ...if fails then assume text and stop checking (lowest possible dtype)
        typ = "int"
        for v in values:
            try:
                v = float(v)
                if v.is_integer():
                    pass
                else:
                    typ = "float"
            except:
                typ = "text"
                break
        return typ

    def tab(self, field):
        """Prints a frequency count of the unique values for a single field.

        TODO: split into multiple methods, eg field_stats vs field_values/frequencies
        TODO: standardize table string formatting, eg as a vectordata.stringformat() method, and simply populate a stats vectordata table and call its method
        TODO: sort freq table by percentages
        TODO: fix unicode print error
        TODO: maybe also return a dict?
        """
        
        typ = self.field_type(field)
        getval = lambda v: v
        values = [f[field] for f in self]

        def getmissing(v):
            """Sets valid values to none so that only missing values are counted in stats"""
            v = is_missing(getval(v))
            v = True if v else None 
            return v

        from . import sql
        
        printfields = ["", "frequency", "percent"]
        printrows = []

        for uniq in sorted(set(values)):
            freq = values.count(uniq)
            perc = freq / float(len(self)) * 100
            perc = "%.2f%%" % perc
            printrow = [uniq, freq, perc]
            printrows.append(printrow)

        # format outputstring
        outstring = "Frequency tabulation for field: '%s' \n" % field
        outstring += "type: %s \n" % typ
        outstring += "values: " + "\n"

        row_format = "{:>15}" * (len(printfields))
        outstring += row_format.format(*printfields) + "\n"
        for row in printrows:
            outstring += row_format.format(*row) + "\n"

        print(outstring)

    def histogram(self, field, width=None, height=None, bins=10):
        """Renders the value distribution of a given field in a histogram plot, 
        returned as a PyAgg Canvas of size width/height. This canvas can be used
        to call "save()" or "view()". Default histogram bins is 10.
        
        TODO:
        - Should it return Canvas, or just straight view() it? 
        """
        import pyagg
        from . import classypie
        values = [f[field] for f in self]

        bars = []
        for (_min,_max),group in classypie.split(values, breaks="equal", classes=bins):
            label = "%s to %s" % (_min,_max)
            count = len(group)
            bars.append((label,count))
            
        c = pyagg.graph.BarChart()
        c.add_category(name="Title...", baritems=bars)
        return c.draw() # draw returns the canvas

    ### FILTERING ###

    def get(self, func):
        """Iterates over features that meet filter conditions.
        Func takes a Feature instance as input and yield only those where it returns True.
        """
        new = VectorData()
        new.fields = [field for field in self.fields]
        
        for feat in self:
            if func(feat):
                yield feat

    def group(self, key):
        """Iterates over keyvalue-group pairs based on key"""
        for uid,feats in itertools.groupby(sorted(self, key=key), key=key):
            yield uid, list(feats)

    ### OTHER ###

    def select(self, func):
        """Returns new filtered VectorData instance. 
        Func takes a Feature instance as input and keeps only those where it returns True.
        """
        new = VectorData()
        new.fields = [field for field in self.fields]
        
        for feat in self:
            if func(feat):
                new.add_feature(feat.row, feat.geometry)

        return new

    def aggregate(self, key, geomfunc=None, fieldmapping=[]):
        """Aggregate values and geometries within key groupings.
        
        Arguments:
            key: List of field names or a function to group by. 
            geomfunc (optional): Specifies how to aggregate geometries, either intersection, union, difference,
                or a function that takes all geometries to aggregate. If not set (default), does not aggregate geometries,
                returning a non-spatial table with only null-geometries. 
            fieldmapping: Specifies a set of aggregation rules used to calculate the new value for each group. 
                Specified as a list of (outfield,valuefield,stat) tuples, where outfield is the field name 
                of a new or existing field to write, valuefield is the field name or function that retrieves the value
                to calculate statistics on, and stat is the name of the statistic to calculate or a function that takes
                the list of values from the group as defined by valuefield. 
                Valid stat values include: 
                - fdsf...
        """
        # TODO: Move to manager...?
        out = VectorData()

        if isinstance(key, list):
            keyfields = key
            key = lambda f: [f[field] for field in keyfields]
            # add keyfields to fieldmapping
            fieldmapping = [(field,field,"first") for field in keyfields] + fieldmapping
        
        out.fields = [fieldname for fieldname,_,_ in fieldmapping]

        if not geomfunc:
            geomfunc = lambda fs: None

        from . import sql
        
        for feats in sql.groupby(self, key=key):
            row,geom = sql.aggreg(feats, aggregfuncs=fieldmapping, geomfunc=geomfunc)
            out.add_feature(row=row, geometry=geom)

        return out

    def duplicates(self, subkey=None, fieldmapping=[]):
        """Removes duplicate geometries by grouping and aggregating their values.
        
        Arguments: 
            subkey (optional): If specified, for each set of duplicate geometries will perform separate aggregations 
                for each subgroup defined by subkey. Geometry duplicates will continue to exist if they have more than 
                one subkey grouping. 
            fieldmapping: Defines the value aggregations. See aggregate(). 
        """
        # TODO: Move to manager...?
        if subkey:
            # additional subgrouping based on eg attributes
            if isinstance(subkey, list):
                keyfields = subkey
                subkey = lambda f: [f[field] for field in keyfields]
                # add keyfields to fieldmapping
                fieldmapping = [(field,field,"first") for field in keyfields] + fieldmapping
            keywrap = lambda f: (f.geometry, subkey(f))
        else:
            # only by geometry
            keywrap = lambda f: f.geometry
            
        geomfunc = lambda items: items[0].geometry # since geometries are same within each group, pick first one
        out = self.aggregate(keywrap, geomfunc=geomfunc, fieldmapping=fieldmapping)
        
        return out

    def join(self, other, key, fieldmapping=[], collapse=False, keepall=True):
        """Matches and joins the features in this dataset with the features in another dataset.
        Returns a new joined dataset.

        Note: if the other dataset has fields with the same name as the main dataset, those will not be joined, keeping
            only the ones in the main dataset. 
        
        Arguments:
            other: The other VectorData dataset to join to this one.
            key: Can be a single fieldname, multiple fieldnames, or function that returns the link for both tables.
                
                TODO: 
                - Maybe introduce separate lkey and rkey args... 
                
            collapse (optional): If True, collapses and aggregates all matching features in the other dataset (default), otherwise
                adds a new row for each matching pair. 
            fieldmapping (optional): If collapse is True, this determines the aggregation rules. See aggregate(). 
            keepall (optional): If True, keeps all features in the main dataset regardless (default), otherwise only keeps the 
                ones that match.
        """
        # TODO: enable multiple join conditions in descending priority, ie key can be a list of keys, so looks for a match using the first key, then the second, etc, until a match is found.
        # TODO: Move to manager...?
        out = VectorData()
        out.fields = list(self.fields)
        out.fields += (field for field in other.fields if field not in self.fields)

        otheridx = [i for i,field in enumerate(other.fields) if field not in self.fields]

        from . import sql

        if isinstance(key, (list,tuple)):
            keyfunc = lambda f: tuple([f[k] for k in key])
        elif hasattr(key,"__call__"):
            keyfunc = key
        else:
            keyfunc = lambda f: f[key]
            
        key1 = key2 = keyfunc

        if collapse:
            out.fields += (fieldtup[0] for fieldtup in fieldmapping if fieldtup[0] not in out.fields)
            fieldmapping_default = [(field,lambda f,field=field:f[field],"first") for field in other.fields if field not in self.fields]
            fs,vfs,afs = zip(*fieldmapping) or [[],[],[]]
            
            def getfm(item):
                if item[0] in fs:
                    return fieldmapping[fs.index(item[0])]
                else:
                    return item
            fieldmapping_old = fieldmapping
            fieldmapping = [getfm(item) for item in fieldmapping_default]
            fieldmapping += (item for item in fieldmapping_old if item[0] not in self.fields and item[0] not in other.fields)
            print(fieldmapping)

            def grouppairs(data1, key1, data2, key2):
                "aggregates"
                # create hash table
                # inspired by http://rosettacode.org/wiki/Hash_join#Python
                hsh = dict()
                for keyval,f2s in itertools.groupby(sorted(data2,key=key2), key=key2):
                    aggval = sql.aggreg(f2s, aggregfuncs=fieldmapping)
                    hsh[keyval] = aggval
                # iterate join
                for f1 in data1:
                    keyval = key1(f1)
                    if keyval in hsh:
                        f2row = hsh[keyval]
                        yield f1,f2row
                    elif keepall:
                        f2row = (None for f in fieldmapping)
                        yield f1,f2row

        else:
            def grouppairs(data1, key1, data2, key2):
                "pairwise"
                # create hash table
                # inspired by http://rosettacode.org/wiki/Hash_join#Python
                hsh = dict()
                for keyval,f2s in itertools.groupby(sorted(data2,key=key2), key=key2):
                    hsh[keyval] = list(f2s)
                # iterate join
                for f1 in data1:
                    keyval = key1(f1)
                    if keyval in hsh:
                        f2s = hsh[keyval]
                        for f2row in f2s:
                            yield f1,[f2row[i] for i in otheridx]
                    elif keepall:
                        f2row = [None for i in otheridx]
                        yield f1,f2row

        for pair in grouppairs(self, key1, other, key2):
            f1,f2row = pair
            row = list(f1.row)
            row += f2row
            out.add_feature(row=row, geometry=f1.geometry)

        return out
    

##        from . import sql
##
##        groupbydata = self
##        valuedata = other
##        def _condition(item):
##            f1,f2 = item
##            return condition(f1,f2)
##
##        if not fieldmapping:
##            fieldmapping = [ (field, lambda (f1,f2),field=field: f2[field], "first")
##                            for field in other.fields
##                             if field not in self.fields]
##
##        # add fields
##        out.fields = list(groupbydata.fields)
##        out.fields.extend([name for name,valfunc,aggfunc in fieldmapping])
##
##        # loop
##        for groupfeat in groupbydata:
##            newrow = list(groupfeat.row)
##            geoj = groupfeat.geometry
##
##            # aggregate
##            combinations = ((groupfeat,valfeat) for valfeat in valuedata)
##            matches = sql.where(combinations, _condition)
##            newrow.extend( sql.aggreg(matches, fieldmapping) )
##
##            # add
##            out.add_feature(newrow, geoj)
##
##        return out        
            
##        outvec.fields = self.fields + other.fields
##        
##        for feat1 in self:
##            anymatch = False
##            
##            for feat2 in other:
##                match = condition(feat1,feat2)
##                
##                if match:
##                    anymatch = True
##                    row = feat1.row + feat2.row
##                    outvec.add_feature(row=row, geometry=feat1.geometry)
##
##            if not anymatch and keepall:
##                row = feat1.row + ["" for _ in range(len(feat2.row))]
##                outvec.add_feature(row=row, geometry=feat1.geometry)
##
##        return outvec
                

    ### ACCESS TO ADVANCED METHODS FROM INTERNAL MODULES ###

    @property
    def manage(self):
        from . import manager
        return _ModuleFuncsAsClassMethods(self, manager)

    @property
    def analyze(self):
        from . import analyzer
        return _ModuleFuncsAsClassMethods(self, analyzer)

    @property
    def convert(self):
        from . import converter
        return _ModuleFuncsAsClassMethods(self, converter)


    ###### SPATIAL INDEXING #######

    def create_spatial_index(self, type=None, backend=None, **kwargs):
        """Creates spatial index to allow quick overlap search methods.
        If features are changed, added, or dropped, the index must be created again.
        """
        # if no preference, try the default
        if type is None:
            type = DEFAULT_SPATIAL_INDEX

        # try the preferred one
        try:
            if type == 'rtree':
                self.spindex = spindex.Rtree(backend=backend, **kwargs)
            elif type == 'quadtree':
                self.spindex = spindex.QuadTree(backend=backend, bbox=self.bbox, **kwargs)
            else:
                raise Exception('No such spatial index type: {}'.format(type))
        
        except ImportError:
            # if didn't work, try all and use first one that works
            for type in ['rtree','quadtree']:
                try:
                    if type == 'rtree':
                        self.spindex = spindex.Rtree(backend=backend, **kwargs)
                    elif type == 'quadtree':
                        self.spindex = spindex.QuadTree(backend=backend, bbox=self.bbox, **kwargs)
                    else:
                        raise Exception('No such spatial index type: {}'.format(type))
                except ImportError:
                    pass
            
        # populate the spindex
        if hasattr(self, 'spindex'):
            for feat in self:
                if feat.geometry:
                    self.spindex.insert(feat.id, feat.bbox)
   
    def quick_overlap(self, bbox):
        """
        Quickly get features whose bbox overlap the specified bbox via the spatial index.
        """
        if not hasattr(self, "spindex"):
            raise Exception("You need to create the spatial index before you can use this method")
        # ensure min,min,max,max pattern
        xs = bbox[0],bbox[2]
        ys = bbox[1],bbox[3]
        bbox = [min(xs),min(ys),max(xs),max(ys)]
        # return generator over results
        overlaps = self.spindex.intersects(bbox)
        return (self[id] for id in overlaps)

    def quick_disjoint(self, bbox):
        """
        Quickly get features whose bbox do -not- overlap the specified bbox via the spatial index.
        """
        if not hasattr(self, "spindex"):
            raise Exception("You need to create the spatial index before you can use this method")
        # ensure min,min,max,max pattern
        xs = bbox[0],bbox[2]
        ys = bbox[1],bbox[3]
        bbox = [min(xs),min(ys),max(xs),max(ys)]
        # return generator over results
        overlaps = set((id for id in self.spindex.intersects(bbox)))
        allids = set(self.features.keys())
        disjoint = allids.difference(overlaps)
        return (self[id] for id in disjoint)

    def quick_nearest(self, bbox, n=None, radius=None):
        """
        Quickly get n features whose bbox are nearest the specified bbox via the spatial index.

        TODO: radius option not yet implemented. 
        """
        # TODO: special handling if points data, might be faster to just test all.
        # ...
        
        if not hasattr(self, "spindex"):
            raise Exception("You need to create the spatial index before you can use this method")

        if radius != None:
            raise NotImplementedError("Spatial index nearest with radius option not yet implemented")
        
        # ensure min,min,max,max pattern
        xs = bbox[0],bbox[2]
        ys = bbox[1],bbox[3]
        bbox = [min(xs),min(ys),max(xs),max(ys)]
        # return generator over results
        if not n:
            n = len(self)

        for id in self.spindex.nearest(bbox, num_results=n): # radius not yet respected
            feat = self[id]
            yield feat
           
##        if radius:
##            window = shapely.geometry.box(*bbox)
##            for id in self.spindex.nearest(bbox, num_results=n):
##                feat = self[id]
##                # bboxes are sorted by distance, so only yield while bbox is inside of radius
##                if window.distance(shapely.geometry.box(*feat.bbox)) <= radius:
##                    yield feat
##                else:
##                    break
##        else:
##            # TODO: if wanting to find x nearest regardless of distance, then we must sort by max bbox distance,
##            # then group by continuous intersectioning ones, and return from each group until n reached
##            box = shapely.geometry.box(*bbox)
##            def maxdist(otherfeat):
##                "get highest distance to any of the corners of bbox"
##                return max((box.distance(shapely.geometry.Point(*p)) for p in shapely.geometry.box(*otherfeat.bbox).coordinates))
##            feats = (self[id] for id in self.spindex.nearest(bbox, num_results=n))
##            sortfeats = (f for f in sorted(feats, key=maxdist))
##            i = 0
##            def grouped():
##                groupid = 0
##                prevfeat = None
##                for feat in sortfeats:
##                    if fsd:
##                        fdsf
##            for key,subfeats in sorted(sortfeats):
##                yield self[id]
        
    ###### GENERAL #######

    def save(self, savepath, **kwargs):
        fields = self.fields
        rowgeoms = ((feat.row,feat.geometry) for feat in self)
        rows, geometries = zip(*rowgeoms)
        saver.to_file(fields, rows, geometries, savepath, **kwargs)

    def copy(self):
        new = VectorData()
        new.type = self.type
        new.fields = [field for field in self.fields]
        featureobjs = (Feature(new, feat.row, feat.geometry) for feat in self )
        new.features = OrderedDict([ (feat.id,feat) for feat in featureobjs ])
        #if hasattr(self, "spindex"): new.spindex = self.spindex.copy() # NO SUCH METHOD
        new.crs = pycrs.parse.from_proj4(self.crs.to_proj4()) # separate copy
        return new
    
    def map(self, width=None, height=None, bbox=None, title="", background=None, crs=None, **styleoptions):
        """Shortcut for easily creating a Map instance containing this dataset as a layer.
        
        Args:
            width/height (optional): Desired width/height of the map. This can be changed again later. 
            bbox (optional): If given, only renders the given bbox, specified as (xmin,ymin,xmax,ymax).
            title (optional): Title to be shown on the map.
            background (optional): Background color of the map.
            **styleoptions (optional): How to style the feature geometry, as documented in "renderer.VectorLayer".
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

    def browse(self, limit=None):
        """Opens a Tkinter window for viewing and interacting with the rows contained in this dataset.
        
        Args:
            limit (optional): Limits the number of rows to be displayed, in case of very large datasets. 
        """
        from .. import app
        win = app.builder.DatasetTableGUI(self, limit=limit)
        win.mainloop()

    def view(self, width=None, height=None, bbox=None, title="", background=None, crs=None, **styleoptions):
        """Opens a Tkinter window for viewing and interacting with the dataset on a map.
        
        Args are same as for "map()".
        """
        from .. import app
        mapp = self.map(width, height, bbox, title=title, background=background, crs=crs, **styleoptions)
        # make gui
        mapp.view()
        #win = app.builder.SimpleMapViewerGUI(mapp)
        #win.mainloop()




    
