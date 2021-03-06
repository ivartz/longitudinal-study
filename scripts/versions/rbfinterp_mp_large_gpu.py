import numpy as np
import nibabel as nib
from nilearn.image import resample_to_img
from scipy.interpolate import Rbf
import torch.multiprocessing as mp
#from multiprocessing import shared_memory, set_start_method
#from multiprocessing.pool import Pool
#from torch.multiprocessing import set_start_method
"""
try:
     set_start_method('spawn')
except RuntimeError:
    pass
"""
import argparse
#import copy
import pathlib
import sys
import torch
#import os.path
#sys.path.append('/home/ivar/Downloads/keops') # For enabling import of pykeops
#from pykeops.torch import LazyTensor

# _t suffix in names means tuple
# _l suffix in names means list
# _q suffix in name means a multiprocessing queue
# etc.

def flatten_tensor(T):
    # Flattens the M x N x ..., x D tensor T while preserving original indices
    # to the output shape (MxNx...xD, F, M, N, ..., D)
    # where F is a vector of the values in T, and M, N, ..., D a vector of 
    # original indices in each dimension in T (for the values in F).
    # https://stackoverflow.com/questions/46135070/generalise-slicing-operation-in-a-numpy-array/46135084#46135084
    n = T.ndim
    grid = np.ogrid[tuple(map(slice, T.shape))]
    out = np.empty(T.shape + (n+1,), dtype=T.dtype)
    for i in range(n):
        out[...,i+1] = grid[i]
    out[...,0] = T
    out.shape = (-1,n+1)
    # Return everything
    return out
    # Only return voxels that are not np.nan
    #return out[~np.isnan(out[:,0])]
    # Only return voxels that are not zero
    #return out[out[:,0] != 0]

def calculate_default_chunksize(num_items, num_workers):
    # Taken from source code for Python 3.8.0, line 468 (_map_async)
    # https://github.com/python/cpython/blob/v3.8.0/Lib/multiprocessing/pool.py
    chunksize, extra = divmod(num_items, num_workers * 4)
    if extra:
        chunksize += 1
    if num_items == 0:
        chunksize = 0
    return chunksize
"""
def calculate_default_epsilon_rbf_multiquadric_4D(x):
    
    # https://github.com/scipy/scipy/blob/v1.4.1/scipy/interpolate/rbf.py#L59-L290
    # default epsilon is the "the average distance between nodes" based
    # on a bounding hypercube
    
    xi = np.asarray([np.asarray(a, dtype=np.float_).flatten()
                                  for a in (x[:,0], x[:,1], x[:,2], x[:,3])])
    N = xi.shape[-1]
    ximax = np.amax(xi, axis=1)
    ximin = np.amin(xi, axis=1)
    edges = ximax - ximin
    edges = edges[np.nonzero(edges)]
    return np.power(np.prod(edges)/N, 1.0/edges.size)
"""
"""
def multiquadric_kernel(x, y, epsilon=1):
    
    #For use in pykeops
    
    x_i = LazyTensor(x[:, None, :])  # (M, 1, :)
    y_j = LazyTensor(y[None, :, :])  # (1, N, :)
    D_ij = ((x_i - y_j) ** 2).sum(-1)  # (M, N) symbolic matrix of squared distances
    return ((1/epsilon * D_ij) ** 2 + 1).sqrt()
"""
def interpolate_volume_elements_to_linear_time(full_volumes_data_arr, \
                                               intervals_between_volumes_t, \
                                               z_interval_t, \
                                               y_interval_t, \
                                               x_interval_t):
    num_vols = len(full_volumes_data_arr)
    assert num_vols - 1 == len(intervals_between_volumes_t), \
    "Non-matching number of volumes and intervals"
    volumes_data_l = [None for _ in range(num_vols)]
    for num in range(num_vols):
        # Get volume element data
        volumes_data_l[num] = \
                       full_volumes_data_arr[num][z_interval_t[0]:z_interval_t[1], \
                                                  y_interval_t[0]:y_interval_t[1], \
                                                  x_interval_t[0]:x_interval_t[1]]
    # Stack all volumes along a new time dimension, creating the data with shape (t, z, y, x)
    data = np.stack(tuple(volumes_data_l))
            
    # Get the resulting dimensions of the stacked data
    # for later use in the grid defition
    tdim, zdim, ydim, xdim = data.shape
    
    # Flatten the stacked data, for use in Rbf
    data_flattened = flatten_tensor(data)
    
    # Get the colums in the flattened data
    # The voxel values
    f = data_flattened[:,0]
    # Time coordinates of the voxel values
    t = data_flattened[:,1]
    # Z coordinates of the voxel values
    z = data_flattened[:,2]
    # Y coordinates of the voxel values
    y = data_flattened[:,3]
    # X coordinates of the voxel values
    x = data_flattened[:,4]
    
    # Make grids of indices with resolutions we want after the interpolation
    grids = [np.mgrid[time_idx:time_idx+1:1/interval_duration, 0:zdim, 0:ydim, 0:xdim] \
    for time_idx, interval_duration in enumerate(intervals_between_volumes_t)]
    
    # Stack all grids
    TI, ZI, YI, XI = np.hstack(tuple(grids))
    
    # Create radial basis functions
    #rbf_clinst = Rbf(t, z, y, x, f, function="multiquadric", norm='euclidean')
    rbf = Rbf(t, z, y, x, f, function='multiquadric') # If scipy 1.1.0 , only euclidean, default
    
    # Interpolate the voxel values f to have values for the indices in the grids,
    # resulting in interpolated voxel values FI
    # This uses the Rbfs
    FI = rbf(TI, ZI, YI, XI)
    
    data_interpolated = FI
    
    return data_interpolated

def interpolate_volume_elements_to_linear_time_gpu(kernel_func, \
                                               full_volumes_data_arr, \
                                               intervals_between_volumes_t, \
                                               z_interval_t, \
                                               y_interval_t, \
                                               x_interval_t):
    # CPU
    num_vols = len(full_volumes_data_arr)
    assert num_vols - 1 == len(intervals_between_volumes_t), \
    "Non-matching number of volumes and intervals"
    volumes_data_l = [None for _ in range(num_vols)]
    for num in range(num_vols):
        # Get volume element data
        volumes_data_l[num] = \
                       full_volumes_data_arr[num][z_interval_t[0]:z_interval_t[1], \
                                                  y_interval_t[0]:y_interval_t[1], \
                                                  x_interval_t[0]:x_interval_t[1]]
    # Stack all volumes along a new time dimension, creating the data with shape (t, z, y, x)
    data = np.stack(tuple(volumes_data_l))
            
    # Get the resulting dimensions of the stacked data
    # for later use in the grid defition
    tdim, zdim, ydim, xdim = data.shape
    
    # Flatten the stacked data, for use in Rbf
    data_flattened = flatten_tensor(data)
    
    # Get the colums in the flattened data
    # The voxel values
    b = data_flattened[:,0]
    # The rest
    x = data_flattened[:,1:]
    
    # GPU
    dtype = torch.cuda.FloatTensor
    
    # Transfer to GPU
    #epsilon = torch.from_numpy(np.array(epsilon)).type(dtype)
    b = torch.from_numpy(b).type(dtype).view(-1,1)
    x = torch.from_numpy(x).type(dtype)
    
    K_xx = kernel_func(x, x, epsilon="Default")
    
    epsilon = kernel_func.epsilon
    
    #print("er det her den stopper?")
    #sys.stdout.flush()

    #print("sdkbfsjkd!")
    #sys.stdout.flush()
    #alpha = 10  # Ridge regularization
    #a = K_xx.solve(b, alpha=alpha)
    a = K_xx.solve(b)
    #print("eller her?")
    #sys.stdout.flush()
    
    ZI = torch.from_numpy(np.mgrid[0:zdim]).type(dtype)
    YI = torch.from_numpy(np.mgrid[0:ydim]).type(dtype)
    XI = torch.from_numpy(np.mgrid[0:xdim]).type(dtype)
    
    # CPU & GPU
    TI = torch.stack(tuple(torch.from_numpy(np.mgrid[time_idx:time_idx+1:1/interval_duration]).type(dtype) \
                    for time_idx, interval_duration in enumerate(intervals_between_volumes_t)), dim=0).type(dtype).view(-1)
    TI, ZI, YI, XI = torch.meshgrid(TI, ZI, YI, XI)
    #"""

    
    grid = torch.stack((TI.contiguous().view(-1), \
                     ZI.contiguous().view(-1), \
                     YI.contiguous().view(-1), \
                     XI.contiguous().view(-1)), dim=1)
    #"""
    """
    grid = torch.stack((TI.view(-1), \
                     ZI.view(-1), \
                     YI.view(-1), \
                     XI.view(-1)), dim=1)
    """
    K_gridx = kernel_func(grid, x, epsilon=epsilon)
    
    data_interpolated = K_gridx @ a
    
    data_interpolated = data_interpolated.view(np.sum(intervals_between_volumes_t), zdim, ydim, xdim)
    
    #print(data_interpolated)
    
    #del TI, ZI, YI, XI, grid, K_gridx, a, b, x, epsilon
    
    return data_interpolated

def vol_get_subvols_interval_indexes_all(vol_shape, subvol_shape, stride_shape):
    
    # Orig volume shape
    zdim, ydim, xdim = vol_shape
    
    #
    stride_z, stride_y, stride_x = stride_shape
    
    # Subvol shape
    vol_dim_z, vol_dim_y, vol_dim_x = subvol_shape
    
    # Calculate the number of non-overlapping volumes along each dimension
    num_vols_z, num_vols_y, num_vols_x = zdim//stride_z, ydim//stride_y, xdim//stride_x
    # For testing purposes
    #num_vols_z, num_vols_y, num_vols_x = 5, 5, 5

    # 
    extra_vol_dim_z, extra_vol_dim_y, extra_vol_dim_x = zdim % stride_z, ydim % stride_y, xdim % stride_x
    
    # A list of tuples containing interval indexes along each dimension
    interval_indexes_l = []
    
    for vol_num_z in range(num_vols_z):
        for vol_num_y in range(num_vols_y):
            for vol_num_x in range(num_vols_x):
                # -- Stride version --
                # Most cases
                # Check that volume is within the index dimensions
                if vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    # Create slice intervals, defining location of subvolume
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # Edge cases
                # If not extra vol dims
                # 1. z, <=y, <=x
                if not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 2. <=z, y, <=x
                elif not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 3. <=z, <=y, x
                elif not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 4. z, y, <=x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 5. <=z, y, x
                elif not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 6. z, <=y, x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 7. z, y, x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # If extra vol dims
                # 1. z, <=y, <=x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 2. <=z, y, <=x
                elif extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 3. <=z, <=y, x
                elif extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 4. z, y, <=x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 5. <=z, y, x
                elif extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 6. z, <=y, x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
                # 7. z, y, x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1:
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
    
    return interval_indexes_l

def check_if_mask_is_a_cube(mask):
    # Determine if the mask has a non-rotated cube shape
    idx = np.where(mask == 1)
    zmin, zmax = np.min(idx[0]), np.max(idx[0])
    ymin, ymax = np.min(idx[1]), np.max(idx[1])
    xmin, xmax = np.min(idx[2]), np.max(idx[2])
    if ((1+zmax)-zmin)*((1+ymax)-ymin)*((1+xmax)-xmin) == len(idx[0]):
        print("mask has a non-rotated cube shape, will interpolate exactly as much voxels as within mask reach\ngiven current subvol shape and stride settings")
        return True, (((1+zmax)-zmin,(1+ymax)-ymin,(1+xmax)-xmin),(zmin,ymin,xmin))
    else:
        print("mask has not a non-rotated cube shape, will interpolate some extra voxels\nto ensure all voxels within mask are interpolated given current subvol shape and stride settings")
        return False, (((1+zmax)-zmin,(1+ymax)-ymin,(1+xmax)-xmin),(zmin,ymin,xmin))

def vol_get_subvols_interval_indexes_mask(vol_shape, subvol_shape, stride_shape, mask, mask_spec):
    
    # If subvol and stride is a voxel, faster calculate
    if subvol_shape == (1,1,1) and stride_shape == (1,1,1):
        # Every voxel is a subvol (specified by interval indexes)
        idx = np.where(mask == 1)
        interval_indexes_l = [((idx[0][i],idx[0][i]+1),\
                               (idx[1][i],idx[1][i]+1),\
                               (idx[2][i],idx[2][i]+1)) for i in range(len(idx[0]))]
        return interval_indexes_l
    
    # Check if the mask has a non-rotated cube shape
    mask_is_cube, cube_mask_def = mask_spec
    
    if mask_is_cube:
        mask_shape, mask_offset = cube_mask_def
        print(mask_shape)
        # Going to calculate subvols for the cube-shaped mask only
        zdim, ydim, xdim = mask_shape
        ztrans, ytrans, xtrans = mask_offset
    else:
        # Going to calculate subvols for the entire (non-masked) volume,
        # then returning subvols that only completely fit within the 
        # non-cube shaped mask.
        # Orig. volume shape
        zdim, ydim, xdim = vol_shape
    
    #
    stride_z, stride_y, stride_x = stride_shape
    
    # Subvol shape
    vol_dim_z, vol_dim_y, vol_dim_x = subvol_shape
    
    # Calculate the number of non-overlapping volumes along each dimension
    num_vols_z, num_vols_y, num_vols_x = zdim//stride_z, ydim//stride_y, xdim//stride_x
    
    # 
    extra_vol_dim_z, extra_vol_dim_y, extra_vol_dim_x = zdim % stride_z, ydim % stride_y, xdim % stride_x
    
    # A list of tuples containing interval indexes along each dimension
    interval_indexes_l = []
    
    #
    #interval_index_extra_t = ()
    
    for vol_num_z in range(num_vols_z):
        for vol_num_y in range(num_vols_y):
            for vol_num_x in range(num_vols_x):
                # -- Stride version --
                """
                # Edge cases
                # If extra vol dims
                # 1. z, <=y, <=x
                if extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    #print("her!")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 2. <=z, y, <=x
                
                if extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    #print("hør")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                
                # 3. <=z, <=y, x
                elif extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    #print("hir")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 4. z, y, <=x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    #print("h")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 5. <=z, y, x
                elif extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim:
                    #print("e")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 6. z, <=y, x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    #print("o")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 7. z, y, x
                elif extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and extra_vol_dim_x and vol_num_x == num_vols_x - 1:
                    #print("r")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z+extra_vol_dim_z-vol_dim_z, \
                                    vol_num_z*stride_z+extra_vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+extra_vol_dim_x-vol_dim_x, \
                                    vol_num_x*stride_x+extra_vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # If not extra vol dims
                # 1. z, <=y, <=x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 2. <=z, y, <=x
                elif not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 3. <=z, <=y, x
                elif not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 4. z, y, <=x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and vol_num_x*stride_x+vol_dim_x <= xdim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 5. <=z, y, x
                elif not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_z*stride_z+vol_dim_z <= zdim:
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 6. z, <=y, x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1 \
                and vol_num_y*stride_y+vol_dim_y <= ydim:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                # 7. z, y, x
                elif not extra_vol_dim_z and vol_num_z == num_vols_z - 1 \
                and not extra_vol_dim_y and vol_num_y == num_vols_y - 1 \
                and not extra_vol_dim_x and vol_num_x == num_vols_x - 1:
                    z_interval_t = (zdim-vol_dim_z, \
                                    zdim)
                    y_interval_t = (ydim-vol_dim_y, \
                                    ydim)
                    x_interval_t = (xdim-vol_dim_x, \
                                    xdim)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                """
                """
                # 2. <=z, y, <=x
                if vol_num_z*stride_z+vol_dim_z <= zdim \
                and extra_vol_dim_y \
                and vol_num_x*stride_x+vol_dim_x <= xdim \
                and vol_num_z < num_vols_z - 1 \
                and vol_num_y == num_vols_y - 1 \
                and vol_num_x < num_vols_x - 1:
                    #print("hør")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y+extra_vol_dim_y-vol_dim_y, \
                                    vol_num_y*stride_y+extra_vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                """
                # Most cases
                # Check that volume is within the index dimensions
                if vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x <= xdim:# \
                #and vol_num_z < num_vols_z - 1 \
                #and vol_num_y < num_vols_y - 1 \
                #and vol_num_x < num_vols_x - 1:
                    #print(vol_num_x)
                    # Create slice intervals, defining location of subvolume
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x, \
                                    vol_num_x*stride_x+vol_dim_x)
                    # Append volume slice to list
                    interval_index_t = (z_interval_t, y_interval_t, x_interval_t)
                """
                # 3. <=z, <=y, x
                if vol_num_z*stride_z+vol_dim_z <= zdim \
                and vol_num_y*stride_y+vol_dim_y <= ydim \
                and vol_num_x*stride_x+vol_dim_x < xdim \
                and extra_vol_dim_x \
                and vol_num_z < num_vols_z - 1 \
                and vol_num_y < num_vols_y - 1 \
                and vol_num_x == num_vols_x - 1:
                    print("hir")
                    #sys.exit()
                    z_interval_t = (vol_num_z*stride_z, \
                                    vol_num_z*stride_z+vol_dim_z)
                    y_interval_t = (vol_num_y*stride_y, \
                                    vol_num_y*stride_y+vol_dim_y)
                    x_interval_t = (vol_num_x*stride_x+vol_dim_x-(vol_dim_x-extra_vol_dim_x)-1, \
                                    vol_num_x*stride_x+vol_dim_x+extra_vol_dim_x-1)
                    print(x_interval_t)
                    # Append volume slice to list
                    interval_index_extra_t = (z_interval_t, y_interval_t, x_interval_t)
                """
                if interval_index_t and mask_is_cube:
                    # Add subvol index with applied translation,
                    # thus placing the mask subvol at correct location
                    # in original space.
                    interval_indexes_l += [((ztrans+interval_index_t[0][0],ztrans+interval_index_t[0][1]),\
                                            (ytrans+interval_index_t[1][0],ytrans+interval_index_t[1][1]),
                                            (xtrans+interval_index_t[2][0],xtrans+interval_index_t[2][1]))]
                    """
                    if interval_index_extra_t:
                        # Add subvol index with applied translation,
                        # thus placing the mask subvol at correct location
                        # in original space.
                        interval_indexes_l += [((ztrans+interval_index_extra_t[0][0],ztrans+interval_index_extra_t[0][1]),\
                                                (ytrans+interval_index_extra_t[1][0],ytrans+interval_index_extra_t[1][1]),
                                                (xtrans+interval_index_extra_t[2][0],xtrans+interval_index_extra_t[2][1]))]
                    """
                
                elif interval_index_t and not mask_is_cube and \
                    np.any(mask[interval_index_t[0][0]:interval_index_t[0][1], \
                                interval_index_t[1][0]:interval_index_t[1][1], \
                                interval_index_t[2][0]:interval_index_t[2][1]]):
                    # If mask is not a cube, include subvol if it fits completely within the mask
                    # TODO: Method for translating and filling in partially matching vols
                    interval_indexes_l += [interval_index_t]
                    """
                    if interval_index_extra_t and \
                        np.any(mask[interval_index_extra_t[0][0]:interval_index_extra_t[0][1], \
                                    interval_index_extra_t[1][0]:interval_index_extra_t[1][1], \
                                    interval_index_extra_t[2][0]:interval_index_extra_t[2][1]]):
                        # If mask is not a cube, include subvol if it fits completely within the mask
                        # TODO: Method for translating and filling in partially matching vols
                        interval_indexes_l += [interval_index_extra_t]
                    """
    
    return interval_indexes_l

def vol_get_subvols_interval_indexes_mid_testing(vol_shape, subvol_shape, stride_shape):
    
    # Does not work with stride; non-stride version
    # Orig volume shape
    zdim, ydim, xdim = vol_shape
    
    #
    stride_z, stride_y, stride_x = stride_shape
    
    # Subvol shape
    vol_dim_z, vol_dim_y, vol_dim_x = subvol_shape
    
    # Calculate the number of non-overlapping volumes along each dimension
    #num_vols_z, num_vols_y, num_vols_x = zdim//stride_z, ydim//stride_y, xdim//stride_x
    num_vols_z, num_vols_y, num_vols_x = 10, 10, 10
    #num_vols_z, num_vols_y, num_vols_x = 3, 3, 3
    
    # A list of tuples containing interval indexes along each dimension
    interval_indexes_l = []
    
    for vol_num_z in range(num_vols_z):
        for vol_num_y in range(num_vols_y):
            for vol_num_x in range(num_vols_x):
                # Non-stride centered version
                z_interval_t = ((zdim//2)-vol_dim_z*(num_vols_z//2) + vol_dim_z*vol_num_z, \
                                (zdim//2)-vol_dim_z*(num_vols_z//2) + vol_dim_z*(vol_num_z+1)) # The patient y axis
                
                y_interval_t = ((ydim//2)-vol_dim_y*(num_vols_y//2) + vol_dim_y*vol_num_y, \
                                (ydim//2)-vol_dim_y*(num_vols_y//2) + vol_dim_y*(vol_num_y+1)) # The patient z axis
                
                x_interval_t = ((xdim//2)-vol_dim_x*(num_vols_x//2) + vol_dim_x*vol_num_x, \
                                (xdim//2)-vol_dim_x*(num_vols_x//2) + vol_dim_x*(vol_num_x+1)) # The patient x axis

                interval_indexes_l += [(z_interval_t, y_interval_t, x_interval_t)]
    
    print("number of subvols to process: " + str(len(interval_indexes_l)))
    
    return interval_indexes_l

def save_nifti(data, f, affine, header):
    img = nib.spatialimages.SpatialImage(data, affine=affine, header=header)
    img.set_data_dtype(np.float32)
    nib.save(img, f)

def merge_update_to_disk(save_dir, \
                         subvol_mem_index_buffer, \
                         subvol_mem_data_buffer, \
                         nifti_affine, \
                         nifti_header, \
                         current_process, \
                         current_process_name, \
                         tot_vol_shape, \
                         using_mask, \
                         mask_is_cube, \
                         mask):
    # This function takes time and is a bottleneck when called frequently
    # Merge update files sequentially to avoid loading all volumes 
    # into in working menory at the same time
        
    for time_point in range(subvol_mem_data_buffer.shape[1]):
        #print("write %s: for time point %i, reading previously saved voxels from %s" % \
        #     (current_process_name, time_point+1, save_dir + "/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npz"))
        
        # Load the existing voxel data for time_point
        stitched_data_time_point = np.load(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npz")["arr_0"]
        
        # Only look at the subvols from the current time point
        subvols_data_buffer_time_point = subvol_mem_data_buffer[:, time_point]
                
        # Iterate through the subvols for the given time_point
        for buffer_index, subvol_data in enumerate(subvols_data_buffer_time_point):
            
            # Edge case: Make sure nan data is not added.
            # nan data may happen when an interpolate process nears the end of the processing chunk when the last shared memory data is not
            # completely filled with new values and where incomplete places are set to np.nan by purpose
            # in the inteprolation processes for them to be discarded.
            if not np.all(np.isnan(subvol_data)):
                
                #print("write %s: for time point %i, going over subvol %i" % (current_process_name, time_point+1, buffer_index+1))
                
                # Get slice indices
                slice_indices = subvol_mem_index_buffer[buffer_index]
                z_interval_t, \
                y_interval_t, \
                x_interval_t = \
                slice_indices[0], slice_indices[1], slice_indices[2]
                
                # Version that takes the min - deterministic
                # an attempt to avoid being sensitive to interpolation overshooting
                # as of Gibbs phenomenon.
                
                # Get the existing subvol data previously stored
                subvol_existing_data = stitched_data_time_point[z_interval_t[0]:z_interval_t[1], \
                                                                y_interval_t[0]:y_interval_t[1], \
                                                                x_interval_t[0]:x_interval_t[1]]
                
                # Make a new subvol data that is going to contain the mean 
                # of existing and new subvol data if non nan value at 
                # a voxel location in existing subvol
                new_subvol_data = subvol_data
                
                # For voxels that are not nan in existing subvol data, 
                # take the mean of existing and new voxels 
                # and store the means in new subvol data
                new_subvol_data[~np.isnan(subvol_existing_data)] = \
                np.min((subvol_existing_data[~np.isnan(subvol_existing_data)], \
                        subvol_data[~np.isnan(subvol_existing_data)]), axis=0)
                
                # Update the stitched_data with the new subvol data, which contains
                # 1. means of existing and new voxels if existing voxel was not nan
                # 2. new voxels if exisiting voxel was nan
                stitched_data_time_point[z_interval_t[0]:z_interval_t[1], \
                                         y_interval_t[0]:y_interval_t[1], \
                                         x_interval_t[0]:x_interval_t[1]] = new_subvol_data
        if time_point == 0:
            #print("write %s: percent complete:" % current_process_name, end=" ")
            #print("{0:.2f}".format(100*np.sum(~np.isnan(stitched_data_time_point))/np.prod(tot_vol_shape[1:])))
            # Set up tiny progressbar
            progbw = subvol_mem_data_buffer.shape[1]*3
            sys.stdout.write("write %s: writing volumes for time point [%s]" % (current_process_name, (" " * progbw)))
            sys.stdout.flush()
            sys.stdout.write("\b" * (progbw+1)) # return to start of line, after '['
        if time_point < subvol_mem_data_buffer.shape[1]:
            # Write to progress bar
            sys.stdout.write(" " + str(time_point+1) + " ")
            sys.stdout.flush()
        if time_point == subvol_mem_data_buffer.shape[1]-1:
            # End the progress bar
            sys.stdout.write("]\n")
        #print("write %s: saving updated raw and NIFTI1 to disk" % current_process_name)
        # Save raw voxel data (overwriting)
        np.savez(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npz", stitched_data_time_point)
        # Save NIFTI1 file of the current stitched data (overwriting)
        if using_mask and not mask_is_cube:
            # use the mask to not save excess sobvol voxels outside of mask
            save_nifti(stitched_data_time_point*mask, save_dir + "/nii/" + "{0:03d}".format(time_point+1) + ".nii", nifti_affine, nifti_header)
        else:
            #save_nifti(stitched_data_time_point, save_dir + "/" + "{0:03d}".format(time_point+1) + ".nii.gz", nifti_affine, nifti_header)
            save_nifti(stitched_data_time_point, save_dir + "/nii/" + "{0:03d}".format(time_point+1) + ".nii", nifti_affine, nifti_header)

def stitch_subvols_from_shared_mem_and_save(results_shared_mem_index_tensors_q, \
                                       results_shared_mem_data_tensors_q, \
                                       results_shared_mem_index_tensors_event, \
                                       results_shared_mem_data_tensors_event, \
                                       subvol_shape, \
                                       tot_vol_shape, \
                                       tot_num_subvols, \
                                       save_dir, \
                                       nifti_header, \
                                       nifti_affine, \
                                       subvols_mem_buffer_size, \
                                       using_mask, \
                                       mask_is_cube, \
                                       mask):
    #print("YES I STARTED!!!! HERE!!")
    #sys.stdout.flush()
    #
    current_process = mp.current_process()
    current_process_name = current_process.name
    
    #print("her")
    #sys.stdout.flush()
    
    # Create save directories if they don't exist
    pathlib.Path(save_dir + "/raw").mkdir(parents=True, exist_ok=True)
    pathlib.Path(save_dir + "/nii").mkdir(parents=False, exist_ok=True)
    
    # Create empty numpy array that is going to contain the stitched data
    stitched_data = np.empty(tot_vol_shape, dtype=np.float32)
    stitched_data.fill(np.nan)
    # Save the first version of the stitched data, individual files for each time point
    [np.savez(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npz", data) \
        for time_point, data in enumerate(stitched_data)]
    # Delete the data from memory
    del stitched_data
    #print("og her")
    sys.stdout.flush()
    #
    num_subvols_processed = 0
    #print("hero")
    #sys.stdout.flush()
    while True:
        #if not results_shared_mem_data_tensors_q.empty() \
        #and not results_shared_mem_index_tensors_q.empty():
        if True:
            # Get the result tensors in shared memory
            #print("sdjfsdkljfnk.sjn")
            #sys.stdout.flush()
            #"""
            subvol_mem_index_shared_buffer_tensor = \
                    results_shared_mem_index_tensors_q.get()
            #print("what is this shit")
            #sys.stdout.flush()
            #results_shared_mem_index_tensors_event.set()
            #print(subvol_mem_index_shared_buffer_tensor.shape)
            #sys.stdout.flush()
            subvol_mem_data_shared_buffer_tensor = \
                    results_shared_mem_data_tensors_q.get()
            results_shared_mem_data_tensors_event.set()
            #print(subvol_mem_data_shared_buffer_tensor.shape)
            #"""
            """
            subvol_mem_data_shared_buffer_tensor = \
                    results_shared_mem_data_tensors_q.get()
            subvol_mem_index_shared_buffer_tensor = \
                    results_shared_mem_index_tensors_q.get()
            """
            #print("hdhdhdhdhdh")
            #print(subvol_mem_index_shared_buffer_tensor)
            #sys.stdout.flush()
            if subvol_mem_index_shared_buffer_tensor.dim() == 1 \
            and subvol_mem_data_shared_buffer_tensor.dim() == 1:
                # Finished
                #del subvol_mem_index_shared_buffer_tensor
                #del subvol_mem_data_shared_buffer_tensor
                break
            else:
                
                #print("write %s: got shared memory tensors containing result data" % current_process_name)
                #sys.stdout.flush()
                """
                # Read the data from the shared buffers as numpy arrays
                # using the names that were received from the queue
                subvol_mem_index_shared_buffer = \
                    shared_memory.SharedMemory(name=subvol_mem_index_shared_buffer_name)
                subvol_mem_data_shared_buffer = \
                    shared_memory.SharedMemory(name=subvol_mem_data_shared_buffer_name)
                
                # View the shared memory as numpy arrays
                subvol_mem_index_buffer_numpy = \
                    np.ndarray((subvols_mem_buffer_size, 3, 2), \
                               dtype=np.int32, \
                               buffer=subvol_mem_index_shared_buffer.buf)
                
                subvol_mem_data_buffer_numpy = \
                    np.ndarray((subvols_mem_buffer_size, tot_vol_shape[0])+subvol_shape, \
                               dtype=np.float32, \
                               buffer=subvol_mem_data_shared_buffer.buf)
                               
                """
                # Pass the data over to CPU
                subvol_mem_index_buffer_numpy = \
                    subvol_mem_index_shared_buffer_tensor.cpu().numpy()
                subvol_mem_data_buffer_numpy = \
                    subvol_mem_data_shared_buffer_tensor.cpu().numpy()
                    
                #del subvol_mem_index_shared_buffer_tensor
                #del subvol_mem_data_shared_buffer_tensor
                
                # TODO: Slows down?
                num_subvols_received = np.sum([1 for v in subvol_mem_data_buffer_numpy[:, 0] if not np.all(np.isnan(v))])
                
                #print("write %s: number of subvols in shared memory: %i" % (current_process_name, num_subvols_received))
                
                #print("write %s: starting to write shared memory blocks to disk" % current_process_name)
                #sys.stdout.flush()
                
                # Merge buffered subvols with saved subvols from disk
                # and save (overwrite) raw and NIFTI1 files to disk.
                # This takes some time
                merge_update_to_disk(save_dir, \
                                     subvol_mem_index_buffer_numpy, \
                                     subvol_mem_data_buffer_numpy, \
                                     nifti_affine, \
                                     nifti_header, \
                                     current_process, \
                                     current_process_name, \
                                     tot_vol_shape, \
                                     using_mask, \
                                     mask_is_cube, \
                                     mask)
                                     
                num_subvols_processed += num_subvols_received
                
                print("write %s: total number of subvols processed: %i" % (current_process_name, num_subvols_processed))
                
                print("write %s: percent complete:" % current_process_name, end=" ")
                print("{0:.2f}".format(100*(num_subvols_processed/tot_num_subvols)))
                
                sys.stdout.flush()
                
                # Close shared memory blocks, indicating that
                # this process will not use this 
                # shared memory instance any more
                #subvol_mem_index_shared_buffer.close()
                #subvol_mem_data_shared_buffer.close()

def interpolate_subvol(z_interval_t, \
                       y_interval_t, \
                       x_interval_t):
    
    current_process = mp.current_process()
    current_process_name = current_process.name
    
    # Perform the actual interpolation
    #print("interpolate %s: received intervals for interpolating subvol" % current_process_name)
    #print([z_interval_t, y_interval_t, x_interval_t])
    #sys.stdout.flush()
    
    data_interpolated_tensor = \
    interpolate_volume_elements_to_linear_time_gpu(interpolate_subvol.multiquadric_kernel, \
                                               interpolate_subvol.full_volumes_data_arr, \
                                               interpolate_subvol.intervals_between_volumes_t, \
                                               z_interval_t, \
                                               y_interval_t, \
                                               x_interval_t)

    
    #print("interpolate %s: still here?" % current_process_name)
    #sys.stdout.flush()

    #print([z_interval_t, y_interval_t, x_interval_t])
    # Create torch tensor of the subvol slice indices
    index_array_tensor = torch.tensor([z_interval_t, y_interval_t, x_interval_t], dtype=torch.int16, device=torch.device('cuda'))
    #print(index_array_tensor)
    
    # Store the result data in local subvol buffers
    #print("interpolate %s: putting interpolated data into memory buffer" % current_process_name)
    interpolate_subvol.subvol_mem_index_buffer_tensor[interpolate_subvol.num_subvols_buffered] = index_array_tensor
    #print(interpolate_subvol.subvol_mem_index_buffer_tensor[interpolate_subvol.num_subvols_buffered])
    interpolate_subvol.subvol_mem_data_buffer_tensor[interpolate_subvol.num_subvols_buffered] = data_interpolated_tensor
    
    #del index_array_tensor
    #del data_interpolated_tensor
    
    # Increment the subvol buffer counter
    interpolate_subvol.num_subvols_buffered += 1
    
    #print("interpolate %s: buffering interpolated subvols in memory; %i/%i" % \
    #    (current_process_name, interpolate_subvol.num_subvols_buffered, interpolate_subvol.subvols_mem_buffer_size))
    #sys.stdout.flush()
    #"""
    # Save last interpolated volumes that do not 100 percent fill shared memory
    if (interpolate_subvol.num_shared_mem_completed == (interpolate_subvol.tot_num_subvols//interpolate_subvol.subvols_mem_buffer_size)//(interpolate_subvol.num_workers) and \
        interpolate_subvol.num_subvols_buffered == interpolate_subvol.tot_num_subvols % interpolate_subvol.subvols_mem_buffer_size) or \
        (interpolate_subvol.num_shared_mem_completed > (interpolate_subvol.tot_num_subvols//interpolate_subvol.subvols_mem_buffer_size)//(interpolate_subvol.num_workers)):
        #print("BAM!")
        #sys.stdout.flush()
        interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full = True
    #"""
    #print("sdsds")
    #sys.stdout.flush()
    # Only share result data as completely filled shared memory, except when the last potential incomplete
    # shared mempory results from last subvol interpolated and a special "finished" message is passed as argument

    if interpolate_subvol.num_subvols_buffered == interpolate_subvol.subvols_mem_buffer_size or \
        interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full:
        
        # Reset buffer of shared objects if treshold exceeded
        if interpolate_subvol.num_shared_mem_buffered == interpolate_subvol.shared_mem_buffer_size:
            # shared memory objects buffer exceeded, will overwrite old shared memory objects
            # hope that the computer managed to write the data to disk in time
            # Reset the shared memory objects buffer counter
            print("interpolate %s: warning: shared memory buffer full. Assuming data was written to disk in time, resetting buffer counter" % current_process_name)
            sys.stdout.flush()
            interpolate_subvol.num_shared_mem_buffered = 0
            
        #print("interpolate %s: ok here? % current_process_name")
        #sys.stdout.flush()
        # Edge case: all subvols have been processed and we need to fill an incomplete shared memory.
        # The incomplete / old parts of the shared memory data is set to np.nan for being discarded 
        # from inclusion in the stitching process
        if interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full:
            #print("interpolate %s: warning: non-full new shared memory, setting shared old data to np.nan to avoid being saved (again)\nIf RAM allows it, set subvols_mem_buffer_size to 'Auto' for optimal performance\nOtherwise, lower subvols_mem_buffer_size for lower RAM usage\n(slower, but better than receiving a lot of\nthis message when using a large subvols_mem_buffer_size)" % current_process_name)
            print("interpolate %s: warning: non-full new shared memory, setting shared old data to np.nan to avoid being saved (again)" % current_process_name)
            sys.stdout.flush()
            #interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_subvols_buffered:] = np.nan # np.nan will work on the gpu
            interpolate_subvol.subvol_mem_data_buffer_tensor[interpolate_subvol.num_subvols_buffered:] = np.nan # np.nan will work on the gpu

     # Add the buffer tensors to a shared buffer of tensors
        #print("interpolate %s: time to do something" % current_process_name)
        #sys.stdout.flush()
        interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
            interpolate_subvol.subvol_mem_index_buffer_tensor
        interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
            interpolate_subvol.subvol_mem_data_buffer_tensor
            
        #del interpolate_subvol.subvol_mem_index_buffer_tensor
        #del interpolate_subvol.subvol_mem_data_buffer_tensor

        # Put the shared memory to the queue for letting the image stitching process
        # access the shared memory
        #print("interpolate %s: putting shared interpolated data into queue" % current_process_name)
        #sys.stdout.flush()
        #interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
        #interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered].pin_memory()
        #interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
        #interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered].share_memory_()
        #print(interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered])
        #sys.stdout.flush()
        
        #print(interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered].is_shared())
        
        interpolate_subvol.results_shared_mem_index_tensors_q.put(interpolate_subvol.shared_mem_buffer_index_buffer_tensors[interpolate_subvol.num_shared_mem_buffered])
        
        #print("interpolate %s: and waiting" % current_process_name)
        #sys.stdout.flush()
        
        #interpolate_subvol.results_shared_mem_index_tensors_event.wait()
        
        # access the shared memory
        #print("interpolate %s: putting shared interpolated data into queue again" % current_process_name)
        #sys.stdout.flush()
        
        #print(interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered])
        
        #interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
        #interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered].pin_memory()
        #interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered] = \
        #interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered].share_memory_()
        interpolate_subvol.results_shared_mem_data_tensors_q.put(interpolate_subvol.shared_mem_buffer_data_buffer_tensors[interpolate_subvol.num_shared_mem_buffered])
        
        
        
        #print("interpolate %s: and waiting again" % current_process_name)
        #sys.stdout.flush()
        
        interpolate_subvol.results_shared_mem_data_tensors_event.wait()
        
        #interpolate_subvol.results_shared_mem_index_tensors_q.wait()
        #interpolate_subvol.results_shared_mem_data_tensors_q.wait()
        
        # Increment the shared memory objects buffer counters
        interpolate_subvol.num_shared_mem_buffered += 1
        
        interpolate_subvol.num_shared_mem_completed += 1
        
        # Reset the subvol buffer counter
        interpolate_subvol.num_subvols_buffered = 0

        if interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full:
            # Resetting the incomplete buffer flag
            interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full = False

def interpolate_subvol_init(volumes_data_arr, \
                            intervals_between_volumes_t, \
                            subvol_shape, \
                            tot_vol_shape, \
                            subvols_mem_buffer_size, \
                            shared_mem_buffer_size, \
                            tot_num_subvols, \
                            num_workers, \
                            results_shared_mem_index_tensors_q, \
                            results_shared_mem_data_tensors_q, \
                            results_shared_mem_index_tensors_event, \
                            results_shared_mem_data_tensors_event):
    #mp.set_start_method('spawn')
    #import torch
    import os.path
    import sys
    sys.path.append('/home/ivar/Downloads/keops') # For enabling import of pykeops
    from pykeops.torch import LazyTensor # Cuda init must happen after process spawn
    #torch.cuda.empty_cache()
                                
    # The queues
    interpolate_subvol.results_shared_mem_index_tensors_q = results_shared_mem_index_tensors_q
    interpolate_subvol.results_shared_mem_data_tensors_q = results_shared_mem_data_tensors_q
    
    # Events for synchronization of queues
    interpolate_subvol.results_shared_mem_index_tensors_event = results_shared_mem_index_tensors_event
    interpolate_subvol.results_shared_mem_data_tensors_event = results_shared_mem_data_tensors_event
    
    # A copy of the original volumes
    interpolate_subvol.full_volumes_data_arr = volumes_data_arr
    
    # 
    interpolate_subvol.intervals_between_volumes_t = intervals_between_volumes_t
    
    # The buffer size specified
    interpolate_subvol.subvols_mem_buffer_size = subvols_mem_buffer_size
    
    # A buffer counter that is used to fill the memory buffer
    interpolate_subvol.num_subvols_buffered = 0
    
    """
    # non-shared memory buffers for storing the result data, before copying into shared memory
    interpolate_subvol.subvol_mem_index_buffer = \
        np.empty((subvols_mem_buffer_size, 3, 2), dtype=np.int32)
    
    interpolate_subvol.subvol_mem_data_buffer = \
        np.empty((subvols_mem_buffer_size, tot_vol_shape[0])+subvol_shape, dtype=np.float32)
    """

    interpolate_subvol.subvol_mem_index_buffer_tensor = \
        torch.empty((subvols_mem_buffer_size, 3, 2), dtype=torch.int16, device=torch.device('cuda'))
    
    interpolate_subvol.subvol_mem_data_buffer_tensor = \
        torch.empty((subvols_mem_buffer_size, tot_vol_shape[0])+subvol_shape, dtype=torch.float32, device=torch.device('cuda'))
    
        
    # Buffer for storing shared memory objects so that it can be accessed later
    interpolate_subvol.shared_mem_buffer_size = shared_mem_buffer_size
    
    interpolate_subvol.shared_mem_buffer_index_buffer_tensors = \
        torch.empty((shared_mem_buffer_size,)+interpolate_subvol.subvol_mem_index_buffer_tensor.shape, \
        dtype=torch.int16, device=torch.device('cuda'))
        
    interpolate_subvol.shared_mem_buffer_data_buffer_tensors = \
        torch.empty((shared_mem_buffer_size,)+interpolate_subvol.subvol_mem_data_buffer_tensor.shape, \
        dtype=torch.float32, device=torch.device('cuda'))
    
    interpolate_subvol.num_shared_mem_buffered = 0
        
    interpolate_subvol.num_shared_mem_completed = 0
    
    interpolate_subvol.last_shared_mem_buffer_and_will_be_non_full = False
    
    interpolate_subvol.tot_num_subvols = tot_num_subvols
    
    interpolate_subvol.num_workers = num_workers
    
    def calculate_default_epsilon_rbf_multiquadric_4D(x):
        
        # https://github.com/scipy/scipy/blob/v1.4.1/scipy/interpolate/rbf.py#L59-L290
        # default epsilon is the "the average distance between nodes" based
        # on a bounding hypercube
        
        xi = np.asarray([np.asarray(a, dtype=np.float_).flatten()
                                      for a in (x[:,0], x[:,1], x[:,2], x[:,3])])
        N = xi.shape[-1]
        ximax = np.amax(xi, axis=1)
        ximin = np.amin(xi, axis=1)
        edges = ximax - ximin
        edges = edges[np.nonzero(edges)]
        return np.power(np.prod(edges)/N, 1.0/edges.size)
    
    def multiquadric_kernel(x, y, epsilon="Default"):
        
        #For use in pykeops
        
        if epsilon=="Default":
            epsilon = \
            calculate_default_epsilon_rbf_multiquadric_4D(x.cpu().numpy())
        multiquadric_kernel.epsilon = \
        torch.tensor(epsilon, dtype=torch.float32, device=torch.device('cuda'))
        
        x_i = LazyTensor(x[:, None, :])  # (M, 1, :)
        y_j = LazyTensor(y[None, :, :])  # (1, N, :)
        D_ij = ((x_i - y_j) ** 2).sum(-1)  # (M, N) symbolic matrix of squared distances
        return ((1/multiquadric_kernel.epsilon * D_ij) ** 2 + 1).sqrt()
        
    interpolate_subvol.multiquadric_kernel = multiquadric_kernel
    

if __name__ == "__main__":
    
    # Required for using CUDA in subprocesses. 
    # By default, "fork" will not work.
    #mp.set_start_method('spawn')
    
    # Define command line options
    # this also generates --help and error handling
    CLI=argparse.ArgumentParser()
    CLI.add_argument(
      "--nifti",  # name on the CLI - drop the `--` for positional/required parameters
      nargs="*",  # 0 or more values expected => creates a list
      help="Takes in the relative file path + \
      file name of two or more co-registrated NIFTI1 files, \
      separated by space",
      type=str,
      default=["1.nii", "2.nii"],
    )
    CLI.add_argument(
      "--timeint",
      nargs="*",
      help="The time interval between the NIFTI1 file scans, in some time unit, f. ex. days, separated by space",
      type=int,
      default=[7],
    )
    CLI.add_argument(
      "--mask",
      help="A binary mask of the ROI co-regitered to the first .nii file passed in as argument after --nifti . When used, will only interpolate subvols in the true (1) regions in the binary mask",
      type=str,
      default="none",
    )
    CLI.add_argument(
      "--savedir",
      help="The directory to save interpolated NIFTI files and raw voxel data",
      type=str,
      default="interpolated",
    )
    # Parse the command line
    args = CLI.parse_args()
 
    # Set the shape of each subvolume that is unterpolated over time
    # 20, 20, 20 was the maximum shape on a 32 GB RAM machine before memory error
    
    # Configuration consists of three parts; only Part 1 affects actual imaging results.
    
    # Part 1: Size of a common sub volume across all volumes
    # that we want to interpolate over time. I think larger size should better
    # cope with rapid spatial changes. However, this will slow down the interpolation. 
    # The default settings are good enough for capturing typical tumor growth (?)
    # Default:
    # NOTE, TODO: 2020-01-15: When using --mask , the script currently only works 
    # correctly with default subvol_shape and subvol_stride
    # see vol_get_subvols_interval_indexes_mask
    subvol_shape = (3, 3, 3)
    subvol_stride = (2, 2, 2)
    #subvol_stride = (3, 3, 3)
    
    # Heavy on a machine with 8GB RAM
    #subvol_shape = (12, 12, 12)
    #subvol_stride = (8, 8, 8)
    
    # when using non-cube shaped ROIs
    #subvol_shape = (1, 1, 1)
    #subvol_stride = (1, 1, 1)

    #subvol_shape = (6, 6, 6)
    #subvol_stride = (4, 4, 4)
    
    # Part 2: Parameters that may need to be changed depending on the computer.
    
    # The two parameters adjust how often 
    # intermediate results are written to disk
    
    # Should be as large as the RAM allows. The same as chunksize in the multiprocessing starmap function
    # Larger subvols_mem_buffer_size decreases the frequency of writing to disk
    # and increases the size of the data chunks written to disk.
    # For full volumes
    #subvols_mem_buffer_size = 90000
    #subvols_mem_buffer_size = 60000
    # For roi volumes
    subvols_mem_buffer_size = 100
    #subvols_mem_buffer_size = 10
    # Automatic modes: comment out subvols_mem_buffer_size below
    #subvols_mem_buffer_size = "AutoChunksize"
    #subvols_mem_buffer_size = "AutoChunksize2"
    #subvols_mem_buffer_size = "AutoTotNum"
    
    # Should be large if you have a slow disk, but then you need a lot of RAM,
    # especially if you have many CPU cores and allow maximum CPU utilization;
    # mp.cpu_count().
    # If this value is too low combined with a fast CPU,
    # then the program may hang beceause the write to disk
    # process did not access a shared memory block in time before it was overwitten by
    # a newer shared memory block in an interpolate process. 
    # This means that the writer process will wait for a resource 
    # that was made inaccessable by an interpolate process and
    # will result in a deadlock.
    # A lower shared_mem_buffer_size increases the risk of deadlock.
    # It should not be too large, since write to disk takes time.
    # For full volumes
    #shared_mem_buffer_size = 300
    # For roi volumes
    #shared_mem_buffer_size = 200
    #shared_mem_buffer_size = 2
    shared_mem_buffer_size = 10
    
    # num_workes needs to be > 1
    # to have concurrent worker and writer process 
    #num_workers = mp.cpu_count()-1
    
    # Part 3: The number of workers. Make a large as possible, recommended number of cpu cores (mp.cpu_count()) - 1
    # Otherwise make smaller if the program takes up too much resources.
    # Main will run in a serparate process, thus subtract 1 from mp.cpu_count()
    # to utilize exactly all available cpu cores
    #num_workers = mp.cpu_count() - 1
    num_workers = 3
        
    print("----------------------------------------------------------------")
    print("Welcome to the Radial Basis Function time interpolation routine!")
    print("                  This will run for a while                     ")
    print("----------------------------------------------------------------")
   
    # Assuming all volumes have the same voxel dimensions
    # and spatial dimensions, and are properly co-registrated.
    vols_spatialimg_t = tuple(nib.load(file) for file in args.nifti)
    
    # Resample data in volumes to the first volume using affine transforms in the nifti headers.
    vols_spatialimg_resampled_t = tuple(resample_to_img(vol_spatialimg, \
                                                        vols_spatialimg_t[0]) \
                                                        for vol_spatialimg in vols_spatialimg_t[1:])
    
    # Constrcut tuples containing the nifti object (including voxel data data) of each examination volume
    volumes_niilike_t = (vols_spatialimg_t[0],) + vols_spatialimg_resampled_t
    volumes_data_arr = np.stack(tuple(volume_niilike.get_fdata() for volume_niilike in volumes_niilike_t))
    
    # Test with random data
    #n = 5
    #volumes_data_arr = np.stack((np.random.rand(n,n,n), np.random.rand(n,n,n)))
    
    # Construct tuple containing interval (in a given time unit, f. ex. days) between each examination
    #intervals_between_volumes_t = (7,) # Note , at the end in other to make 
                                            # it iterable when it contains only one value
    intervals_between_volumes_t = tuple(args.timeint) # (Time interval)
    
    # Get the shape of the first volume for use in the interpolation configuration
    vol_shape = volumes_niilike_t[0].shape 
    
    # Pre-select all subvolumes common across time in all volumes
    # that we want to interpolate to linear time, optionally using a binary mask.
    # This comes in terms of interval start and stop indices in x, y and z directions.
    if args.mask != "none":
        using_mask = True
        mask_spatialimg = nib.load(args.mask)
        mask_spatialimg_resampled = resample_to_img(mask_spatialimg, vols_spatialimg_t[0], interpolation="nearest")
        mask_data_arr = mask_spatialimg_resampled.get_fdata()
        mask_is_cube, mask_descriptions = check_if_mask_is_a_cube(mask_data_arr)
        interval_indexes_l = vol_get_subvols_interval_indexes_mask(vol_shape, subvol_shape, subvol_stride, mask_data_arr, (mask_is_cube, mask_descriptions))
        #sys.exit()
    else:
        using_mask = False
        mask_is_cube = False
        mask_data_arr = np.array([])
        interval_indexes_l = vol_get_subvols_interval_indexes_all(vol_shape, subvol_shape, subvol_stride)
    
    #
    tot_num_subvols = len(interval_indexes_l)
    
    # Automatic option: Set in-memory subvol buffer size to be equal the starmap chunksize
    # May consume too much RAM when run with many --nifti input volumes
    if subvols_mem_buffer_size == "AutoChunksize":
        print("subvols_mem_buffer_size (chunksize) set to AutoChunksize")
        subvols_mem_buffer_size = calculate_default_chunksize(len(interval_indexes_l), num_workers)
    elif subvols_mem_buffer_size == "AutoChunksize2":
        print("subvols_mem_buffer_size (chunksize) set to AutoChunksize")
        subvols_mem_buffer_size = 1+calculate_default_chunksize(len(interval_indexes_l), num_workers)//4
    elif subvols_mem_buffer_size == "AutoTotNum":
        print("subvols_mem_buffer_size (chunksize) set to AutoTotNum")
        if tot_num_subvols % (num_workers):
            subvols_mem_buffer_size = 1+(tot_num_subvols//(num_workers))
        else:
            subvols_mem_buffer_size = tot_num_subvols//(num_workers)
    
    # Calculate chunksize using a default formula
    #chunksize = calculate_default_chunksize(len(interval_indexes_l), num_workers)
    
    print("number of subvols to process: %i" % tot_num_subvols)
    print("selected subvol memory buffer size (chunksize): %i" % subvols_mem_buffer_size)
    
    if shared_mem_buffer_size == "Auto":
        print("shared_mem_buffer_size set to Auto")
        shared_mem_buffer_size = 2+((tot_num_subvols//(num_workers))//subvols_mem_buffer_size)
    
    print("selected shared memory buffer size: %i" % shared_mem_buffer_size)
    #print("selected chunksize: %i" % chunksize)
    #"""
    # Multiprocessing manager
    #manager = mp.Manager()
        
    # Queues
    results_shared_mem_index_tensors_q = mp.SimpleQueue()
    results_shared_mem_data_tensors_q = mp.SimpleQueue()
    
    # Events for synchronization
    results_shared_mem_index_tensors_event = mp.Event()
    results_shared_mem_data_tensors_event = mp.Event()
    
    # The final shape of the total volumes interpolated over time (number of time units)
    tot_vol_shape = (np.sum(intervals_between_volumes_t),) + vol_shape

    #print(tot_vol_shape)
    #test = np.empty(tot_vol_shape, dtype=np.float32)
    
    # Initialize multiprocessing pool of num_workers workers # 
    mp_p = mp.Pool(num_workers, \
                   interpolate_subvol_init, \
                   initargs=(volumes_data_arr, \
                             intervals_between_volumes_t, \
                             subvol_shape, \
                             tot_vol_shape, \
                             subvols_mem_buffer_size, \
                             shared_mem_buffer_size, \
                             tot_num_subvols, \
                             num_workers, \
                             results_shared_mem_index_tensors_q, \
                             results_shared_mem_data_tensors_q, \
                             results_shared_mem_index_tensors_event, \
                             results_shared_mem_data_tensors_event), \
                   ) # maxtasksperchild=1

    # Start process that listens for names of shared memory 
    # on results_shared_mem_names_qand that can be used to 
    # access result data from interpolation processes.
    # Stich together the result data into
    # the complete interpolated volume series in a memory efficient manner
    """
    mp_p.apply_async(stitch_subvols_from_shared_mem_and_save, args=(results_shared_mem_index_tensors_q, \
                                                               results_shared_mem_data_tensors_q, \
                                                               results_shared_mem_index_tensors_event, \
                                                               results_shared_mem_data_tensors_event, \
                                                               subvol_shape, \
                                                               tot_vol_shape, \
                                                               tot_num_subvols, \
                                                               args.savedir, \
                                                               volumes_niilike_t[0].header, \
                                                               volumes_niilike_t[0].affine, \
                                                               subvols_mem_buffer_size, \
                                                               using_mask, \
                                                               mask_is_cube, \
                                                               mask_data_arr))
    """

    sp = mp.Process(target=stitch_subvols_from_shared_mem_and_save, args=(results_shared_mem_index_tensors_q, \
                                                               results_shared_mem_data_tensors_q, \
                                                               results_shared_mem_index_tensors_event, \
                                                               results_shared_mem_data_tensors_event, \
                                                               subvol_shape, \
                                                               tot_vol_shape, \
                                                               tot_num_subvols, \
                                                               args.savedir, \
                                                               volumes_niilike_t[0].header, \
                                                               volumes_niilike_t[0].affine, \
                                                               subvols_mem_buffer_size, \
                                                               using_mask, \
                                                               mask_is_cube, \
                                                               mask_data_arr))
    sp.daemon = True
    sp.start()
    
    #print("dfd")

    # Interpolate subvols in paralell
    mp_p.starmap(interpolate_subvol, interval_indexes_l, chunksize=subvols_mem_buffer_size)
    
    # Interpolation processes is finished, so put a finish messages to results_shared_mem_names_q in order to end
    # the writing process
    #results_shared_mem_tensors_q.put_nowait(("finished", "finished"))
    results_shared_mem_index_tensors_q.put(torch.tensor([], dtype=torch.int16, device=torch.device('cuda')))
    results_shared_mem_index_tensors_event.wait()
    results_shared_mem_data_tensors_q.put(torch.tensor([], dtype=torch.float32, device=torch.device('cuda')))
    results_shared_mem_data_tensors_event.wait()
    
    # Close the multiprocessing pool, joun for waiting for it to terminate
    mp_p.close()
    mp_p.join()
    sp.join()
    #"""
    print("----------------------------------------------------------------")   
    print("                  Finished interpolation                        ")
    print("----------------------------------------------------------------")

