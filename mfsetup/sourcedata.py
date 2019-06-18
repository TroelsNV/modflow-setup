import os
import numbers
import numpy as np
import pandas as pd
from flopy.utils import binaryfile as bf
from .fileio import save_array
from .discretization import fix_model_layer_conflicts, verify_minimum_layer_thickness
from .gis import get_values_at_points, shp2df
from .grid import get_ij
from .interpolate import get_source_dest_model_xys, interp_weights, interpolate, regrid
from .units import convert_length_units, convert_time_units, convert_flux_units

renames = {'mult': 'multiplier',
           'elevation_units': 'length_units',
           'from_parent': 'from_source_model_layers',
           }


class SourceData:
    """Class for handling source_data specified in config file.

    Parameters
    ----------
    data : str, list or dict
        source_data entry read from a configuration file
    """
    def __init__(self, filenames=None, length_units='unknown',
                 time_units='unknown',
                 dest_model=None):

        self.filenames = filenames
        self.length_units = length_units
        self.time_units = time_units
        self.dest_model = dest_model

    @property
    def unit_conversion(self):
        return self.length_unit_conversion * self.time_unit_conversion

    @property
    def length_unit_conversion(self):
        return convert_length_units(self.length_units,
                                    getattr(self.dest_model, 'length_units', 'unknown'))

    @property
    def time_unit_conversion(self):
        return convert_time_units(self.time_units,
                                  getattr(self.dest_model, 'time_units', 'unknown'))

    @staticmethod
    def from_config(data, type, **kwargs):
        """Create a SourceData instance from a source_data
        entry read from an MFsetup configuration file.

        Parameters
        ----------
        data : str, list or dict
            Parse entry from the configuration file.
        type : str
            'array' for array data or 'tabular' for tabular data


        """
        data_dict = {}
        key = 'filenames'
        if isinstance(data, dict):
            data = data.copy()
            # rename keys for constructors
            for k, v in renames.items():
                if k in data.keys():
                    data[v] = data.pop(k)
            data_dict = data.copy()
            if key[:-1] in data_dict.keys(): # plural vs singular
                data_dict[key] = {0: data_dict.pop(key[:-1])}
            elif key in data_dict.keys():
                if isinstance(data_dict[key], list):
                    data_dict[key] = {i: f for i, f in enumerate(data_dict[key])}
            elif 'from_source_model_layers' in data_dict.keys():
                pass
            else:
                data_dict = {key: data_dict}
        elif isinstance(data, str):
            data_dict[key] = {0: data}
        elif isinstance(data, list):
            data_dict[key] = {i: f for i, f in enumerate(data)}
        else:
            raise TypeError("unrecognized input: {}".format(data))

        if type == 'array':
            return ArraySourceData(**data_dict, **kwargs)
        elif type == 'tabular':
            return TabularSourceData(**data_dict, **kwargs)
        else:
            raise TypeError("need to specify data type (array or tabular)")


class ArraySourceData(SourceData):
    """Subclass for handling array-based source data."""
    def __init__(self, variable, filenames=None, length_units='unknown', time_units='unknown',
                 dest_model=None, source_modelgrid=None, source_array=None,
                 from_source_model_layers=None, by_layer=False,
                 resample_method='nearest', vmin=-1e30, vmax=1e30,
                 multiplier=1.):

        SourceData.__init__(self, filenames=filenames,
                            length_units=length_units, time_units=time_units,
                            dest_model=dest_model)

        self.variable = variable
        self.source_modelgrid = source_modelgrid
        self._source_mask = None
        if from_source_model_layers == {}:
            from_source_model_layers = None
        self.from_source_model_layers = from_source_model_layers
        self.source_array = None
        if source_array is not None:
            self.source_array = np.atleast_3d(source_array)
        self.dest_modelgrid = getattr(self.dest_model, 'modelgrid', None)
        self.by_layer = by_layer
        self.resample_method = resample_method
        self._interp_weights = None
        self.vmin = vmin,
        self.vmax = vmax,
        self.mult = multiplier
        self.data = {}
        assert True

    @property
    def dest_source_layer_mapping(self):
        nlay = self.dest_model.nlay
        if self.from_source_model_layers is None:
            return self.dest_model.parent_layers
        elif self.from_source_model_layers is not None:
            nspecified = len(self.from_source_model_layers)
            if self.by_layer and nspecified != nlay:
                raise Exception("Variable should have {} layers "
                                "but only {} are specified: {}"
                                .format(nlay, nspecified, self.from_source_model_layers))
            return self.from_source_model_layers
        elif self.filenames is not None:
            nspecified = len(self.filenames)
            if self.by_layer and nspecified != nlay:
                raise Exception("Variable should have {} layers "
                                "but only {} are specified: {}"
                                .format(nlay, nspecified, self.filenames))

    @property
    def interp_weights(self):
        """For a given parent, only calculate interpolation weights
        once to speed up re-gridding of arrays to inset."""
        if self._interp_weights is None:
            source_xy, dest_xy = get_source_dest_model_xys(self.parent,
                                                           self,
                                                           source_mask=self._source_grid_mask)
            self._interp_weights = interp_weights(source_xy, dest_xy)
        return self._interp_weights

    @property
    def _source_grid_mask(self):
        """Boolean array indicating window in parent model grid (subset of cells)
        that encompass the inset model domain. Used to speed up interpolation
        of parent grid values onto inset grid."""
        if self._source_mask is None:
            if self.dest_model.parent_mask.shape == self.source_modelgrid.xcellcenters.shape:
                mask = self.dest_model.parent_mask
            else:
                x, y = np.squeeze(self.dest_model.bbox.exterior.coords.xy)
                pi, pj = get_ij(self.source_modelgrid, x, y)
                pad = 2
                i0, i1 = pi.min() - pad, pi.max() + pad
                j0, j1 = pj.min() - pad, pj.max() + pad
                mask = np.zeros((self.source_modelgrid.nrow,
                                 self.source_modelgrid.ncol), dtype=bool)
                mask[i0:i1, j0:j1] = True
            self._source_mask = mask
        return self._source_mask

    def regrid_from_source_model(self, source_array,
                                 mask=None,
                                 method='linear'):
        """Interpolate values in source array onto
        the destination model grid, using SpatialReference instances
        attached to the source and destination models.

        Parameters
        ----------
        source_array : ndarray
            Values from source model to be interpolated to destination grid.
            1 or 2-D numpy array of same sizes as a
            layer of the source model.
        mask : ndarray (bool)
            1 or 2-D numpy array of same sizes as a
            layer of the source model. True values
            indicate cells to include in interpolation,
            False values indicate cells that will be
            dropped.
        method : str ('linear', 'nearest')
            Interpolation method.
        """
        if mask is not None:
            return regrid(source_array, self.source_modelgrid, self.dest_modelgrid,
                          mask1=mask,
                          method=method)
        if method == 'linear':
            parent_values = source_array.flatten()[self._source_grid_mask.flatten()]
            regridded = interpolate(parent_values,
                                    *self.interp_weights)
        elif method == 'nearest':
            regridded = regrid(source_array, self.source_modelgrid, self.dest_modelgrid,
                               method='nearest')
        regridded = np.reshape(regridded, (self.dest_modelgrid.nrow,
                                           self.dest_modelgrid.ncol))
        return regridded

    def get_data(self):
        data = {}
        if self.filenames is not None:
            for i, f in self.filenames.items():

                if isinstance(f, numbers.Number):
                    data[i] = f
                elif isinstance(f, str):
                    # sample "source_data" that may not be on same grid
                    # TODO: add bilinear and zonal statistics methods
                    if f.endswith(".asc") or f.endswith(".tif"):
                        if self.resample_method != 'nearest':
                            raise ValueError('unrecognized resample method: {}'.format(self.resample_method))
                        arr = get_values_at_points(f,
                                                   self.dest_model.modelgrid.xcellcenters.ravel(),
                                                   self.dest_model.modelgrid.ycellcenters.ravel())
                        arr = np.reshape(arr, (self.dest_modelgrid.nrow,
                                               self.dest_modelgrid.ncol))

                    # TODO: add code to interpret hds and cbb files
                    # interpolate from source model using source model grid
                    # otherwise assume the grids are the same

                    # read numpy array on same grid
                    # (load_array checks the shape)
                    elif self.source_modelgrid is None:
                        arr = self.dest_model.load_array(f)

                    assert arr.shape == self.dest_modelgrid.shape[1:]
                    data[i] = arr * self.mult * self.unit_conversion

            # interpolate any missing arrays from consecutive files based on weights
            for i, arr in data.items():
                if np.isscalar(arr):
                    source_k = arr
                    weight0 = source_k - np.floor(source_k)
                    # get the next layers above and below that have data
                    source_k0 = int(np.max([k for k, v in data.items()
                                            if isinstance(v, np.ndarray) and k < i]))
                    source_k1 = int(np.min([k for k, v in data.items()
                                            if isinstance(v, np.ndarray) and k > i]))
                    data[i] = weighted_average_between_layers(data[source_k0],
                                                              data[source_k1],
                                                              weight0=weight0)

        # regrid source data from another model
        elif self.source_array is not None:

            for dest_k, source_k in self.dest_source_layer_mapping.items():

                # destination model layers copied from source model layers
                if source_k <= 0:
                    arr = self.source_array[0]
                elif np.round(source_k, 4) in range(self.source_array.shape[0]):
                    source_k = int(np.round(source_k, 4))
                    arr = self.source_array[source_k]
                # destination model layers that are a weighted average
                # of consecutive source model layers
                else:
                    weight0 = source_k - np.floor(source_k)
                    source_k0 = int(np.floor(source_k))
                    source_k1 = int(np.ceil(source_k))
                    arr = weighted_average_between_layers(self.source_array[source_k0],
                                                          self.source_array[source_k1],
                                                          weight0=weight0)
                # interpolate from source model using source model grid
                # otherwise assume the grids are the same
                if self.source_modelgrid is not None:
                    # exclude invalid values in interpolation from parent model
                    mask = self._source_grid_mask & (arr > self.vmin) & (arr < self.vmax)

                    arr = self.regrid_from_source_model(arr,
                                                        mask=mask,
                                                        method='linear')

                assert arr.shape == self.dest_modelgrid.shape[1:]
                data[dest_k] = arr * self.mult * self.unit_conversion

        # no files or source array provided
        else:
            raise ValueError("No files or source model grid provided.")
        self.data = data
        return data

    @staticmethod
    def from_config(data, **kwargs):
        return SourceData.from_config(data, type='array', **kwargs)


class MFBinaryArraySourceData(ArraySourceData):
    """Subclass for handling MODFLOW binary array data
    that may come from another model."""
    def __init__(self, variable, filename=None,
                 length_units='unknown', time_units='unknown',
                 dest_model=None, source_modelgrid=None,
                 from_source_model_layers=None, by_layer=False,
                 resample_method='nearest', vmin=-1e30, vmax=1e30
                 ):

        ArraySourceData.__init__(self, variable=variable,
                                 length_units=length_units, time_units=time_units,
                                 dest_model=dest_model, source_modelgrid=source_modelgrid,
                                 from_source_model_layers=from_source_model_layers,
                                 by_layer=by_layer,
                                 resample_method=resample_method, vmin=vmin, vmax=vmax)

        self.filename = filename

    @property
    def dest_source_layer_mapping(self):
        nlay = self.dest_model.nlay
        # if mapping between source and dest model layers isn't specified
        # use property from dest model
        # this will be the DIS package layer mapping if specified
        # otherwise same layering is assumed for both models
        if self.from_source_model_layers is None:
            return self.dest_model.parent_layers
        elif self.from_source_model_layers is not None:
            nspecified = len(self.from_source_model_layers)
            if self.by_layer and nspecified != nlay:
                raise Exception("Variable should have {} layers "
                                "but only {} are specified: {}"
                                .format(nlay, nspecified, self.from_source_model_layers))
            return self.from_source_model_layers

    def get_data(self, **kwargs):
        """Get array data from binary file for a single time;
        regrid from source model to dest model and transfer layer
        data from source model to dest model based on from_source_model_layers
        argument to class.

        Parameters
        ----------
        kwargs : keyword arguments to flopy.utils.binaryfile.HeadFile

        Returns
        -------
        data : dict
            Dictionary of 2D arrays keyed by destination model layer.
        """

        if self.filename.endswith('hds'):
            bfobj = bf.HeadFile(self.filename)
            self.source_array = bfobj.get_data(**kwargs)

        elif self.filename[:-4] in {'.cbb', '.cbc'}:
            raise NotImplementedError('Cell Budget files not supported yet.')

        data = {}
        for dest_k, source_k in self.dest_source_layer_mapping.items():

            # destination model layers copied from source model layers
            if source_k <= 0:
                arr = self.source_array[0]
            elif np.round(source_k, 4) in range(self.source_array.shape[0]):
                source_k = int(np.round(source_k, 4))
                arr = self.source_array[source_k]
            # destination model layers that are a weighted average
            # of consecutive source model layers
            # TODO: add transmissivity-based weighting if upw exists
            else:
                weight0 = source_k - np.floor(source_k)
                source_k0 = int(np.floor(source_k))
                source_k1 = int(np.ceil(source_k))
                arr = weighted_average_between_layers(self.source_array[source_k0],
                                                      self.source_array[source_k1],
                                                      weight0=weight0)
            # interpolate from source model using source model grid
            # otherwise assume the grids are the same
            if self.source_modelgrid is not None:
                # exclude invalid values in interpolation from parent model
                mask = self._source_grid_mask & (arr > self.vmin) & (arr < self.vmax)

                arr = self.regrid_from_source_model(arr,
                                                    mask=mask,
                                                    method='linear')

            assert arr.shape == self.dest_modelgrid.shape[1:]
            data[dest_k] = arr * self.mult * self.unit_conversion

        self.data = data
        return data


class MFArrayData(SourceData):
    """Subclass for handling array-based source data that can
    be scalars, lists of scalars, array data or filepath(s) to arrays on
    same model grid."""
    def __init__(self, variable, filenames=None, values=None, length_units='unknown', time_units='unknown',
                 dest_model=None, vmin=-1e30, vmax=1e30, by_layer=False,
                 multiplier=1.):

        SourceData.__init__(self, filenames=filenames, length_units=length_units, time_units=time_units,
                            dest_model=dest_model)

        self.variable = variable
        self.values = values
        self.vmin = vmin
        self.vmax = vmax
        self.mult = multiplier
        self.dest_modelgrid = getattr(self.dest_model, 'modelgrid', None)
        self.by_layer = by_layer
        self.data = {}
        assert True

    def get_data(self):
        data = {}

        # convert to dict
        if isinstance(self.values, str) or np.isscalar(self.values):
            if self.by_layer:
                nk = self.dest_model.nlay
            else:
                nk = 1
            self.values = {k: self.values for k in range(nk)}
        elif isinstance(self.values, list):
            self.values = {i: val for i, val in enumerate(self.values)}
        for i, val in self.values.items():
            if isinstance(val, str):
                abspath = os.path.normpath(os.path.join(self.dest_model._config_path, val))
                arr = np.loadtxt(abspath)
            elif np.isscalar(val):
                arr = np.ones(self.dest_modelgrid.shape[1:]) * val
            else:
                arr = val
            assert arr.shape == self.dest_modelgrid.shape[1:]
            data[i] = arr * self.mult * self.unit_conversion

        if self.by_layer:
            if len(data) != self.dest_model.nlay:
                raise Exception("Variable should have {} layers "
                                "but only {} are specified: {}"
                                .format(self.dest_model.nlay,
                                        len(data),
                                        self.values))
        self.data = data
        return data

    @staticmethod
    def from_config(data, **kwargs):
        raise NotImplementedError()


class TabularSourceData(SourceData):
    """Subclass for handling array-based source data."""

    def __init__(self, filenames, id_column=None, include_ids=None,
                 length_units='unknown', time_units='unknown',
                 column_mappings=None,
                 dest_model=None):
        SourceData.__init__(self, filenames=filenames, length_units=length_units, time_units=time_units,
                            dest_model=dest_model)

        self.id_column = id_column
        self.include_ids = include_ids
        self.column_mappings = column_mappings
        assert True

    @staticmethod
    def from_config(data, **kwargs):
        return SourceData.from_config(data, type='tabular', **kwargs)

    def get_data(self):

        dfs = []
        for i, f in self.filenames.items():
            if f.endswith('.shp') or f.endswith('.dbf'):
                df = shp2df(f)

            elif f.endswith('.csv'):
                df = pd.read_csv(f)

            dfs.append(df)

        df = pd.concat(dfs)
        if self.id_column is not None:
            df.index = df[self.id_column]
        if self.include_ids is not None:
            df = df.loc[self.include_ids]

        # rename any columns specified in config file to required names
        df.rename(columns=self.column_mappings, inplace=True)
        df.columns = [c.lower() for c in df.columns]

        # drop any extra unnamed columns from accidental saving of the index on to_csv
        drop_columns = [c for c in df.columns if 'unnamed' in c]
        df.drop(drop_columns, axis=1, inplace=True)

        return df


def setup_array(model, package, var, vmin=-1e30, vmax=1e30,
                source_model=None, source_package=None,
                write_fmt='%.6e',
                **kwargs):

    data = model.cfg[package].get(var)

    # for getting data from a different package in the source model
    if source_package is None:
        source_package = package

    # data specified directly
    if data is not None:
        sd = MFArrayData(variable=var, values=data, dest_model=model,
                         vmin=vmin, vmax=vmax,
                         **kwargs)

    # data specified as source_data
    else:
        cfg = model.cfg[package].get('source_data', {})
        cfg_data = cfg.get(var, {'from_parent': {}})
        from_model_keys = [k for k in cfg_data.keys() if 'from_' in k]
        from_model = True if len(from_model_keys) > 0 else False
        # data from files
        if var in cfg and not from_model:
            # TODO: files option doesn't support interpolation between top and botm[0]
            sd = ArraySourceData.from_config(model.cfg[package]['source_data'][var],
                                             variable=var,
                                             dest_model=model,
                                             vmin=vmin, vmax=vmax,
                                             **kwargs)

        # data regridded from parent model
        elif from_model:
            key = from_model_keys[0]
            from_source_model_layers = cfg_data[key].copy() #cfg[var][key].copy()
            modelname = key.split('_')[1]
            filenames = None
            # TODO: generalize this to allow for more than one source model
            if modelname == 'parent':
                source_model = model.parent

            # data from parent model MODFLOW binary output
            if 'binaryfile' in from_source_model_layers:
                filename = from_source_model_layers.pop('binaryfile')
                sd = MFBinaryArraySourceData(variable=var, filename=filename,
                                             dest_model=model,
                                             source_modelgrid=source_model.modelgrid,
                                             from_source_model_layers=from_source_model_layers,
                                             length_units=model.cfg[modelname]['length_units'],
                                             time_units=model.cfg[modelname]['time_units'],
                                             vmin=vmin, vmax=vmax,
                                             **kwargs)

            # data read from Flopy instance of parent model
            else:
                # the botm array has to be handled differently
                # because dest. layers may be interpolated between
                # model top and first botm
                if var == 'botm':
                    nlay, nrow, ncol = source_model.dis.botm.array.shape
                    source_array = np.zeros((nlay+1, nrow, ncol))
                    source_array[0] = source_model.dis.top.array
                    source_array[1:] = source_model.dis.botm.array
                    from_source_model_layers = {k: v+1 for k, v in from_source_model_layers.items()}
                else:
                    source_array = getattr(source_model, source_package).__dict__[var].array

                sd = ArraySourceData(variable=var, filenames=filenames,
                                     dest_model=model,
                                     source_modelgrid=source_model.modelgrid,
                                     source_array=source_array,
                                     from_source_model_layers=from_source_model_layers,
                                     length_units=model.cfg[modelname]['length_units'],
                                     time_units=model.cfg[modelname]['time_units'],
                                     vmin=vmin, vmax=vmax,
                                     **kwargs)
            if var == 'vka':
                model.cfg['upw']['layvka'] = getattr(source_model, source_package).layvka.array[0]
        else:
            raise Exception("No source data found for {} package: {}".format(package, var))

    data = sd.get_data()

    # special handling of some variables
    # (for lakes)
    if var == 'botm':
        bathy = model.lake_bathymetry
        top = model.load_array(model.cfg['intermediate_data']['top'][0])
        lake_botm_elevations = top[bathy != 0] - bathy[bathy != 0]

        # adjust layer botms to lake bathymetry (if any)
        # set layer bottom at lake cells to the botm of the lake in that layer
        for k, botm in data.items():
            inlayer = lake_botm_elevations > botm[bathy != 0]
            if not np.any(inlayer):
                continue
            botm[bathy != 0][inlayer] = lake_botm_elevations[inlayer]

        # fix any layering conflicts and save out botm files
        botm = np.stack([data[i] for i in range(len(data))])
        min_thickness = model.cfg['dis'].get('minimum_layer_thickness', 1)
        botm = fix_model_layer_conflicts(top, botm,
                                         minimum_thickness=min_thickness)
        isvalid = verify_minimum_layer_thickness(top, botm,
                                                 np.ones(botm.shape, dtype=int),
                                                 min_thickness)
        if not isvalid:
            raise Exception('Model layers less than {} {} thickness'.format(min_thickness,
                                                                            model.length_units))
        data = {i: arr for i, arr in enumerate(botm)}
    elif var == 'rech':
        for i, arr in data.items():
            # assign high-k lake recharge for stress period
            # apply in same units as source recharge array
            data[i][model.isbc[0] == 2] = model.lake_recharge[i]
            # zero-values to lak package lakes
            data[i][model.isbc[0] == 1] = 0.
    elif var == 'ibound':
        for i, arr in data.items():
            data[i][model.isbc[i] == 1] = 0.
    elif var in ['hk', 'k']:
        for i, arr in data.items():
            data[i][model.isbc[i] == 2] = model.cfg['model'].get('hiKlakes_value', 1e4)
    elif var in ['ss', 'sy']:
        for i, arr in data.items():
            data[i][model.isbc[i] == 2] = 1.



    # intermediate data
    # set paths to intermediate files and external files
    model.setup_external_filepaths(package, var,
                                   model.cfg[package]['{}_filename_fmt'.format(var)],
                                   nfiles=len(data))
    # write out array data to intermediate files
    # assign lake recharge values (water balance surplus) for any high-K lakes
    for i, arr in data.items():
        save_array(model.cfg['intermediate_data'][var][i], arr, fmt=write_fmt)


def weighted_average_between_layers(arr0, arr1, weight0=0.5):
    """"""
    weights = [weight0, 1-weight0]
    return np.average([arr0, arr1], axis=0, weights=weights)