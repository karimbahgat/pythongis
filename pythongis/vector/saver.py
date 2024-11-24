

# import builtins
import itertools
import math

# import fileformats
import shapefile as pyshp
import pygeoj

# PY3 fix
try: 
    str = unicode # in py2, make str synonymous with str
    zip = itertools.izip
except:
    pass


NaN = float("nan")

def is_missing(value):
    return value in (None,"") or (isinstance(value, float) and math.isnan(value))

def to_file(fields, rows, geometries, filepath, encoding="utf8", maxprecision=12, **kwargs):

    def encode(value):
        if isinstance(value, int):
            # ints are kept as ints
            return value
        elif isinstance(value, float):
            if value.is_integer():
                return int(value)
            elif math.isnan(value):
                return None
            else:
                # floats are rounded
                return round(value, maxprecision)
        elif isinstance(value, str):
            # unicode is custom encoded into bytestring
            return value.encode(encoding)
        elif value is None:
            return value
        else:
            # brute force anything else to string representation
            return bytes(value)

    def detect_fieldtypes(fields, rows):
        # TODO: allow other data types such as dates etc...
        # set fields with correct fieldtype
        fieldtypes = []
        for fieldindex,fieldname in enumerate(fields):
            fieldlen = 1
            decimals = 0
            fieldtype = "N" # assume number until proven otherwise
            for row in rows:
                value = row[fieldindex]
                
                if is_missing(value):
                    # empty value, so just keep assuming same type
                    pass
                
                else:
                    try:
                        # make nr fieldtype if content can be made into nr

                        # convert to nr or throw exception if text
                        value = float(value)

                        # rare special case where text is 'nan', is a valid input to float, so raise exception to treat as text
                        if math.isnan(value):
                            raise ValueError()

                        # TODO: also how to handle inf? math.isinf(). Treat as text or nr? 
                        if math.isinf(value):
                            raise NotImplementedError("Saving infinity values not yet implemented")

                        # detect nr type
                        if value.is_integer():
                            _strnr = str(value)
                        else:
                            # get max decimals, capped to max precision
                            _strnr = format(value, ".%sf"%maxprecision).rstrip("0")
                            decimals = max(( len(_strnr.split(".")[1]), decimals ))
                        fieldlen = max(( len(_strnr), fieldlen ))
                    except ValueError:
                        # but turn to text if any of the cells cannot be made to float bc they are txt
                        fieldtype = "C"
                        value = value if isinstance(value, str) else str(value)
                        fieldlen = max(( len(value), fieldlen ))
                        
            if fieldtype == "N" and decimals == 0:
                fieldlen = fieldlen - 2 if fieldlen > 2 else fieldlen # bc above we measure lengths for ints as if they were floats, ie with an additional ".0"
                func = lambda v: "" if is_missing(v) else int(float(v))
            elif fieldtype == "N" and decimals:
                func = lambda v: "" if is_missing(v) else float(v)
            elif fieldtype == "C":
                func = lambda v: v #encoding are handled later
            else:
                raise Exception("Unexpected bug: Detected field should be always N or C")
            fieldtypes.append( (fieldtype,func,fieldlen,decimals) )
        return fieldtypes
    
    # shapefile
    if filepath.endswith(".shp"):
        shapewriter = pyshp.Writer(filepath, encoding='utf8')

        fieldtypes = detect_fieldtypes(fields,rows)
        
        # set fields with correct fieldtype
        for fieldname,(fieldtype,func,fieldlen,decimals) in zip(fields, fieldtypes):
            fieldname = fieldname.replace(" ","_")[:10]
            shapewriter.field(fieldname, fieldtype, fieldlen, decimals)
        
        # iterate through original shapes
        for row,geoj in zip(rows, geometries):
            shapewriter.shape(geoj)
            row = [func(value) for (typ,func,length,deci),value in zip(fieldtypes,row)]
            shapewriter.record(*row)
            
        # save
        shapewriter.close()

    # geojson file
    elif filepath.endswith((".geojson",".json")):
        geojwriter = pygeoj.new()
        fieldtypes = detect_fieldtypes(fields,rows)
        for row,geom in zip(rows,geometries):
            # encode row values
            row = (func(value) for (typ,func,length,deci),value in zip(fieldtypes,row))
            row = (encode(value) for value in row)
            rowdict = dict(zip(fields, row))
            # create and add feature
            geojwriter.add_feature(properties=rowdict,
                                   geometry=geom)
        # save
        geojwriter.save(filepath, encoding=encoding)

    # normal table file without geometry
    elif filepath.endswith((".txt",".csv")):
        import csv
        
        # TODO: Add option of saving geoms as strings in separate fields
        with open(filepath, "wb") as fileobj:
            csvopts = dict()
            csvopts["delimiter"] = kwargs.get("delimiter", ";") # tab is best for automatically opening in excel...
            writer = csv.writer(fileobj, **csvopts)
            writer.writerow([f.encode(encoding) for f in fields])
            for row,geometry in zip(rows, geometries):
                writer.writerow([encode(val) for val in row])

    elif filepath.endswith(".xls"):
        import xlwt
        
        with open(filepath, "wb") as fileobj:
            wb = xlwt.Workbook(encoding=encoding) # module takes care of encoding for us
            sheet = wb.add_sheet("Data")
            # fields
            for c,f in enumerate(fields):
                sheet.write(0, c, f)
            # rows
            for r,(row,geometry) in enumerate(zip(rows, geometries)):
                for c,val in enumerate(row):
                    # TODO: run val through encode() func, must spit out dates as well
                    sheet.write(r+1, c, val)
            # save
            wb.save(filepath)
            
    else:
        raise Exception("Could not save the vector data to the given filepath: the filetype extension is either missing or not supported")






