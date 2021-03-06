import numpy as np
import nibabel as nib
from nilearn.image import resample_to_img
from scipy.interpolate import Rbf
import multiprocessing as mp
#import torch.multiprocessing as mp
from multiprocessing import shared_memory
#from multiprocessing import shared_memory, set_start_method
#from multiprocessing.pool import Pool
#from torch.multiprocessing import set_start_method
import argparse
import copy
import pathlib
import sys
import torch
import gc
import uuid

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
    #print(T)
    #sys.stdout.flush()
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
    
def calculate_chunksize(num_items, num_workers, num_parts):
    chunksize, extra = divmod(num_items, num_workers * num_parts)
    #if extra:
    #    chunksize += 1
    return chunksize, extra
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
def interpolate_volume_elements_to_linear_time(data, \
                                               intervals_between_volumes_t):
            
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

def interpolate_volume_elements_to_linear_time_torch(kernel_func, \
                                               data, \
                                               intervals_between_volumes_t, \
                                               device='cpu'):
    #gc.collect()
    #torch.cuda.empty_cache()
    #print("sds")
    #print(torch.cuda.memory_snapshot())
    #sys.stdout.flush()
            
    # Get the resulting dimensions of the stacked data
    # for later use in the grid defition
    tdim, zdim, ydim, xdim = data.shape
    
    # Flatten the stacked data, for use in Rbf
    data_flattened = flatten_tensor(data)
    
    
    # Get the colums in the flattened data
    # The voxel values
    b = data_flattened[:,0]
    # The rest. Voxels' spatial and temporal positions
    x = data_flattened[:,1:]
    
    # GPU    
    # Transfer to GPU    
    b = torch.tensor(b, dtype=torch.float32, device=torch.device(device)).view(-1,1)
    
    epsilon = \
    torch.from_numpy(np.array(kernel_func.calculate_default_epsilon_rbf_multiquadric_4D(x), dtype=np.float32)).to(device=torch.device(device))
    
    x = torch.tensor(x, dtype=torch.float32, device=torch.device(device))
    
    #del data
    #del data_flattened
    
    K_xx = kernel_func(x, x, epsilon=epsilon, device=device)
    
    #epsilon = kernel_func.epsilon
    
    #print("er det her den stopper?")
    #sys.stdout.flush()
    
    # Create the interoplant
    # using a conjugate gradient solver
    
    #print(type(a))
    #print(type(b))
    #print(type(K_xx))
    #sys.stdout.flush()
    #alpha = 10  # Ridge regularization
    #a = K_xx.solve(b, alpha=alpha)
    #a = K_xx.solve(b, use_Kahan =True)
    a = K_xx.solve(b)
    #print("eller her?")
    #sys.stdout.flush()
    
    # CPU & GPU
    # Make new points to resample the interpolant on
    ZI = torch.tensor(np.mgrid[0:zdim], dtype=torch.float32, device=torch.device(device))
    YI = torch.tensor(np.mgrid[0:ydim], dtype=torch.float32, device=torch.device(device))
    XI = torch.tensor(np.mgrid[0:xdim], dtype=torch.float32, device=torch.device(device))
    
    TI = torch.cat(tuple(torch.tensor(np.mgrid[time_idx:time_idx+1:1/interval_duration], dtype=torch.float32, device=torch.device(device)) \
                    for time_idx, interval_duration in enumerate(intervals_between_volumes_t)), dim=0).view(-1)
                    
    TI, ZI, YI, XI = torch.meshgrid(TI, ZI, YI, XI)
    
    grid = torch.stack((TI.contiguous().view(-1), \
                     ZI.contiguous().view(-1), \
                     YI.contiguous().view(-1), \
                     XI.contiguous().view(-1)), dim=1)
    
    # 
    K_gridx = kernel_func(grid, x, epsilon=epsilon, device=device)
    
    # The interpolation
    data_interpolated = K_gridx @ a
    
    # View the data 
    # with correct shape
    data_interpolated = data_interpolated.view(np.sum(intervals_between_volumes_t), zdim, ydim, xdim)
    
    #data_interpolated_numpy = copy.deepcopy(data_interpolated.cpu().clone().numpy().copy())
    #data_interpolated_numpy = copy.deepcopy(data_interpolated.cpu().numpy())
    #data_interpolated_numpy = data_interpolated.cpu().numpy()
    
    #del tdim, zdim, ydim, xdim
    
    #del K_gridx, a, grid, TI, ZI, YI, XI, K_xx, b, epsilon, x
    
    #gc.collect()
    
    #torch.cuda.empty_cache()
    
    #torch.cuda.ipc_collect()
    
    #torch.cuda.synchronize(0)
    
    # Return interpolated data that is copied
    # into RAM in the form of a numpy array
    return data_interpolated.cpu().numpy()

def put_data_in_shared_mem(data):
    # Create an fill shared memory with existing numpy array
    # return the name of the shared memory block
    
    # Create shared memory instance
    data_shared = \
                shared_memory.SharedMemory(create=True, \
                size=data.nbytes)
    
    # View shared memory as numpy array
    data_shared_numpy = \
                np.ndarray(data.shape, \
                dtype=data.dtype, \
                buffer=data_shared.buf)
    
    # Copy data into the shared memory
    data_shared_numpy[:] = \
                data[:]
    
    # Delete the original data
    del data
    
    # Force free memory
    gc.collect()
    
    return data_shared
#"""
def put_data_in_shared_mem_torch(data, device='cpu'):
    # Create an fill shared memory with existing numpy array
    # return the name of the shared memory block
    
    # Make torch tensor
    data_torch = torch.tensor(data, dtype=torch.float32, device=torch.device(device))
    
    # Make the torch tensor shared across processes
    data_torch.share_memory_()
    
    # Delete the original data
    del data
    
    # Force free memory
    if device == 'cuda':
        torch.cuda.empty_cache()
        
        torch.cuda.ipc_collect()
    
    gc.collect()
    
    return data_torch
#"""
def access_shared_subvols_data(interval_index_t, \
                               shared_mem_obj_name, \
                               volumes_shape):
    # Unpack intervals
    z_interval_t, y_interval_t, x_interval_t = interval_index_t
    
    # Access shared volumes memory block by name
    data_shared = \
                shared_memory.SharedMemory(name=shared_mem_obj_name)
    
    data_shared_numpy = \
                np.ndarray(volumes_shape, \
                dtype=np.float32, \
                buffer=data_shared.buf)
    
    # Return subvol data. Copy is important;
    # not copying will lead to the shared memory
    # not accessible to other processes.
    result = data_shared_numpy[:, \
                           z_interval_t[0]:z_interval_t[1], \
                           y_interval_t[0]:y_interval_t[1], \
                           x_interval_t[0]:x_interval_t[1] \
                           ].copy()
    
    # Close the shared memory object
    # signaling that this shared memory object
    # is not going to be used any longer
    data_shared.close()
    
    return result

def access_shared_subvols_data_torch(interval_index_t, \
                                     shared_mem, \
                                     device='cuda'):
    # Unpack intervals
    z_interval_t, y_interval_t, x_interval_t = interval_index_t
    
    if device == 'cpu':
        result = shared_mem[:, \
                            z_interval_t[0]:z_interval_t[1], \
                            y_interval_t[0]:y_interval_t[1], \
                            x_interval_t[0]:x_interval_t[1] \
                            ].numpy()
    elif device == 'cuda':
        result = shared_mem[:, \
                            z_interval_t[0]:z_interval_t[1], \
                            y_interval_t[0]:y_interval_t[1], \
                            x_interval_t[0]:x_interval_t[1]].cpu().numpy()
    return result

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
        stitched_data_time_point = np.load(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npy")
        
        # Only look at the subvols from the current time point
        subvols_data_buffer_time_point = subvol_mem_data_buffer[:, time_point]
        
        # TODO: gc.collect() here for efficient memory use?
                
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
        #sys.stdout.flush()
        # Save raw voxel data (overwriting)
        np.save(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npy", stitched_data_time_point)
        # Save NIFTI1 file of the current stitched data (overwriting)
        if using_mask and not mask_is_cube:
            # use the mask to not save excess sobvol voxels outside of mask
            save_nifti(stitched_data_time_point*mask, save_dir + "/nii/" + "{0:03d}".format(time_point+1) + ".nii", nifti_affine, nifti_header)
        else:
            #save_nifti(stitched_data_time_point, save_dir + "/" + "{0:03d}".format(time_point+1) + ".nii.gz", nifti_affine, nifti_header)
            save_nifti(stitched_data_time_point, save_dir + "/nii/" + "{0:03d}".format(time_point+1) + ".nii", nifti_affine, nifti_header)

def stitch_subvols_from_shared_mem_and_save(results_shared_mem_names_q, \
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
    #
    current_process = mp.current_process()
    current_process_name = current_process.name
    
    # Create save directories if they don't exist
    pathlib.Path(save_dir + "/raw").mkdir(parents=False, exist_ok=True)
    pathlib.Path(save_dir + "/nii").mkdir(parents=False, exist_ok=True)
    
    # Create empty numpy array that is going to contain the stitched data
    stitched_data = np.empty(tot_vol_shape, dtype=np.float32)
    stitched_data.fill(np.nan)
    # Save the first version of the stitched data, individual files for each time point
    [np.save(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npy", data) \
        for time_point, data in enumerate(stitched_data)]
    # Delete the data from memory
    del stitched_data
    
    gc.collect()
    
    #
    num_subvols_processed = 0
    
    while True:
        if not results_shared_mem_names_q.empty():
            # Get the names of the shared memory blocks containing subvol indices and interpolated data
            subvol_mem_index_shared_buffer_name, \
            subvol_mem_data_shared_buffer_name = \
                results_shared_mem_names_q.get_nowait()
            
            if subvol_mem_index_shared_buffer_name == "finished":
                # Finished
                break
            else:
                
                print("write %s: got names of shared memory blocks containing result data" % current_process_name)
                sys.stdout.flush()
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
                
                #print(subvol_mem_data_buffer_numpy)
                #sys.stdout.flush()
                
                # TODO: Slows down?
                num_subvols_received = np.sum([1 for v in subvol_mem_data_buffer_numpy[:, 0] if not np.all(np.isnan(v))])
                
                #print("write %s: number of subvols in shared memory: %i" % (current_process_name, num_subvols_received))
                
                print("write %s: starting to write shared memory blocks to disk" % current_process_name)
                sys.stdout.flush()
                
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
                #subvol_mem_index_shared_buffer.unlink()
                #subvol_mem_data_shared_buffer.unlink()
                
                # 
                del subvol_mem_index_buffer_numpy
                del subvol_mem_data_buffer_numpy
                
                # Force free delete arrays
                gc.collect()

def stitch_subvols_from_tmpdir_and_save(subvol_shape, \
                                       tot_vol_shape, \
                                       tot_num_subvols, \
                                       save_dir, \
                                       nifti_header, \
                                       nifti_affine, \
                                       subvols_mem_buffer_size, \
                                       using_mask, \
                                       mask_is_cube, \
                                       mask):
    #
    current_process = mp.current_process()
    current_process_name = current_process.name
    
    # Create save directories if they don't exist
    pathlib.Path(save_dir + "/raw").mkdir(parents=False, exist_ok=True)
    pathlib.Path(save_dir + "/nii").mkdir(parents=False, exist_ok=True)
    
    # Create empty numpy array that is going to contain the stitched data
    stitched_data = np.empty(tot_vol_shape, dtype=np.float32)
    stitched_data.fill(np.nan)
    # Save the first version of the stitched data, individual files for each time point
    [np.save(save_dir + "/raw/" + "{0:03d}".format(time_point+1) + "_raw_voxels.npy", data) \
        for time_point, data in enumerate(stitched_data)]
    # Delete the data from memory
    del stitched_data
    
    gc.collect()
    
    #
    num_subvols_processed = 0
    
    (_, _, idxfiles) = next(os.walk(save_dir + "/idxtmp"))
    (_, _, datafiles) = next(os.walk(save_dir + "/datatmp"))
    
    for idxfile, datafile in zip(idxfiles, datafiles):
                
        print("write %s: loading result data from disk" % current_process_name)
        sys.stdout.flush()
        # 
        subvol_mem_index_buffer_numpy = \
            np.load(save_dir + "/idxtmp/" + idxfile)
        subvol_mem_data_buffer_numpy = \
            np.load(save_dir + "/datatmp/" + datafile)
        
        # TODO: Slows down?
        num_subvols_received = np.sum([1 for v in subvol_mem_data_buffer_numpy[:, 0] if not np.all(np.isnan(v))])
        
        #print("write %s: number of subvols in shared memory: %i" % (current_process_name, num_subvols_received))
        
        print("write %s: starting to write memory blocks to disk" % current_process_name)
        sys.stdout.flush()
        
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
        #subvol_mem_index_shared_buffer.unlink()
        #subvol_mem_data_shared_buffer.unlink()
        
        # 
        #del subvol_mem_index_buffer_numpy
        #del subvol_mem_data_buffer_numpy
        
        # Force free delete arrays
        #gc.collect()

def interpolate_subvol(interval_index_t):
    
    current_process = mp.current_process()
    current_process_name = current_process.name
    
    #torch.cuda.empty_cache()
    #torch.cuda.ipc_collect()
    
    # Perform the actual interpolation
    print("interpolate %s: received intervals for interpolating subvol" % current_process_name)
    sys.stdout.flush()
    
    # 
    if interpolate_subvol.interpolate_backend == "scipy_cpu":
        # interpolate_subvol.volumes_data_shared_mem
        # is a string of the name of a shared memory object
        subvols_data = access_shared_subvols_data(interval_index_t, \
                                                  interpolate_subvol.volumes_data_shared_mem, \
                                                  interpolate_subvol.volumes_shape)
        # Perform the interpolation
        data_interpolated = \
        interpolate_volume_elements_to_linear_time(subvols_data, \
                                                   interpolate_subvol.intervals_between_volumes_t)
    elif interpolate_subvol.interpolate_backend == "pykeops_cpu":
        # interpolate_subvol.volumes_data_shared_mem
        # is a shared tensor in CPU memory
        subvols_data = access_shared_subvols_data_torch(interval_index_t, \
                                                        interpolate_subvol.volumes_data_shared_mem,\
                                                        device='cpu')
        # Perform the interpolation
        data_interpolated = \
        interpolate_volume_elements_to_linear_time_torch(interpolate_subvol.gaussian_kernel, \
                                                   subvols_data, \
                                                   interpolate_subvol.intervals_between_volumes_t, \
                                                   device='cpu')
    elif interpolate_subvol.interpolate_backend == "pykeops_cpu_gpu":
        # interpolate_subvol.volumes_data_shared_mem
        # is a shared tensor in CPU memory
        subvols_data = access_shared_subvols_data_torch(interval_index_t, \
                                                        interpolate_subvol.volumes_data_shared_mem,\
                                                        device='cpu')
        # Perform the interpolation
        data_interpolated = \
        interpolate_volume_elements_to_linear_time_torch(interpolate_subvol.gaussian_kernel, \
                                                   subvols_data, \
                                                   interpolate_subvol.intervals_between_volumes_t, \
                                                   device='cuda')
    elif interpolate_subvol.interpolate_backend == "pykeops_gpu":
        # interpolate_subvol.volumes_data_shared_mem
        # is a shared tensor in GPU memory
        subvols_data = access_shared_subvols_data_torch(interval_index_t, \
                                                        interpolate_subvol.volumes_data_shared_mem, \
                                                        device='cuda')
        # Perform the interpolation
        data_interpolated = \
        interpolate_volume_elements_to_linear_time_torch(interpolate_subvol.gaussian_kernel, \
                                                   subvols_data, \
                                                   interpolate_subvol.intervals_between_volumes_t, \
                                                   device='cuda')

    # Create numpy array of the subvol slice indices
    index_array = np.array(interval_index_t)
    
    # Store the result data in local subvol buffers
    #print("interpolate %s: putting interpolated data into memory buffer" % current_process_name)
    #sys.stdout.flush()
    interpolate_subvol.subvol_mem_index_buffer[interpolate_subvol.num_subvols_buffered] = index_array
    interpolate_subvol.subvol_mem_data_buffer[interpolate_subvol.num_subvols_buffered] = data_interpolated
    
    #del index_array
    #del data_interpolated
    
    #torch.cuda.empty_cache()
    #torch.cuda.ipc_collect()
    #torch.cuda.synchronize(0)
    
    
    #gc.collect() # slow
    
    # Increment the subvol buffer counter
    interpolate_subvol.num_subvols_buffered += 1
    
    print("interpolate %s: buffering interpolated subvols in memory; %i/%i" % \
        (current_process_name, interpolate_subvol.num_subvols_buffered, interpolate_subvol.subvols_mem_buffer_size))
    sys.stdout.flush()
    
    # Save last interpolated volumes that do not 100 percent fill shared memory
    """
    if (interpolate_subvol.num_saved_mem_blocks == (interpolate_subvol.tot_num_subvols//interpolate_subvol.subvols_mem_buffer_size)//(interpolate_subvol.num_workers) and \
        interpolate_subvol.num_subvols_buffered == interpolate_subvol.tot_num_subvols % interpolate_subvol.subvols_mem_buffer_size) or \
        (interpolate_subvol.num_saved_mem_blocks > (interpolate_subvol.tot_num_subvols//interpolate_subvol.subvols_mem_buffer_size)//(interpolate_subvol.num_workers)):
        #print("BAM!")
    """
    #"""
    if interpolate_subvol.num_extra_subvols and \
       ((interpolate_subvol.num_saved_mem_blocks + 1) // interpolate_subvol.mem_block_buffer_size) == interpolate_subvol.mem_block_buffer_size and \
       interpolate_subvol.num_subvols_buffered >= \
       (interpolate_subvol.num_extra_subvols - \
       interpolate_subvol.number_of_subvols_missing_before_no_remainder(interpolate_subvol.num_extra_subvols, interpolate_subvol.num_workers))\
       //interpolate_subvol.num_workers:
    #"""
        # 
        print("interpolate %s: warning: incomplete last memory block encountered, signaling to write to disk earlier" % current_process_name)
        sys.stdout.flush()
        interpolate_subvol.last_mem_block_and_will_be_non_full = True
    
    # Only share result data as completely filled shared memory, except when the last potential incomplete
    # shared mempory results from last subvol interpolated and a special "finished" message is passed as argument
    if interpolate_subvol.num_subvols_buffered == interpolate_subvol.subvols_mem_buffer_size or \
        interpolate_subvol.last_mem_block_and_will_be_non_full:
        
        print("interpolate %s: memory buffer full, time to save to disk for later stitching" % current_process_name)
        sys.stdout.flush()
        """
        # Reset buffer of shared objects if treshold exceeded
        if interpolate_subvol.num_buffered_mem_blocks == interpolate_subvol.mem_block_buffer_size:
            # shared memory objects buffer exceeded, will overwrite old shared memory objects
            # hope that the computer managed to write the data to disk in time
            # Reset the shared memory objects buffer counter
            print("interpolate %s: warning: shared memory object buffer full, resetting buffer counter" % current_process_name)
            sys.stdout.flush()
            interpolate_subvol.num_buffered_mem_blocks = 0
        """
        """
        # Close and unlink old shared objects at object buffer location if available
        if interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks] != None:
            # Assuming the data in the shared memory had enought time to be written to disk
            # Close access to the shared memory from this process
            # Also, unlink the shared objects, and thus freeing up memory
            print("interpolate %s: closing access to shared memory" % current_process_name)
            sys.stdout.flush()
            interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][0].close()
            interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][1].close()
            #interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][0].unlink()
            #interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][1].unlink()
        """
        # Prepare shared memory objects for sharing result data with the image stitching process
        # https://docs.python.org/3.8/library/multiprocessing.shared_memory.html#multiprocessing.shared_memory.SharedMemory
        """
        print("1")
        sys.stdout.flush()
        subvol_mem_index_shared_buffer = \
            shared_memory.SharedMemory(create=True, \
            size=interpolate_subvol.subvol_mem_index_buffer.nbytes)
        print("2")
        sys.stdout.flush()
        subvol_mem_data_shared_buffer = \
            shared_memory.SharedMemory(create=True, \
            size=interpolate_subvol.subvol_mem_data_buffer.nbytes)    
        print("3")
        sys.stdout.flush()
        
        # Add the shared memory objects to a buffer
        interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks] = \
        (subvol_mem_index_shared_buffer, subvol_mem_data_shared_buffer)
        
        print("4")
        sys.stdout.flush()
        # View the shared memory data as numpy arrays for inserting data
        subvol_mem_index_shared_buffer_numpy = \
        np.ndarray(interpolate_subvol.subvol_mem_index_buffer.shape, \
            dtype=interpolate_subvol.subvol_mem_index_buffer.dtype, \
            buffer=interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][0].buf)
        print("5")
        sys.stdout.flush()
        subvol_mem_data_shared_buffer_numpy = \
        np.ndarray(interpolate_subvol.subvol_mem_data_buffer.shape, \
            dtype=interpolate_subvol.subvol_mem_data_buffer.dtype, \
            buffer=interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][1].buf)
        print("6")
        sys.stdout.flush()
        """
        # Edge case: all subvols have been processed and we need to fill an incomplete shared memory.
        # The incomplete / old parts of the shared memory data is set to np.nan for being discarded 
        # from inclusion in the stitching process
        if interpolate_subvol.last_mem_block_and_will_be_non_full:
            #print("interpolate %s: warning: non-full new shared memory, setting shared old data to np.nan to avoid being saved (again)\nIf RAM allows it, set subvols_mem_buffer_size to 'Auto' for optimal performance\nOtherwise, lower subvols_mem_buffer_size for lower RAM usage\n(slower, but better than receiving a lot of\nthis message when using a large subvols_mem_buffer_size)" % current_process_name)
            print("interpolate %s: warning: non-full new memory block containing old data, setting old data to np.nan to avoid being stitched (again)" % current_process_name)
            sys.stdout.flush()
            interpolate_subvol.subvol_mem_data_buffer[interpolate_subvol.num_subvols_buffered:] = np.nan
        """
        print("7")
        sys.stdout.flush()
        # Copy the the result data into shared memory
        #print("interpolate %s: copying interpolated data buffer into shared memory" % current_process_name)
        subvol_mem_index_shared_buffer_numpy[:] = \
            interpolate_subvol.subvol_mem_index_buffer[:]
        subvol_mem_data_shared_buffer_numpy[:] = \
            interpolate_subvol.subvol_mem_data_buffer[:]
        print("8")
        sys.stdout.flush()
        # Put the names of the shared memory to the queue for letting the image stitching process
        # access the shared memory by the using these names
        print("interpolate %s: putting shared memory names of interpolated data into queue" % current_process_name)
        sys.stdout.flush()
        interpolate_subvol.results_shared_mem_names_q.put_nowait((interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][0].name, \
                                                                  interpolate_subvol.shared_mem_obj_buffer[interpolate_subvol.num_buffered_mem_blocks][1].name))
        print("9")
        sys.stdout.flush()
        """
        
        # Save to DISK
        print("interpolate %s: saving interpolated data to temporalily to disk" % current_process_name)
        
        tempname=str(uuid.uuid4())
        np.save(interpolate_subvol.save_dir + "/idxtmp/" + tempname, interpolate_subvol.subvol_mem_index_buffer)
        np.save(interpolate_subvol.save_dir + "/datatmp/" + tempname, interpolate_subvol.subvol_mem_data_buffer)
        
        # Increment the shared memory objects buffer counters
        #interpolate_subvol.num_buffered_mem_blocks += 1
        #print("10")
        #sys.stdout.flush()
        interpolate_subvol.num_saved_mem_blocks += 1
        #print("11")
        #sys.stdout.flush()
        # Reset the subvol buffer counter
        interpolate_subvol.num_subvols_buffered = 0
        #print("12")
        #sys.stdout.flush()
        if interpolate_subvol.last_mem_block_and_will_be_non_full:
            # Resetting the incomplete buffer flag
            interpolate_subvol.last_mem_block_and_will_be_non_full = False
        #print("13")
        #sys.stdout.flush()

def interpolate_subvol_init(volumes_data_shared_mem, \
                            volumes_shape, \
                            intervals_between_volumes_t, \
                            subvol_shape, \
                            tot_vol_shape, \
                            subvols_mem_buffer_size, \
                            mem_block_buffer_size, \
                            tot_num_subvols, \
                            num_workers, \
                            interpolate_backend, \
                            num_extra_subvols, \
                            save_dir):
    #import os.path
    #import sys
    #sys.path.append('/home/ivar/Downloads/keops') # For enabling import of pykeops
    #import pykeops
    if interpolate_backend != "scipy_cpu":
        from pykeops.torch import LazyTensor
    
    # The queue containing names of the shared memory 
    # containing the results: indices and interpolated data
    #interpolate_subvol.results_shared_mem_names_q = results_shared_mem_names_q
    interpolate_subvol.save_dir = save_dir
    
    # Store the name of shared memory block containing all original volumes
    # non-interpolated volumes
    interpolate_subvol.volumes_data_shared_mem = volumes_data_shared_mem
    
    # 
    interpolate_subvol.volumes_shape = volumes_shape
    
    # 
    interpolate_subvol.intervals_between_volumes_t = intervals_between_volumes_t
    
    # The buffer size specified
    interpolate_subvol.subvols_mem_buffer_size = subvols_mem_buffer_size
    
    # A buffer counter that is used to fill the memory buffer
    interpolate_subvol.num_subvols_buffered = 0
    
    # non-shared memory buffers for storing the result data, before copying into shared memory
    interpolate_subvol.subvol_mem_index_buffer = \
        np.empty((subvols_mem_buffer_size, 3, 2), dtype=np.int32)
    
    interpolate_subvol.subvol_mem_data_buffer = \
        np.empty((subvols_mem_buffer_size, tot_vol_shape[0])+subvol_shape, dtype=np.float32)
        
    # Buffer for storing shared memory objects so that it can be accessed later
    interpolate_subvol.mem_block_buffer_size = mem_block_buffer_size
    
    #interpolate_subvol.shared_mem_obj_buffer = np.array([None]*mem_block_buffer_size)
    
    #interpolate_subvol.num_buffered_mem_blocks = 0
        
    interpolate_subvol.num_saved_mem_blocks = 0
    
    interpolate_subvol.last_mem_block_and_will_be_non_full = False
    
    interpolate_subvol.tot_num_subvols = tot_num_subvols
    
    interpolate_subvol.num_workers = num_workers
    
    interpolate_subvol.interpolate_backend = interpolate_backend
    
    interpolate_subvol.num_extra_subvols = num_extra_subvols
    
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
    
    def multiquadric_kernel(x, y, epsilon="Default", device='cpu'):
        
        #For use in pykeops
        
        # TODO: This kernel does not work correcty
        # Suspecting that it leads to K_xx that does not meed criteria
        # https://www.kernel-operations.io/keops/_auto_tutorials/interpolation/plot_RBF_interpolation_torch.html#interpolation-in-2d
        # https://www.kernel-operations.io/keops/_auto_examples/pytorch/plot_test_invkernel_torch_helper.html
        # https://www.kernel-operations.io/keops/api/math-operations.html
        # In other words, that multiquadric_kernel does not define a
        # symmetric, positive and definite linear reduction (?)
        """
        if epsilon=="Default":
            epsilon = \
            calculate_default_epsilon_rbf_multiquadric_4D(x.cpu().numpy())
            multiquadric_kernel.epsilon = \
            torch.tensor(epsilon, dtype=torch.float32, device=torch.device(device))
        """
        x_i = LazyTensor(x[:, None, :])  # (M, 1, :)
        y_j = LazyTensor(y[None, :, :])  # (1, N, :)
        D_ij = ((x_i - y_j) ** 2).sum(-1)  # (M, N) symbolic matrix of squared distances
        return ((1/epsilon * D_ij) ** 2 + 1).sqrt()
        
    interpolate_subvol.multiquadric_kernel = multiquadric_kernel
    
    def gaussian_kernel(x, y, epsilon="Default", device='cpu'):
        """
        if epsilon=="Default":
            epsilon = \
            calculate_default_epsilon_rbf_multiquadric_4D(x.cpu().numpy())
            gaussian_kernel.epsilon = \
            torch.tensor(epsilon, dtype=torch.float32, device=torch.device(device))
        """
        x_i = LazyTensor(x[:, None, :])  # (M, 1, 1)
        y_j = LazyTensor(y[None, :, :])  # (1, N, 1)
        D_ij = ((x_i - y_j) ** 2).sum(-1)  # (M, N) symbolic matrix of squared distances
        return (- D_ij / (2 * epsilon ** 2)).exp()  # (M, N) symbolic Gaussian kernel matrix
    
    interpolate_subvol.gaussian_kernel = gaussian_kernel
    
    interpolate_subvol.gaussian_kernel.calculate_default_epsilon_rbf_multiquadric_4D = \
    calculate_default_epsilon_rbf_multiquadric_4D
    
    def number_of_subvols_missing_before_no_remainder(extra_subvols, num_workers):
        missing = 0
        while extra_subvols % num_workers:
            missing += 1
            extra_subvols += 1
        return missing
    
    interpolate_subvol.number_of_subvols_missing_before_no_remainder = number_of_subvols_missing_before_no_remainder

if __name__ == "__main__":
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
    
    #interpolate_backend = "scipy_cpu"
    interpolate_backend = "pykeops_cpu"
    #interpolate_backend = "pykeops_cpu_gpu"
    #interpolate_backend = "pykeops_gpu"
    
    #if interpolate_backend == "pykeops_cpu_gpu" or interpolate_backend == "pykeops_gpu":
    if interpolate_backend != "scipy_cpu":
        # Required for using CUDA in subprocesses. 
        # By default, "fork" will not work.
        mp.set_start_method('spawn')
        import os
        import os.path
        #sys.path.append(os.getcwd()+'/lib/keops-master') # For enabling import of pykeops
        sys.path.append('/home/ivar/Downloads/keops') # For enabling import of pykeops
    
    # The number of workers. Make a large as possible, recommended number of cpu cores (mp.cpu_count()) - 1
    # Otherwise make smaller if the program takes up too much resources.
    # Main will run in a serparate process, thus subtract 1 from mp.cpu_count()
    # to utilize exactly all available cpu cores
    #num_workers = mp.cpu_count() - 1
    #num_workers = 10
    num_workers = 3
    
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
    #subvols_mem_buffer_size = 400
    #subvols_mem_buffer_size = 10
    # Automatic modes: comment out subvols_mem_buffer_size below
    subvols_mem_buffer_size = "AutoChunksize"
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
    mem_block_buffer_size = "Auto"
    
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
    volumes_data_arr = np.stack(tuple(volume_niilike.get_fdata(dtype=np.float32) for volume_niilike in volumes_niilike_t))
    
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
        subvols_mem_buffer_size, num_extra_subvols = calculate_chunksize(tot_num_subvols, num_workers, 4)
    elif subvols_mem_buffer_size == "AutoChunksize2":
        print("subvols_mem_buffer_size (chunksize) set to AutoChunksize2")
        subvols_mem_buffer_size, num_extra_subvols = calculate_chunksize(tot_num_subvols, num_workers, 4*70)
    elif subvols_mem_buffer_size == "AutoTotNum":
        print("subvols_mem_buffer_size (chunksize) set to AutoTotNum")
        subvols_mem_buffer_size, num_extra_subvols = calculate_chunksize(tot_num_subvols, num_workers, 1)
    
    # Calculate chunksize using a default formula
    #chunksize = calculate_default_chunksize(len(interval_indexes_l), num_workers)
    
    print("number of subvols to process: %i" % tot_num_subvols)
    print("selected subvol memory buffer size (chunksize): %i" % subvols_mem_buffer_size)
    
    if mem_block_buffer_size == "Auto":
        print("mem_block_buffer_size set to Auto")
        #mem_block_buffer_size = 2+((tot_num_subvols//(num_workers))//subvols_mem_buffer_size)
        #if (tot_num_subvols/num_workers) % subvols_mem_buffer_size:
        #"""
        if num_extra_subvols:
            mem_block_buffer_size = np.int32(1+((tot_num_subvols/num_workers)//subvols_mem_buffer_size))
        else:
            mem_block_buffer_size = np.int32((tot_num_subvols/num_workers)//subvols_mem_buffer_size)
        #"""
        #mem_block_buffer_size = np.int32((tot_num_subvols/num_workers)//subvols_mem_buffer_size)
    
    print("selected shared memory object buffer size: %i" % mem_block_buffer_size)
    #print("selected chunksize: %i" % chunksize)
    #"""
    
    # Partition the data up in i large list of 
    # tuples, where each tuple: (interval_index_t, subvol_data_arr)
    #indexes_subvols_tuple_l = make_index_volume_tuple_list(interval_indexes_l, volumes_data_arr)
    if interpolate_backend == "scipy_cpu":
        volumes_data_shared_mem_obj = put_data_in_shared_mem(volumes_data_arr)
        volumes_data_shared_mem = volumes_data_shared_mem_obj.name
    elif interpolate_backend == "pykeops_cpu" or interpolate_backend == "pykeops_cpu_gpu":
        volumes_data_shared_mem = put_data_in_shared_mem_torch(volumes_data_arr, device='cpu')
    elif interpolate_backend == "pykeops_gpu":
        volumes_data_shared_mem = put_data_in_shared_mem_torch(volumes_data_arr, device='cuda')
    
    # The shape of array containing all the original volumes
    volumes_shape = (len(volumes_niilike_t),) + vol_shape
    
    # Multiprocessing manager
    manager = mp.Manager()
        
    # Queue with tuples each containing two strings; (subvols_ind_shared_mem_name, subvols_data_shared_mem_name)
    #results_shared_mem_names_q = manager.Queue()
    
    # The final shape of the total volumes interpolated over time (number of time units)
    tot_vol_shape = (np.sum(intervals_between_volumes_t),) + vol_shape
    #"""
    # Initialize multiprocessing pool of num_workers workers
    mp_p = mp.Pool(num_workers, \
                   interpolate_subvol_init, \
                   initargs=(volumes_data_shared_mem, \
                             volumes_shape, \
                             intervals_between_volumes_t, \
                             subvol_shape, \
                             tot_vol_shape, \
                             subvols_mem_buffer_size, \
                             mem_block_buffer_size, \
                             tot_num_subvols, \
                             num_workers, \
                             interpolate_backend, \
                             num_extra_subvols, \
                             args.savedir) \
                   ) # maxtasksperchild=1
    #"""
    
    #
    pathlib.Path(args.savedir + "/idxtmp").mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.savedir + "/datatmp").mkdir(parents=False, exist_ok=True)
    
    """
    # Start process that listens for names of shared memory 
    # on results_shared_mem_names_qand that can be used to 
    # access result data from interpolation processes.
    # Stich together the result data into
    # the complete interpolated volume series in a memory efficient manner
    sp = mp.Process(target=stitch_subvols_from_shared_mem_and_save, args=(results_shared_mem_names_q, \
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
    """
    # Interpolate subvols in paralell
    #mp_p.starmap(interpolate_subvol, interval_indexes_l, chunksize=subvols_mem_buffer_size)
    
    mp_p.map(interpolate_subvol, interval_indexes_l, chunksize=subvols_mem_buffer_size)
    
    # Interpolation processes is finished, so put a finish messages to results_shared_mem_names_q in order to end
    # the writing process
    #results_shared_mem_names_q.put_nowait(("finished", "finished"))
    
    # Close the multiprocessing pool, joun for waiting for it to terminate
    #sp.join()
    
    mp_p.close()
    mp_p.join()
    
    stitch_subvols_from_tmpdir_and_save(subvol_shape, \
                                        tot_vol_shape, \
                                        tot_num_subvols, \
                                        args.savedir, \
                                        volumes_niilike_t[0].header, \
                                        volumes_niilike_t[0].affine, \
                                        subvols_mem_buffer_size, \
                                        using_mask, \
                                        mask_is_cube, \
                                        mask_data_arr)
    
    if interpolate_backend == "scipy_cpu":
        volumes_data_shared_mem_obj.close()
        volumes_data_shared_mem_obj.unlink()
    #"""
    print("----------------------------------------------------------------")   
    print("                  Finished interpolation                        ")
    print("----------------------------------------------------------------")

