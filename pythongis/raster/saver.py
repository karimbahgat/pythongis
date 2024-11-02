
# builtins
import os

# import PIL as the saver
import PIL
import PIL.TiffImagePlugin
import PIL.TiffTags

def to_file(bands, meta, filepath, **kwargs):
    
    def combine_bands(bands):
        # saving in image-like format, so combine and prep final image
        if len(bands) == 1:
            img = bands[0].img
            return img
        elif len(bands) == 3:
            # merge all images together
            mode = "RGB"
            bands = [band.img for band in bands]
            img = PIL.Image.merge(mode, bands)
            return img
        elif len(bands) == 4:
            # merge all images together
            mode = "RGBA"
            bands = [band.img for band in bands]
            img = PIL.Image.merge(mode, bands)
            return img
        else:
            # raise error if more than 4 bands, because PIL cannot save such images
            raise Exception("Cannot save more than 4 bands to one file; split and save each band separately")

    def create_world_file(savepath, geotrans):
        dir, filename_and_ext = os.path.split(savepath)
        filename, extension = os.path.splitext(filename_and_ext)
        world_file_path = os.path.join(dir, filename) + ".wld"
        with open(world_file_path, "w") as writer:
            # note that the params are arranged slightly differently
            # ...in the world file from the usual affine a,b,c,d,e,f
            # ...so rearranging their sequence
            xscale,xskew,xoff,yskew,yscale,yoff = geotrans
            string = '\n'.join((str(val) for val in [xscale,yskew,xskew,yscale,xoff,yoff]))
            writer.write( string )

    def create_prj_file(filepath, crs):
        root,ext = os.path.splitext(filepath)
        with open('{}.prj'.format(root), 'w') as writer:
            wkt = crs.to_esri_wkt()
            writer.write(wkt)

    if filepath.endswith((".ascii",".asc")):
        # create header
        width, height = img.size()
        xscale,xskew,xoffset, yskew,yscale,yoffset = meta["affine"]
        
        xorigtype = "xllcenter"
        yorigtype = "yllcenter"

        header = ""
        header += "NCOLS %s \n"%width
        header += "NROWS %s \n"%height
        header += "%s %s \n"%(xorigtype,xoffset)
        header += "%s %s \n"%(yorigtype,yoffset)
        if xscale != yscale:
            raise Exception("When saving to ascii format xscale and yscale must be the same, ie each cell must be a perfect square")
        header += "CELLSIZE %s \n"%xscale
        header += "NODATA_VALUE %s \n"%bands[0].nodataval # TODO: only temp hack to use nodataval of first band

        # write bands
        filename_root, ext = os.path.splitext(filepath)
        for i, band in enumerate(bands):
            (img, cells) = band
            newpath = filename_root + "_" + i + ext
            with open(newpath, "w") as tempfile:
                # write header
                tempfile.write(header)
                # write cells
                for y in range(height):
                    row = " ".join((str(cells[x,y]) for x in range(width)))+"\n"
                    tempfile.write(row)
                    
        # finally create world file for the geotransform
        create_world_file(newpath, meta["affine"])

        # create the prj file for the crs
        create_prj_file(filepath, meta["crs"])

    elif filepath.endswith((".tif", ".tiff", ".geotiff")):        
        # write directly to tag meta
        PIL.TiffImagePlugin.WRITE_LIBTIFF = False
        tags = PIL.TiffImagePlugin.ImageFileDirectory()

        # GTRasterTypeGeoKey, aka midpoint pixels vs topleft area pixels
        # pythongis rasters are always area
        tags[1025] = 1.0
        tags.tagtype[1025] = 12 #double, only works with PIL patch

        # ModelTiepointTag
        xscale,xskew,xoffset, yskew,yscale,yoffset = meta["affine"]
        x,y = 0,0
        geo_x,geo_y = xoffset,yoffset
        tags[33922] = tuple(map(float,[x,y,0,geo_x,geo_y,0]))
        tags.tagtype[33922] = 12 #double, only works with PIL patch

        # ModelPixelScaleTag
        tags[33550] = tuple(map(float,[xscale,yscale,0]))
        tags.tagtype[33550] = 12 #double, only works with PIL patch

        # ModelTransformationTag, aka 4x4 transform coeffs...
        xscale,xskew,xoff, yskew,yscale,yoff = meta["affine"]
        a,b,d = xscale,xskew,xoff
        e,f,h = yskew,yscale,yoff
        x4_coeffs = [a,b,0,d,
                     e,f,0,h,
                     0,0,0,0,
                     0,0,0,0]
        tags[34264] = tuple(map(float,x4_coeffs))
        tags.tagtype[34264] = 12 #double, only works with PIL patch

        # nodata
        nodataval = bands[0].nodataval # # TODO: only temp hack to use nodataval of first band
        if nodataval != None:
            tags[42113] = str(nodataval)
            tags.tagtype[42113] = 2 #ascii dtype

        # finally save the file using tiffmeta headers
        img = combine_bands(bands)
        img.save(filepath, tiffinfo=tags, **kwargs)

        # crs should be saved as part of the geotiff tags, but not yet implemented
        # for now create a separate prj file for the crs
        create_prj_file(filepath, meta["crs"])

    elif filepath.endswith((".jpg",".jpeg",".png",".bmp",".gif")):
        # save
        img = combine_bands(bands)
        if not filepath.endswith((".png",".gif")):
            img = img.convert('RGB') # only png and gif support alpha band
        img.save(filepath, **kwargs)
        
        # write world file
        create_world_file(filepath, meta["affine"])

        # create the prj file for the crs
        create_prj_file(filepath, meta["crs"])

    elif filepath.lower().endswith(".txt"):
        # cell by cell table format
        with open(filepath, "w") as writer:
            bandnum = meta.get("bandnum", 0)
            band = bands[bandnum]
            pixelaccess = band.load()
            w,h = band.size
            delimiter = meta.get("delimiter", None)
            cellid = meta.get("cellid", "colrow")
            
            if cellid == "xy":
                fields = ["x","y","value"]
                writer.write("\t".join(fields)+"\n")
                xscale,xskew,xoffset, yskew,yscale,yoffset = meta["affine"]
                for row in range(h):
                    for col in range(w):
                        val = pixelaccess[col,row]
                        x,y = col*xscale + row*xskew + xoffset, col*yscale + row*yskew + yoffset
                        row = [x,y,val]
                        writer.write("\t".join(row)+"\n")
                        
            elif cellid == "colrow":
                fields = ["column","row","value"]
                writer.write("\t".join(fields)+"\n")
                for row in range(h):
                    for col in range(w):
                        val = pixelaccess[col,row]
                        row = [col,row,val]
                        writer.write("\t".join(row)+"\n")
                        
            else:
                raise Exception("Cellid option must be either xy or colrow")
            
        # write world file
        create_world_file(filepath, meta["affine"])

        # create the prj file for the crs
        create_prj_file(filepath, meta["crs"])

    else:

        raise Exception("Could not save a raster to the given filepath: the filetype extension is either missing or not supported")

