from PIL import Image
import requests
from io import BytesIO

import os
import math
import hashlib
import tempfile

TILE_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'pythongis_tile_cache')

def lat_lon_to_web_mercator(lat, lon):
    """
    Convert latitude and longitude to Web Mercator (x, y).
    
    Parameters:
    - lat (float): Latitude in degrees.
    - lon (float): Longitude in degrees.
    
    Returns:
    - tuple: (x, y) in Web Mercator coordinates.
    """
    # Radius of the Earth in meters
    R = 6378137.0

    # Convert latitude and longitude to radians
    lat_rad = math.radians(lat)
    lon_rad = math.radians(lon)

    # Calculate Web Mercator x and y
    x = R * lon_rad
    y = R * math.log(math.tan(math.pi / 4 + lat_rad / 2))
    
    return x, y

def zoom_for_bbox(bbox, width, height):
    '''Determine zoom level from map extent and size'''
    # how many 256 tiles fit in map
    from math import floor, log2
    w,h = width,height
    tilexfit,tileyfit = w/256, h/256

    # how many map extents fit in world
    dx,dy = abs(bbox[2]-bbox[0]), abs(bbox[3]-bbox[1])
    extxfit,extyfit = 360/dx, 170.1022/dy
    
    # how many tiles total needed to cover entire world
    tilextot,tileytot = tilexfit*extxfit, tileyfit*extyfit
    
    # n is the number of tiles needed to cover world in each direction at zoom level z
    # n = 2**z  ->  log2(n) = z
    n = max(tilextot, tileytot)
    z = floor( log2(n) )

    return int(z)

def lon_to_x(lon, zoom):
    n = 2.0 ** zoom
    x = (lon + 180.0) / 360.0 * n
    return int(x)

def lat_to_y(lat, zoom):
    n = 2.0 ** zoom
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return int(y)

def bbox_to_tiles(bbox, zoom):
    lon1,lat1,lon2,lat2 = bbox
    min_lon, min_lat, max_lon, max_lat = (
        min(lon1,lon2),
        min(lat1,lat2),
        max(lon1,lon2),
        max(lat1,lat2),
    )
    min_x = lon_to_x(min_lon, zoom)
    min_y = lat_to_y(max_lat, zoom)
    max_x = lon_to_x(max_lon, zoom)
    max_y = lat_to_y(min_lat, zoom)

    # hacky padding
    #min_x -= 1
    #min_y -= 1
    #max_x += 1
    #max_y += 1

    tiles = []
    for x in range(min_x, max_x + 1):
        for y in range(min_y, max_y + 1):
            tiles.append((x, y, zoom))
    
    return tiles

# def bbox_to_dynamic_tiles(bbox, dynamic_func, max_zoom, max_recurs=7, sort_key=None):
#     '''For a given bbox, dynamically determine a list of tiles with varying zoom levels,
#     where the zoom level is determined based on the user-defined dynamic_func.'''
#     # first get the coarsest zoom level, ie a single tile of the entire bbox
#     zoom = zoom_for_bbox(bbox, 256, 256)
#     #print('initial zoom', zoom, bbox)

#     # then get the tiles at that zoom level
#     next_tiles = list(bbox_to_tiles(bbox, zoom))

#     # sort initial tiles if requested
#     if sort_key:
#         next_tiles = sorted(next_tiles, key=sort_key)

#     # initiate empty return list
#     tiles = []

#     # for each toplevel tile in bbox
#     print('initial tiles', next_tiles)
#     for tile in next_tiles:
#         # get and add to dynamic tiles
#         tiles = tile_to_dynamic_tiles(tile, dynamic_func, max_zoom, max_recurs, depth=0, tiles=tiles, sort_key=sort_key)

#     # return
#     print('final tiles', tiles)
#     return tiles

# def tile_to_dynamic_tiles(tile, dynamic_func, max_zoom, max_recurs=7, depth=0, tiles=None, sort_key=None):
#     # get tile bbox
#     x,y,z = tile
#     bbox = tile_to_bbox(x, y, z)

#     # run and check results of dynamic_func
#     tile_value = dynamic_func(x, y, z, bbox, max_zoom, max_recurs, depth)

#     if tile_value is None:
#         # tile shouldn't be processed at all
#         # don't do anything
#         pass

#     elif tile_value or z >= max_zoom or depth >= max_recurs:
#         # tile is sufficient, add to results tiles
#         tiles.append(tile)

#     else:
#         # tile is not sufficient, split into child tiles
#         x1,y1,x2,y2 = bbox
#         xoff,yoff = abs(x2-x1)/4, abs(y2-y1)/4
#         xmid,ymid = (x1+x2)/2, (y1+y2)/2
#         corners = [(xmid-xoff,ymid-yoff),
#                     (xmid+xoff,ymid-yoff),
#                     (xmid+xoff,ymid+yoff),
#                     (xmid-xoff,ymid+yoff)]
#         child_tiles = [bbox_to_tiles([cx, cy, cx, cy], z + 1)[0] for cx,cy in corners]

#         # sort tiles if requested
#         if sort_key:
#             child_tiles = sorted(child_tiles, key=sort_key)

#         # recurse further into each child tile
#         for child_tile in child_tiles:
#             tiles = tile_to_dynamic_tiles(child_tile, dynamic_func, max_zoom, max_recurs, depth + 1, tiles, sort_key=sort_key)

#     # return
#     return tiles

def tile_to_bbox(x, y, zoom):
    n = 2.0 ** zoom
    lon1 = (x / n * 360.0) - 180.0
    lat1 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lon2 = ((x + 1) / n * 360.0) - 180.0
    lat2 = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return lon1, lat1, lon2, lat2

def fetch_tile(x, y, zoom, tile_server):
    # Construct the tile URL
    tile_url = tile_server.format(x=x, y=y, z=zoom)
    
    # Fetch the tile image from the web
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
    timeout = 1
    response = requests.get(tile_url, headers=headers, timeout=timeout)
    
    if response.status_code == 200:
        # Open the image from the response
        image = Image.open(BytesIO(response.content))
        return image
    #else:
    #    # Return an empty image or handle the error as needed
    #    return Image.new('RGB', (256, 256), (255,0,0)) # red to indicate error

def fetch_tilecache(x, y, z, tile_server):
    folder = TILE_CACHE_DIR
    if not os.path.exists(folder):
        os.mkdir(folder)
    urlhash = hashlib.md5(tile_server.encode('utf8')).hexdigest()
    tilename = f'tile_cache_{urlhash}_x{x}_y{y}_z{z}.png'
    path = f'{folder}/{tilename}'
    #print(path)
    # fetch from cache or url
    if os.path.exists(path):
        # fetch cached tile image
        #print('opening tile from cache...')
        img = Image.open(path)
    else:
        # fetch img from url
        #print(f'fetching tile from url...')
        img = fetch_tile(x, y, z, tile_server)
        if img:
            # store in cache
            #print('saving...')
            img.save(path)
    # mark tile edges
    if 0: #img and img.mode == 'RGB':
        import numpy as np
        arr = np.array(img)
        color = [0,255,0]
        arr[0,:] = color
        arr[-1,:] = color
        arr[:,0] = color
        arr[:,-1] = color
        img = Image.fromarray(arr)
        # write tile coords
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        text = f"x{x}-y{y}-z{z}"
        draw.text((256//2, 256//2), text, font=font, fill=(0, 255, 0), anchor='mm')
    # return
    return img

def merge_tiles(tiles, tile_server):
    # Get zoom level, should only be one
    assert len(set(zoom for x,y,zoom in tiles)) == 1
    zoom = tiles[0][-1]

    # Calculate the size of the final image
    min_x = min(t[0] for t in tiles)
    min_y = min(t[1] for t in tiles)
    max_x = max(t[0] for t in tiles)
    max_y = max(t[1] for t in tiles)
    image_width = (max_x - min_x + 1) * 256
    image_height = (max_y - min_y + 1) * 256

    # Create a blank image to stitch the tiles onto
    result_image = Image.new('RGB', (image_width, image_height))

    for x in range(min_x, max_x + 1):
        for y in range(min_y, max_y + 1):
            # Fetch the tile image
            tile_image = fetch_tilecache(x, y, zoom, tile_server)

            # Paste the tile image onto the result image at the correct position
            if tile_image:
                result_image.paste(tile_image, (256 * (x - min_x), 256 * (y - min_y)) )

    return result_image

def tiles_to_bbox(tiles):
    tile_boxes = [tile_to_bbox(x, y, zoom)
                for x,y,zoom in tiles]
    box_xs = [t[0] for t in tile_boxes]
    box_xs += [t[2] for t in tile_boxes]
    box_ys = [t[1] for t in tile_boxes]
    box_ys += [t[3] for t in tile_boxes]
    xmin = min(box_xs)
    ymin = min(box_ys)
    xmax = max(box_xs)
    ymax = max(box_ys)
    return xmin,ymax,xmax,ymin
