
import itertools, operator, math
import gc   # garbage collector
from .data import *

import shapely, shapely.ops, shapely.geometry
from shapely.prepared import prep as supershapely






# Overlay Analysis (transfer of values, but no clip)

def spatial_stats(groupbydata, valuedata, fieldmapping=[], keepall=True, subkey=None, key=None, **kwargs):
    """
    Summarizes the values of "valuedata" that overlap "groupbydata",
    and adds the summary statistics to the output data.

    "groupbydata" must be vector instance, but "valuedata" can be either either a vector or raster instance. 

    "fieldmapping" is a list of ('outfieldname', 'getvaluefunction', 'statistic name or function') tuples that decides which
    variables to summarize and how to do so. Valid statistics are count,
    sum, max, min, and average.

    Key is a function for determining if a pair of features should be processed, taking feat and clipfeat as input args and returning True or False
    """

    from . import sql

    out = VectorData()

    # add fields
    out.fields = list(groupbydata.fields)
    out.fields.extend([name for name,valfunc,aggfunc in fieldmapping if name not in out.fields])

    # loop
    if not hasattr(groupbydata, "spindex"): groupbydata.create_spatial_index()
    if keepall:
        groupfeats = groupbydata
    else:
        if hasattr(valuedata, 'bbox'):
            groupfeats = groupbydata.quick_overlap(valuedata.bbox) # take advantage of spindex if not keeping all
        else:
            groupfeats = groupbydata

    if isinstance(valuedata, VectorData):
        # vector in vector
        
        if not hasattr(valuedata, "spindex"): valuedata.create_spatial_index()
        for groupfeat in groupfeats: 

            if not groupfeat.geometry:
                if keepall:
                    newrow = list(groupfeat.row)
                    newrow.extend( (None for _ in fieldmapping) )
                    out.add_feature(newrow, None)

                continue
            
            geom = groupfeat.get_shapely()
            supergeom = supershapely(geom)
            #print groupfeat
            valuefeats = ((valfeat,valfeat.get_shapely()) for valfeat in valuedata.quick_overlap(groupfeat.bbox))

            # aggregate
            if groupbydata.type == valuedata.type == "Polygon":
                # when comparing polys to polys, dont count neighbouring polygons that just touch on the edge
                def overlaps(valgeom):
                    if supergeom.intersects(valgeom) and not geom.touches(valgeom):
                        return True
            else:
                # for lines and points, ok that just touches on the edge
                def overlaps(valgeom):
                    return supergeom.intersects(valgeom)
                
            if key:
                matches = (valfeat for valfeat,valgeom in valuefeats
                           if key(groupfeat,valfeat) and overlaps(valgeom))
            else:
                matches = ((valfeat,valgeom) for valfeat,valgeom in valuefeats
                           if overlaps(valgeom))

            # clean potential junk, maybe allow user setting of minimum area (put on hold for now, maybe user should make sure of this in advance?)
            def cleaned():
                for valfeat,valgeom in matches:
                    yield valfeat
            matches = list(cleaned())

            if subkey:
                if matches:
                    for group in sql.groupby(matches, subkey):
                        aggreg = sql.aggreg(group, fieldmapping)

                        newrow = list(groupfeat.row)
                        newrow.extend( aggreg )
                        out.add_feature(newrow, geom.__geo_interface__)

                elif keepall:
                    newrow = list(groupfeat.row)
                    newrow.extend( (None for _ in fieldmapping) )
                    out.add_feature(newrow, geom.__geo_interface__)

            else:
                if matches:
                    aggreg = sql.aggreg(matches, fieldmapping)

                # add
                if matches:
                    newrow = list(groupfeat.row)
                    newrow.extend( aggreg )
                    out.add_feature(newrow, geom.__geo_interface__)

                elif keepall:
                    newrow = list(groupfeat.row)
                    newrow.extend( (None for _ in fieldmapping) )
                    out.add_feature(newrow, geom.__geo_interface__)

    else:
        # raster in vector
        # TODO: For very large files, something in here produces a crash after returning output even though memory use seems low...
        from .. import raster
        
        for f in groupfeats:
            #print('spatial stats: processing feat', f.id)
            row = f.row + [None for _ in fieldmapping]
            outfeat = out.add_feature(row, f.geometry)

            # check raster input for valuedata
            if isinstance(valuedata, raster.data.RasterData):
                # user provided RasterData
                rast = valuedata
            elif hasattr(valuedata, '__call__'):
                # RasterData is dynamically calculated by provided function
                rast = valuedata(f)
                if rast is None:
                    # means no raster relevant for this feature
                    continue
                elif not isinstance(rast, raster.data.RasterData):
                    # valuedata func returned incorrect value type
                    raise TypeError(f'When `valuedata` arg is a function it must return a RasterData type or None, \
                                    not {type(rast)}')
            else:
                raise TypeError(f'Valuedata must be either RasterData or a function returning a RasterData, \
                                not {type(valuedata)}')

            # crop
            try:
                cropped = raster.manager.crop(rast, f.bbox)
            except:
                # means no raster relevant for this feature
                continue
            del rast
            #gc.collect()
            
            # create VectorData from feature
            # TODO: only check overlapping tiles
            # TODO: how to calc stat on multiple overlapping tiles
            fdata = VectorData()
            fdata.add_feature([], f.geometry)
            
            # clip RasterData to VectorData
            clipped = raster.manager.clip(cropped, fdata)

            #import pythongis as pg
            #mapp = pg.renderer.Map()
            #mapp.add_layer(clipped)
            #mapp.add_layer(fdata, fillcolor=None)
            #mapp.add_legend()
            #mapp.view()

            del fdata
            del cropped
            #gc.collect()
            
            # compute each stat field on feature by calling summarystat on clipped raster
            for statfield,bandnum,outstat in fieldmapping:
                stat = clipped.bands[bandnum].summarystats(outstat)[outstat]
                outfeat[statfield] = stat

            del clipped
            #gc.collect()
        
        #gc.collect()

    return out







# Distance Analysis

##def near_summary(groupbydata, valuedata,
##                 radius=None,   # only those within radius dist
##                 fieldmapping=[], 
##                 n=None,   # only include n nearest
##                 keepall=True, 
##                 **kwargs):
##    """
##    Summarizes the values of "valuedata" that are nearest "groupbydata",
##    and adds the summary statistics to the output data.
##
##    "fieldmapping" is a list of ('outfieldname', 'getvaluefunction', 'statistic name or function') tuples that decides which
##    variables to summarize and how to do so. Valid statistics are count,
##    sum, max, min, and average. 
##    """
##
##    if not radius and not n:
##        raise Exception("Either radius or n (or both) must be set")
##    
##    # define summary conditions
##    # TODO: filters need to optimize using spindex nearest
##    # NOTE: also watch out, quick_nearest with limit can lead to wrong results, since not all of those will be within exact distance
##    # TODO: instead, always consider all nearest, but instead make efficient algo for stopping
##    #       or perhaps multiple expanding quick_nearest...
##    # See eg http://www.cs.umd.edu/~hjs/pubs/incnear2.pdf
##    # ALSO:
##    # speedup by only doing distance calculations once for each unique pair
##    # and then looking up to avoid repeat distance calcs
##
##    from . import sql
##
##    if not hasattr(valuedata, "spindex"):
##        valuedata.create_spatial_index()
##
##    out = VectorData()
##
##    # add fields
##    out.fields = list(groupbydata.fields)
##    out.fields.extend([name for name,valfunc,aggfunc in fieldmapping])
##
##    # loop
##    for groupfeat in groupbydata:
##        print(groupfeat)
##
##        if not groupfeat.geometry:
##            if keepall:
##                newrow = list(groupfeat.row)
##                newrow.extend( ("" for _ in fieldmapping) )
##                out.add_feature(newrow, None)
##
##            continue
##        
##        newrow = list(groupfeat.row)
##        geom = groupfeat.get_shapely()
##
##        # should first test for overlap which is much faster
##        matches = [valfeat for valfeat in valuedata.quick_overlap(groupfeat.bbox)]
##
##        if not matches:
##            # if not, then test for distance...
##            
##            # precalc all distances (so that iterable is a feat-dist tuple) 
##            matches = ((valfeat, geom.distance(valfeat.get_shapely())) for valfeat in valuedata.quick_nearest(groupfeat.bbox, n=n))
##
##            # filter to only those within radius
##            if radius: 
##                matches = sql.where(matches, lambda((f,d)): d <= radius)
##
##            # filter to only n nearest
##            if n:
##                matches = sorted(matches, key=lambda((f,d)): d)
##                matches = sql.limit(matches, n)
##
##            # remove distance from iterable so only feats remain for aggregating
##            matches = [f for f,d in matches]
##
##        # aggregate
##        if matches:
##            newrow.extend( sql.aggreg(matches, fieldmapping) )
##            out.add_feature(newrow, geom.__geo_interface__)
##
##        elif keepall:
##            newrow = list(groupfeat.row)
##            newrow.extend( ("" for _ in fieldmapping) )
##            out.add_feature(newrow, None)
##
####    # insert groupby data fields into fieldmapping
####    basefm = [(name,lambda f:f[name],"first") for name in groupbydata.fields]
####    fieldmapping = basefm + fieldmapping
####    out.fields = [name for name,valfunc,aggfunc in fieldmapping]
####
####    # group by each groupby feature
####    iterable = ([(feat,feat.get_shapely()),(otherfeat,otherfeat.get_shapely())]
####                for feat in groupbydata for otherfeat in valuedata)
####    for group in sql.groupby(iterable, lambda([(f,g),(of,og)]): id(f)):
####
####        # precalc all distances
####        group = ([(f,g),(of,og),g.distance(og)] for (f,g),(of,og) in group)
####
####        # sort by nearest dist dirst
####        group = sorted(group, key=lambda([(f,g),(of,og),d]): d)
####
####        # filter to only those within radius
####        if radius: 
####            group = sql.where(group, lambda([(f,g),(of,og),d]): d <= radius)
####
####        # filter to only n nearest
####        if n:
####            group = sql.limit(group, n)
####
####        # make iter as usually expected by fieldmapping
####        group = ((of,og) for [(f,g),(of,og),d] in group)
####
####        # aggregate and add
####        # (not sure if will be correct, in terms of args expected by fieldmapping...?)
####        row,geom = sql.aggreg(group, fieldmapping, lambda(itr): next(itr)[1])
####        out.add_feature(row, geom)
##
##    return out
##
##def nearest_identity(groupbydata, valuedata,
##                     radius=None,   # only those within radius dist
##                     nearestidfield=None, keepfields=[]):
##    # specialized for only the one nearest match
##    # recording its distance, and optionally its id, and other attribute fields
##    # ...
##    pass

def closest_point(data, otherdata):
    """Returns a dataset of only the closest point to the other"""

    # TODO: not very effective right now
    # should use multipurpose nearest iterator...

    if not hasattr(otherdata, "spindex"):
        otherdata.create_spatial_index()

    from shapely.ops import nearest_points

    out = VectorData()
    out.fields = list(data.fields)
    
    for feat in data:
        shp = feat.get_shapely()
        othershps = (otherfeat.get_shapely() for otherfeat in otherdata)
        nearest = sorted(othershps, key=lambda othershp: shp.distance(othershp))[0]
        npoint = nearest_points(shp, nearest)[0]
        out.add_feature(feat.row, npoint.__geo_interface__)
        
    return out








# Path Analysis

def travelling_salesman(points, **options):
    pass












