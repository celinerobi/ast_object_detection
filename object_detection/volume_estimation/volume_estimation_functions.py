"""
Module containing functions to estimation tank volumes
"""

"""
Load Packages
"""

import os
import json
import tempfile
import shutil

import tqdm
from glob import glob

import numpy as np
import pandas as pd
import geopandas as gpd #important

import cv2
import laspy #las open #https://laspy.readthedocs.io/en/latest/
from shapely.ops import transform
from shapely.geometry import Point, Polygon #convert las to gpd
import rioxarray
import rasterio
from rasterio.warp import calculate_default_transform, reproject, Resampling
import pyproj
import rtree
import re
#$ pip install pygeos
import pygeos

import matplotlib as mpl
import matplotlib.pyplot as plt
from matplotlib import colors as mcolors
colors = dict(mcolors.BASE_COLORS, **mcolors.CSS4_COLORS)
import matplotlib.gridspec as gridspec # GRIDSPEC !
from matplotlib.colorbar import Colorbar # For dealing with Colorbars the proper way - TBD in a separate PyCoffee ?

"""
lidar functions 
"""
def project_list_of_points(initial_proj, final_proj, x_points, y_points):
    """ Convert a utm pair into a lat lon pair 
    Args: 
    initial_proj(str): the initial as a (proj crs)
    x_points(list): a list of the x coordinates for points to project
    y_points(list): a list of the y coordinates for points in las proj
    Returns: 
    (Geometry_wgs84): a list of shapely points in wgs84 proj
    """
    #https://gis.stackexchange.com/questions/127427/transforming-shapely-polygon-and-multipolygon-objects
    #get utm projection
    Geometry = [Point(xy) for xy in zip(x_points,y_points)] #make x+y coordinates into points
    #transform las into wgs84 point
    project = pyproj.Transformer.from_proj(initial_proj, final_proj, always_xy=True).transform
    Geometry_projected = [transform(project, point) for point in Geometry]
    return(Geometry_projected)
"""
DEM Processing
"""
def reproject_dems(initial_dem_dir, final_dem_dir):
    """ Reproject DEMS
    Args: 
    initial_dem_dir(str): directory holding DEMS with original projection
    final_dem_dir(str): directory to hold reprojected imaes 
    """
    initial_dem_paths = glob(initial_dem_dir + "/*.tif")
    dst_crs = 'EPSG:4326'
    for initial_dem_path in initial_dem_paths: #get the bounding box polygons
        dem_name = os.path.splitext(os.path.basename(initial_dem_path))[0]
        with rasterio.open(initial_dem_path) as src:
            transform, width, height = calculate_default_transform(
                src.crs, dst_crs, src.width, src.height, *src.bounds)
            kwargs = src.meta.copy()
            kwargs.update({'crs': dst_crs,
                            'transform': transform,
                            'width': width,
                            'height': height })
            with rasterio.open(os.path.join(final_dem_dir, dem_name+"_EPSG4326.tif"), 'w', **kwargs) as dst:
                for i in range(1, src.count + 1):
                    reproject(source=rasterio.band(src, i),
                                destination=rasterio.band(dst, i),
                                src_transform=src.transform,
                                src_crs=src.crs,
                                dst_transform=transform,
                                dst_crs=dst_crs,
                                resampling=Resampling.nearest)
                    
def getFeatures(gdf):
    """Function to parse features from GeoDataFrame in such a manner that rasterio wants them"""
    return [json.loads(gdf.to_json())['features'][0]['geometry']]

#Get DEM (tif) for each tank
#get utm crs
def get_poly_crs_from_epsg_to_utm(poly):
    """
    Take polygon with coordinates in EPSG get in UTM(meters)
    Args: 
    poly: a shapely olygon objects
    Returns:
    utm_crs: source raster 
    """
    utm_crs_list = pyproj.database.query_utm_crs_info(datum_name="WGS 84",
                                                      area_of_interest = pyproj.aoi.AreaOfInterest(west_lon_degree=poly.bounds[0],
                                                                                                   south_lat_degree=poly.bounds[1],
                                                                                                   east_lon_degree=poly.bounds[2],
                                                                                                   north_lat_degree=poly.bounds[3],),)
    utm_crs = pyproj.CRS.from_epsg(utm_crs_list[0].code)
    return(utm_crs)

def reproject_raster_mask_to_utm(poly, src, clipped_img, clipped_transform, tile_path = None):
    """
    Output values in UTM(meters), coordinates in EPSG
    For a given raster, reproject to a UTM. 
    Args: 
    poly: the original polygon cooresponding to the raster 
    src: source raster 
    out_img: raster mask
    out_transform: corresponding out transform for the raster mask 
    """
    temp_dirpath = tempfile.mkdtemp()
    if tile_path == None:
        tile_path = os.path.join(temp_dirpath, "temp_.tif")
        
    dst_crs = get_poly_crs_from_epsg_to_utm(poly) #utm_crs
    dst_transform, width, height = rasterio.warp.calculate_default_transform(src.crs, dst_crs,
                                                                             src.width, src.height, *src.bounds)
    out_meta = src.meta
    out_meta.update({"driver": "GTiff",
             "height": clipped_img.shape[1],
             "width": clipped_img.shape[2],
             "transform": clipped_transform})

    with rasterio.open(tile_path, 'w', **out_meta) as rast:
        for i in range(1, src.count + 1):
            reproject(source=clipped_img,
                      destination=rasterio.band(rast, i),
                      src_transform=clipped_transform,
                      src_crs=src.crs,
                      dst_transform=dst_transform,
                      dst_crs=dst_crs,
                      resampling=Resampling.nearest)
    #delete temp file
    if os.path.exists(temp_dirpath):
        shutil.rmtree(temp_dirpath)

def get_bounds_for_dems(dem_paths):
    #identify inundation bounds                               
    geometry = []
    dem_names = []
    for dem_path in dem_paths: #get the bounding box polygons
        dem_name = os.path.basename(dem_path)
        dem_names.append(dem_name)
        dem = rasterio.open(dem_path)
        min_lon, min_lat, max_lon, max_lat = dem.bounds
        geometry.append(Polygon([(min_lon,min_lat),(min_lon,max_lat),(max_lon,max_lat),(max_lon,min_lat)]))
    #make dataframe of inundation map bounds
    dem_bounds = gpd.GeoDataFrame({'name': dem_names,'dem_paths': dem_paths,'geometry': geometry})
    return(dem_bounds)

def dem_by_tank(dem_paths, tank_data, output_path):
    #get bounds for dems
    dem_bounds = get_bounds_for_dems(dem_paths)
    dem_bounds.crs = "EPSG:4326"
    #get the dem paths for each tank
    dem_bounds_by_tank = gpd.sjoin(dem_bounds, tank_data, how="right")
    dem_bounds_by_tank = dem_bounds_by_tank.dropna(subset=['dem_paths'])
    
    #iterate over each dem
    tank_poly_grouped_by_dem = dem_bounds_by_tank.groupby(dem_bounds_by_tank.dem_paths) #group gpds by dem
    for dem_path, tank_poly_by_dem in tqdm.tqdm(tank_poly_grouped_by_dem): 
        dem = rasterio.open(dem_path) #load dem
        #iterate over each tank
        tank_poly_by_dem_grouped_by_tank = tank_poly_by_dem.groupby(tank_poly_by_dem.id) #group gpds by tank
        for id_, tank_poly_by_dem_by_tank in tank_poly_by_dem_grouped_by_tank: 
            output_filename = os.path.join(output_path, "DEM_data_tank_id_" + id_ + ".tif") #get output filename
            #get tank polygon
            tank_poly = tank_poly_by_dem_by_tank["geometry"].iloc[0] 
            geo = gpd.GeoDataFrame({'geometry': tank_poly}, index=[0], crs="EPSG:4326")
            coords = getFeatures(geo) 
            #clip dem to tank
            clipped_img, clipped_transform = rasterio.mask.mask(dataset=dem, shapes=coords, crop=True)
            #reproject
            reproject_raster_mask_to_utm(tank_poly, dem, clipped_img, clipped_transform, output_filename)
            
def identify_tank_ids(lidar_by_tank_output_path, DEM_by_tank_output_path):
    regex = re.compile(r'\d+')
    #the tank ids with corresponding lidar data 
    tank_ids_lidar = []
    lidar_by_tank_geojson_name = os.listdir(lidar_by_tank_output_path)
    for lidar_by_tank in lidar_by_tank_geojson_name:
        tank_ids_lidar.append([int(x) for x in regex.findall(lidar_by_tank)][0])

    #the tank ids with corresponding DEM data 
    #vol_est.remove_thumbs(DEM_by_tank_output_path) #remove thumbs 
    tank_ids_dem = []
    DEM_by_tank_tif_name = os.listdir(DEM_by_tank_output_path)
    for DEM_by_tank in DEM_by_tank_tif_name:
        tank_ids_dem.append([int(x) for x in regex.findall(DEM_by_tank)][0])

    #the tank ids with both lidar and DEMs
    tank_ids = list(set(tank_ids_dem).intersection(tank_ids_lidar))
    tank_ids = [str(i) for i in tank_ids]

    #paths to the DEM and lidar data 
    lidar_path_by_tank_for_height = []
    DEM_path_by_tank_for_height = []

    for tank_id in tank_ids:
        lidar_path_by_tank_for_height.append(os.path.join(lidar_by_tank_output_path, [string for string in lidar_by_tank_geojson_name if tank_id in string][0]))
        DEM_path_by_tank_for_height.append(os.path.join(DEM_by_tank_output_path, [string for string in DEM_by_tank_tif_name if tank_id in string][0]))
    return(tank_ids, lidar_path_by_tank_for_height, DEM_path_by_tank_for_height)

def add_bare_earth_data_to_lpc_by_tank_data(lidar_path_by_tank_for_height, DEM_path_by_tank_for_height):
    for i, (lidar_path, DEM_path) in enumerate(zip(lidar_path_by_tank_for_height, DEM_path_by_tank_for_height)):
        # Read in each lidar dataset
        lidar = gpd.read_file(lidar_path)
        lidar_coords = [(x,y) for x, y in zip(lidar["X coordinate"], lidar["Y coordinate"])] #'EPSG:4326' coords

        # Open the DEM raster data and store metadata
        dem_src = rasterio.open(DEM_path)

        # Sample the raster at every point location and store values in DataFrame
        lidar['bare_earth_elevation'] = [z[0] for z in dem_src.sample(lidar_coords)]

        with open(lidar_path, "w") as file:
            file.write(lidar.to_json()) 

"""
Add average base elevation to dataframe
"""     
def average_bare_earth_elevation_for_tanks(gdf, tank_data, dem_paths):    
    """ Calculate the diameter of a given bounding bbox for imagery of a given resolution
    Arg:
    bbox(list): a list of the (xmin, ymin, xmax, ymax) coordinates for box 
    resolution(float): the (gsd) resolution of the imagery
    Returns:
    (diameter): the diameter of the bbox of interest
    """

    tank_data_w_lpc = gpd.sjoin(dem_bounds, tank_data, how = "left")

    for tank_index, tank_poly in tqdm.tqdm(enumerate(tank_data["geometry"])): #iterate over the tank polygons
        if dem_bounds.contains(tank_poly): #identify whether the tank bbox is inside of the state polygon
            index.append(tank_index) #add state name for each tank to list 
    tank_data_in_lidar_extent = tank_data.iloc[index]
    """
    #get average bare earth elevation values values
    for dem_index, dem_poly in enumerate(dem_bounds["geometry"]): #iterate over the dem polygons
        if dem_poly.contains(tank_poly): #identify whether the bbox is inside of the dem map
            #make a geodataframe for each tank polygon that is contained within the dem
            geo = gpd.GeoDataFrame({'geometry': tank_poly}, index=[0], crs=gdf.crs)
            coords = getFeatures(geo) 
            dem = rasterio.open(dem_paths[dem_index])
            out_img, out_transform = rasterio.mask.mask(dataset=dem, shapes=coords, crop = True)
            #average_bare_earth_elevation[tank_index] = np.average(out_img)
            #average to reprojected raster
            out_img_utm = reproject_raster_mask_to_utm(tank_poly, dem, out_img, out_transform)
            average_bare_earth_elevation[tank_index] = np.average(out_img_utm)
    #add inundation values to tank database 
    return(gdf)

    #3. Get the extent of the Lidar data 
    minx, miny, maxx, maxy = lidar["geometry"].total_bounds
    lidar_extent = Polygon([(minx,miny), (minx,maxy), (maxx,maxy), (maxx,miny)])
    

    
    #5. Get the LP corresponding with the tank dataset
    tank_data_w_lpc = gpd.sjoin(tank_data_in_lidar_extent,lidar)
    tank_data_w_lpc = tank_data_w_lpc.dropna(subset=['Z coordinate'])
    #save geodatabase as json
    with open(os.path.join(args.output_tile_level_annotation_path, las_name+".geojson"), 'w') as file:
        file.write(tank_data_w_lpc.to_json()) 
    """
    
def remove_thumbs(path_to_folder_containing_images):
    """ Remove Thumbs.db file from a given folder
    Args: 
    path_to_folder_containing_images(str): path to folder containing images
    Returns:
    None
    """
    if len(glob(path_to_folder_containing_images + "/*.db", recursive = True)) > 0:
        os.remove(glob(path_to_folder_containing_images + "/*.db", recursive = True)[0])

def add_titlebox(ax, text):
    ax.text(.55, .8, text,
    horizontalalignment='center',
    transform=ax.transAxes,
    bbox=dict(facecolor='white', alpha=0.6),
    fontsize=12.5)
    return ax
def height_estimation_figs(tank_ids, lidar_path_by_tank_for_height, DEM_path_by_tank_for_height, plot_path, tiles_dir):
    for i, (tank_id, lidar_path, DEM_path) in enumerate(zip(tank_ids, lidar_path_by_tank_for_height, DEM_path_by_tank_for_height)):
        tank_id = str(tank_id)
        #Read in data 
        ##read in lidar
        lidar = gpd.read_file(lidar_path)
        #lidar = gpd.read_file(os.path.join(lidar_dir,"lidar_tank_id_"+tank_id+".geojson"))
        lidar["lpc_bee_difference"] = lidar["Z coordinate"]-lidar["bare_earth_elevation"]
        lidar.drop(lidar[(lidar['bare_earth_elevation'] ==-999999)].index, inplace=True) #remove no data values
        tank_class = lidar["object_class"].iloc[0]
        ##reproject for plotting
        wgs84 = pyproj.CRS('EPSG:4326')
        utm = pyproj.CRS(lidar["utm_projection"].iloc[0])
        Geometry = vol_est.project_list_of_points(wgs84, utm, lidar["X coordinate"], lidar["Y coordinate"])
        x_y_utm = gpd.GeoDataFrame({'geometry': Geometry})
        X = gpd.GeoDataFrame(x_y_utm).bounds["minx"]
        Y = gpd.GeoDataFrame(x_y_utm).bounds["miny"]
        ##Read in imagery for tank
        tile_path = os.path.join(tiles_dir, lidar["tile_name"].iloc[0]+".tif")
        tile = cv2.imread(tile_path, cv2.IMREAD_UNCHANGED)
        x_max = int(lidar['maxx_polygon_pixels'].iloc[0])
        y_max = int(lidar['maxy_polygon_pixels'].iloc[0])
        x_min = int(lidar['minx_polygon_pixels'].iloc[0])
        y_min = int(lidar['miny_polygon_pixels'].iloc[0])
        tank = tile[y_min:y_max, x_min:x_max]
        ##read in dem
        dem_test = rasterio.open(DEM_path) 
        dem = dem_test.read(1)
        dem[dem==-999999] = np.nan
        dem_test.close()
        #Make figure
        fig = plt.figure(figsize=(12, 8))
        fig.suptitle('Height Estimation for tank id #' +tank_id+"in class "+tank_class, fontsize=16)
        #Define ranges and colors for colorbar, and for the image
        ##min and max values
        vmin=lidar[["Z coordinate","bare_earth_elevation","lpc_bee_difference"]].min().min()
        vmax=lidar[["Z coordinate","bare_earth_elevation","lpc_bee_difference"]].max().max()
        norm = mpl.colors.Normalize(vmin=vmin, vmax=vmax)
        ##get colorbar
        current_cmap = mpl.cm.get_cmap('terrain').copy()
        current_cmap.set_bad(color='red')
        #make grided plot
        widths = [.5, .5, .5, .5, .5, .5, 2]
        heights = [.5, .5, .5, .5, .5, .5, .5, .5, 0.1]
        gs = gridspec.GridSpec(9, 7, width_ratios=widths,height_ratios=heights)
        #plot raw data
        ## Image
        ax_img = plt.subplot(gs[:2, 1:3])
        ax_img.imshow(tank, cmap=current_cmap, norm=norm, aspect="auto")
        ax_img.set_xticks([])
        ax_img.set_yticks([])
        ax_img.set_title('NAIP Imagery') 
        print(ax_img.get_xlim(),ax_img.get_ylim())
        #ax_img.set_aspect('auto', adjustable='box')  # NEW
        ##DEM
        ax_dem = plt.subplot(gs[:2, 3:5])
        ax_dem.imshow(dem, cmap=current_cmap, norm=norm, aspect="auto")
        ax_dem.set_xticks([])
        ax_dem.set_yticks([])
        ax_dem.set_title('Digital Elevation Model')
        #ax_dem.set_aspect(asp)
        #BEE
        ax_bee = plt.subplot(gs[2:4, 1:3])
        ax_bee.scatter(X,Y, c=lidar["bare_earth_elevation"], cmap=current_cmap, norm=norm)
        ax_bee.set_xticks([])
        ax_bee.set_yticks([])
        ax_bee.set_title('Bare Earth Elevation')
        asp = np.diff(ax_bee.get_xlim())[0] / np.diff(ax_bee.get_ylim())[0]
        #ax_bee.set_aspect(asp)
        ##LPC
        ax_lpc = plt.subplot(gs[2:4, 3:5])
        ax_lpc.scatter(X,Y, c=lidar["Z coordinate"], cmap=current_cmap, norm=norm)
        ax_lpc.set_xticks([])
        ax_lpc.set_yticks([])
        ax_lpc.set_title('Lidar Point Cloud')
        #ax_lpc.set_aspect(asp)
        #Difference between DSM and DEM over all values bounding box
        H = round(lidar["lpc_bee_difference"].mean(),2)
        axbboxdist = plt.subplot(gs[4:6, :2])
        axbboxdist.set_title('LPC - DEM')
        add_titlebox(axbboxdist, '(H ='+str(H)+'m)')
        axbboxdist.scatter(X,Y, c=lidar["lpc_bee_difference"], cmap=current_cmap, norm=norm)
        axbboxdist.set_xticks([])
        axbboxdist.set_yticks([])
        #axbboxdist.set_aspect(asp)
        axbboxdist.set_aspect('auto', adjustable='box')
        #Difference between DSM and DEM for all DSM values greater than the 25th quantile 
        Q25 = lidar["Z coordinate"].quantile(.25) 
        idxs = np.where(lidar["Z coordinate"] > Q25)[0]
        H = round(lidar["lpc_bee_difference"].iloc[idxs].mean(),2)
        axQ25 = plt.subplot(gs[4:6, 2:4])
        axQ25.set_title('LPC (over Q25['+str(Q25)+']) - DEM')
        add_titlebox(axQ25, '(H ='+str(H)+'m)')
        axQ25.scatter(X.iloc[idxs], Y.iloc[idxs], c=lidar["lpc_bee_difference"].iloc[idxs], cmap=current_cmap, norm=norm)
        axQ25.set_xticks([])
        axQ25.set_yticks([])
        #axQ25.set_aspect(asp)
        #Difference between DSM and DEM for all DSM values greater than the mean
        mean = lidar["Z coordinate"].mean()
        idxs = np.where(lidar["Z coordinate"] > mean)[0]
        H = round(lidar["lpc_bee_difference"].iloc[idxs].mean(),2)
        axmean = plt.subplot(gs[4:6, 4:6])
        axmean.set_title('LPC - (over mean ['+str(round(mean,2))+']) DEM')
        add_titlebox(axmean, '(H ='+str(H)+'m)')
        axmean.scatter(X.iloc[idxs], Y.iloc[idxs], c=lidar["lpc_bee_difference"].iloc[idxs], cmap=current_cmap, norm=norm)
        axmean.set_xticks([])
        axmean.set_yticks([])
        #axmean.set_aspect(asp)             
        #Difference between DSM and DEM for all DSM values greater than the median
        median = lidar["Z coordinate"].median()
        idxs = np.where(lidar["Z coordinate"] > median)[0]
        H = round(lidar["lpc_bee_difference"].iloc[idxs].mean(),2)
        axmedian = plt.subplot(gs[6:8, :2])
        axmedian.set_title('LPC - (over Q50 ['+str(median)+']) DEM')
        add_titlebox(axmedian, '(H ='+str(H)+'m)')
        axmedian.scatter(X.iloc[idxs], Y.iloc[idxs], c=lidar["lpc_bee_difference"].iloc[idxs], cmap=current_cmap, norm=norm)
        axmedian.set_xticks([])
        axmedian.set_yticks([])
        #axmedian.set_aspect(asp)
        #Difference between DSM and DEM for all DSM values greater than the 75th quantile 
        Q75 = lidar["Z coordinate"].quantile(.75) 
        idxs = np.where(lidar["Z coordinate"] > Q75)[0]
        H = round(lidar["lpc_bee_difference"].iloc[idxs].mean(),2)
        axQ75 = plt.subplot(gs[6:8, 2:4])
        axQ75.set_title('LPC - (over Q75 ['+str(Q75)+') DEM')
        add_titlebox(axQ75, '(H ='+str(H)+'m)')
        axQ75.scatter(X.iloc[idxs], Y.iloc[idxs], c=lidar["lpc_bee_difference"].iloc[idxs], cmap=current_cmap, norm=norm)
        axQ75.set_xticks([])
        axQ75.set_yticks([])
        #axQ75.set_aspect(asp)
        #Difference between DSM and DEM for all DSM values greater than the 75th quantile 
        Q90 = lidar["Z coordinate"].quantile(.90) 
        idxs = np.where(lidar["Z coordinate"] > Q90)[0]
        H = round(lidar["lpc_bee_difference"].iloc[idxs].mean(),2)
        axQ90 = plt.subplot(gs[6:8, 4:6])
        axQ90.set_title('LPC - (over Q75 ['+str(Q90)+') DEM')
        add_titlebox(axQ90, '(H ='+str(H)+'m)')
        axQ90.scatter(X.iloc[idxs], Y.iloc[idxs], c=lidar["lpc_bee_difference"].iloc[idxs], cmap=current_cmap, norm=norm)
        axQ90.set_xticks([])
        axQ90.set_yticks([])
        axQ90.set_aspect(asp)
        #Distribution of LPC
        axhist = plt.subplot(gs[1:7, 6])
        axhist.set_aspect('equal', adjustable='box')  # NEW
        axhist.set_title('LPC Distribution')
        axhist.axvline(x = lidar["Z coordinate"].quantile(1/4), color = 'orange', label = '25% Q')
        axhist.axvline(x = median, color = 'red', label = 'median')
        axhist.axvline(x = mean, color = 'black', label = 'mean')
        axhist.axvline(x = lidar["Z coordinate"].mode()[0], color = 'purple', label = 'mode')
        axhist.axvline(x = Q75, color = 'orange', label = '75% Q')
        axhist.hist(lidar["Z coordinate"], bins = int(len(lidar["Z coordinate"])/100),
                    color = 'blue', edgecolor = 'blue')
        axhist.set_yticks([])
        axhist.set_aspect('auto', adjustable='box')  # NEW
        axhist.legend(loc="upper left")
        #Add in color bar
        cbax = plt.subplot(gs[8, 0:6])
        cb = fig.colorbar(mpl.cm.ScalarMappable(norm=norm, cmap=current_cmap),
                          cax=cbax, orientation='horizontal' )#use the defined variables cmap and norm
        cb.ax.tick_params(labelsize=10) #set ticks
        cb.set_label('Elevation (m)',fontsize=12) #label colorbar
        plt.tight_layout()
        #plot
        plt.close(fig)
        path = os.path.join(plot_path,tank_class)
        os.makedirs(path, exist_ok = True)
        fig.savefig(path, tank_id+".jpg")
        
def write_list(list_, file_path):
    print("Started writing list data into a json file")
    with open(file_path, "w") as fp:
        json.dump(list_, fp)
        print("Done writing JSON data into .json file")

# Read list to memory
def read_list(file_path):
    # for reading also binary mode is important
    with open(file_path, 'rb') as fp:
        list_ = json.load(fp)
        return list_

