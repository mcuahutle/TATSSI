
import os
import sys

# TATSSI modules
from pathlib import Path
current_dir = os.path.dirname(os.path.realpath(__file__))
src_dir = Path(current_dir).parents[1]
sys.path.append(str(src_dir.absolute()))

from TATSSI.time_series.smoothn import smoothn
from TATSSI.time_series.analysis import Analysis
from TATSSI.time_series.mk_test import mk_test
from TATSSI.UI.plots_time_series_analysis import PlotAnomalies
from TATSSI.input_output.utils import save_dask_array, \
        get_geotransform_from_xarray, validate_coordinates, \
        transform_to_wgs84
from TATSSI.UI.helpers.utils import *

#from TATSSI.notebooks.helpers.time_series_analysis import \
#        TimeSeriesAnalysis

import ogr
import xarray as xr
import numpy as np
from rasterio import logging as rio_logging
from statsmodels.tsa.seasonal import seasonal_decompose
from scipy.signal import find_peaks
from scipy.stats import norm

import seaborn as sbn

from dask.diagnostics import ProgressBar
from numba import jit

import matplotlib
matplotlib.use("Qt5Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT \
        as NavigationToolbar

import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.io.shapereader import Reader as cReader
from cartopy.feature import ShapelyFeature
import osr

from PyQt5 import QtCore, QtGui, QtWidgets, uic
from PyQt5.QtCore import Qt, pyqtSlot
from PyQt5.QtGui import QFont

# Experimental
from rpy2.robjects.packages import importr
from rpy2.robjects import FloatVector
from rpy2.robjects import numpy2ri

def get_projection(proj4_string):
    """
    Get spatial reference system from PROJ4 string
    """
    srs = osr.SpatialReference()
    srs.ImportFromProj4(proj4_string)

    return srs

class TimeSeriesAnalysisUI(QtWidgets.QMainWindow):
    def __init__(self, fname, parent=None):
        super(TimeSeriesAnalysisUI, self).__init__(parent)
        uic.loadUi('time_series_analysis.ui', self)
        self.parent = parent

        # List of dialogs
        self.dialogs = list()

        # Set input file name
        self.fname = fname

        # Disable RasterIO logging, just show ERRORS
        log = rio_logging.getLogger()
        log.setLevel(rio_logging.ERROR)

        # Change point R library
        self.cpt = importr('changepoint')

        # Plot input data
        self._plot()

        self.show()

    def __set_variables(self):
        """
        Set variables from TATSSI Time Series object
        """
        # TATSSI time series object
        self.ts = Analysis(fname=self.fname)

        # imshow plots
        self.left_imshow = None
        self.right_imshow = None
        self.projection = None
        # decomposition objects
        self.trend = None
        self.seasonal = None
        self.resid_a = None
        self.resid_m = None

        # Connect time steps with corresponsing method
        self.time_steps_left.currentIndexChanged.connect(
                self.__on_time_steps_left_change)

        self.time_steps_right.currentIndexChanged.connect(
                self.__on_time_steps_right_change)

        # Change time_steps combo boxes stylesheet and add scrollbar
        self.time_steps_left.setStyleSheet("combobox-popup: 0")
        self.time_steps_left.view().setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded)

        self.time_steps_right.setStyleSheet("combobox-popup: 0")
        self.time_steps_right.view().setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded)

        self.bandwidth.setStyleSheet("combobox-popup: 0")
        self.bandwidth.view().setVerticalScrollBarPolicy(
                Qt.ScrollBarAsNeeded)

        # Anomalies button
        self.pbAnomalies.clicked.connect(
                self.on_pbAnomalies_click)
        # Overlay button
        self.pbOverlay.clicked.connect(
                self.on_pbOverlay_click)
        # Climatology button
        self.pbClimatology.clicked.connect(
                self.on_pbClimatology_click)
        # Decomposition button
        self.pbDecomposition.clicked.connect(
                self.on_pbDecomposition_click)
        # MK test
        self.pbMKTest.clicked.connect(
                self.on_pbMKTest_click)

        # Change Point Detection button
        self.pbCPD.clicked.connect(
                self.on_pbCPD_click)

        # Location text event filter to capture ENTER key press
        self.txtLocation.installEventFilter(self)

        # Data variables
        self.data_vars.addItems(self.__fill_data_variables())

        # Years
        self.__fill_year()
        self.years.currentIndexChanged.connect(
                self.__on_years_change)

        # Bandwidth use for time series decomposition
        self.__fill_bandwidth()

        # Time steps
        self.time_steps_left.addItems(self.__fill_time_steps())
        self.time_steps_right.addItems(self.__fill_time_steps())

        # Decomposition model
        self.model.addItems(self.__fill_model())

        # Create plot objects
        self.__create_plot_objects()

        # Populate plots
        self.__populate_plots()

        # Climatology year
        self.climatology_year = None
        self.anomalies = None

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and \
                obj is self.txtLocation:
            if event.key() == QtCore.Qt.Key_Return and \
                    self.txtLocation.hasFocus():
                # Plot on_click event
                self.on_click(event)

        return super().eventFilter(obj, event)

    @log("ficheroLog.log")
    def on_pbMKTest_click(self):
        """
        Compute and save Mann-Kendall test products
        """
        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        start = time.time()
        tiempo_inicial = datetime.now()

        self.progressBar.setEnabled(True)
        msg = f"Computing Mann-Kendall test..."
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        # Get trend based on a moving window
        period = int(self.bandwidth.currentText())
        # Get data type
        dtype = self.left_ds.dtype

        # Check if we have to subset the data
        if not self.left_imshow.get_extent() == self.left_p.get_extent():
            # Create subset
            w, e, s, n = self.left_p.get_extent()
            _data = self.left_ds.sel(longitude=slice(int(w),int(e)),
                    latitude=slice(int(n),int(s)))

            new_gt = get_geotransform_from_xarray(_data)
            _data.attrs['transform'] = new_gt
        else:
            _data = self.left_ds

        def __get_z(x, s=None):
            n = x.shape[2]

            for k in range(n-1):
                for j in range(k+1, n):
                    if s is None:
                        s = np.sign(x[:,:,j] - x[:,:,k])
                    else:
                        s += np.sign(x[:,:,j] - x[:,:,k])

            var_s = (n*(n-1)*(2*n+5))/18

            z = np.where(s > 0,
                    (s - 1)/np.sqrt(var_s),
                    (s + 1)/np.sqrt(var_s)).astype(np.float32)

            z[s==0] = 0.0

            return z

        def __get_p(z):
            # Two tail test
            p = (2*(1-norm.cdf(abs(z)))).astype(np.float32)

            return p

        def __get_h(z, alpha=0.05):
            h = (abs(z) > norm.ppf(1-alpha/2)).astype(np.int8)

            return h

        def __get_trend(z, h):
            trend = np.where((z < 0) & (h == True), -1, 0).astype(np.int16)

            trend = np.where((z > 0) & (h == True), 1, trend)

            return trend

        z = xr.apply_ufunc(__get_z, _data,
                input_core_dims=[['time']],
                dask='parallelized',
                output_dtypes=[np.float32])

        z = z.compute()

        p = xr.apply_ufunc(__get_p, z,
                dask='parallelized',
                output_dtypes=[np.float32])

        h = xr.apply_ufunc(__get_h, z,
                dask='parallelized',
                output_dtypes=[np.int16])

        trend = xr.apply_ufunc(__get_trend, z, h,
                dask='parallelized',
                output_dtypes=[np.int16])

        # Save products
        var = self.data_vars.currentText()
        products = [z, p, h, trend]
        product_names = ['z', 'p', 'h', 'trend']

        for i, product in enumerate(product_names):
            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_mann-kendall_test_{product}.tif')

            msg = f"Saving Mann-Kendal test - {product}..."
            self.progressBar.setFormat(msg)
            self.progressBar.setValue(1)

            # Set attributes from input data
            products[i].attrs = _data.attrs

            # Add time dimension
            products[i] = products[i].expand_dims(
                    dim='time', axis=0)

            save_dask_array(fname=fname, data=products[i],
                    data_var=var, method=None,
                    n_workers=4, progressBar=self.progressBar)

            self.progressBar.setValue(1)

        self.progressBar.setValue(0)
        self.progressBar.setEnabled(False)
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** MK Test *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @log("ficheroLog.log")
    def __frequency_analysis(self):
        """
        Computes the annual frequency of peaks and valleys
        """
        start = time.time()
        tiempo_inicial = datetime.now()
        self.progressBar.setEnabled(True)
        msg = f"Computing one and two-peak annual frequencies..."
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        # Ouput xarrays
        peaks = xr.zeros_like(self.left_ds)
        peaks = peaks.compute()

        layers, rows, cols = self.left_ds.shape

        # Set required distance to be consider an independent peak
        # The assumption is that a peak should occure on a different
        # season, hence getting all time steps on a single year / 4
        distance = int(np.ceil(len(self.single_year_ds.time) / 4))

        # TODO Extremely inefficient way to compute peaks and valleys
        # must be changes for a array-based solution!
        for c in range(cols):
            self.progressBar.setValue(int(((c+1) / cols) * 100))
            for r in range(rows):
                idx, _ = find_peaks(self.left_ds[:,r,c], distance=distance)
                peaks[idx,r,c] = 1

        # Get the number of peaks and valleys on a calendar year
        annual_peaks = peaks.groupby("time.year")
        annual_peaks = annual_peaks.sum(dim='time').astype(np.int8)
        # Copy attributes
        annual_peaks.attrs = self.left_ds.attrs

        # Get the frequency of one and two peaks
        n_years = len(annual_peaks.year)
        freq_one_peak = annual_peaks.where(annual_peaks==1).count(dim='year')
        freq_one_peak = freq_one_peak / n_years
        # Copy attributes
        freq_one_peak.attrs = self.left_ds.attrs
        # Add time dimension
        freq_one_peak = freq_one_peak.expand_dims(
                dim='time', axis=0)

        freq_two_peak = annual_peaks.where(annual_peaks==2).count(dim='year')
        freq_two_peak = freq_two_peak / n_years
        # Copy attributes
        freq_two_peak.attrs = self.left_ds.attrs
        # Add time dimension
        freq_two_peak = freq_two_peak.expand_dims(
                dim='time', axis=0)

        msg = f"Saving peaks and valleys frequencies..."
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_peaks.tif')

        save_dask_array(fname=fname, data=annual_peaks,
                data_var=self.data_vars.currentText(), method=None,
                n_workers=4, progressBar=self.progressBar)

        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_one_peak_frequency.tif')

        save_dask_array(fname=fname, data=freq_one_peak,
                data_var=self.data_vars.currentText(), method=None,
                n_workers=4, progressBar=self.progressBar)

        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_two_peaks_frequency.tif')

        save_dask_array(fname=fname, data=freq_two_peak,
                data_var=self.data_vars.currentText(), method=None,
                n_workers=4, progressBar=self.progressBar)

        self.progressBar.setValue(0)
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** Frecuency Analysis  *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

    @log("ficheroLog.log")
    def on_pbDecomposition_click(self):
        """
        Save decomposition products:
            Trend, Seasonality, Residuals
        """
        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        start = time.time()
        tiempo_inicial = datetime.now()

        # Annual frequency of peaks and valleys
        self.__frequency_analysis()

        self.progressBar.setEnabled(True)
        msg = f"Computing time series decomposition..."
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        # Extract period from the current single year
        period = int(self.bandwidth.currentText())
        nobs = len(self.left_ds)

        # Get data type
        dtype = self.left_ds.dtype

        # Get trend based on a moving window
        trend = self.left_ds.rolling(time=period, min_periods=1,
                center=True).mean().astype(dtype)
        trend.attrs = self.left_ds.attrs

        unique_diffs = np.sort(np.unique(np.diff(self.left_ds.time.dt.month.values)))
        if unique_diffs[0] == -11 and unique_diffs[1] == 1:
            _idx = 'time.month'
        else:
            _idx = 'time.dayofyear'

        period_averages = self.left_ds.groupby(_idx)
        period_averages = period_averages.mean(axis=0).astype(dtype)

        if self.model.currentText()[0] == 'a':
            period_averages -= period_averages.mean(axis=0).astype(dtype)
            seasonal = np.tile(period_averages.T, nobs // period + 1).T[:nobs]
            residuals = (self.left_ds - trend - seasonal).astype(dtype)
        else:
            period_averages /= period_averages.mean(axis=0).astype(dtype)
            seasonal = np.tile(period_averages.T, nobs // period + 1).T[:nobs]
            residuals = (self.left_ds / seasonal / trend).astype(dtype)

        seasonal = None
        del(seasonal)

        period_averages.attrs = self.left_ds.attrs
        residuals.attrs = self.left_ds.attrs

        # Save to disk
        products = [trend, period_averages, residuals]
        product_names = ['trend', 'seasonality', 'residuals']

        var = self.data_vars.currentText()

        for i, product in enumerate(product_names):
            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_seasonal_decomposition_{product}.tif')

            msg = f"Computing time series decomposition - {product}..."
            self.progressBar.setFormat(msg)
            self.progressBar.setValue(1)

            save_dask_array(fname=fname, data=products[i],
                            data_var=var, method=None,
                            n_workers=4, progressBar=self.progressBar)

            self.progressBar.setValue(1)

        # Delete big arrays onced they are saved
        period_averages, residuals = None, None
        del(period_averages, residuals)

        self.progressBar.setValue(0)
        self.progressBar.setEnabled(False)
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** Decomposition  *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @pyqtSlot(int)
    def __on_time_steps_change(self, index):
        """
        Handles a change in the time step to display
        """
        if len(self.time_steps_left.currentText()) == 0 or \
                len(self.time_steps_right.currentText()) == 0 or \
                self.left_imshow is None or \
                self.right_imshow is None:
            return None

        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)

        self.left_imshow.set_data(self.single_year_ds.data[index])
        self.right_imshow.set_data(self.single_year_ds.data[index])

        # Set titles
        self.left_p.set_title(self.time_steps_left.currentText())
        self.right_p.set_title(self.time_steps_right.currentText())

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @log("ficheroLog.log")
    def on_pbClimatology_click(self):
        """
        Save to COGs all the time series analysis products
        """
        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        start = time.time()
        tiempo_inicial = datetime.now()

        # Compute climatology
        self.ts.climatology()

        self.progressBar.setEnabled(True)
        self.progressBar.setValue(1)
        msg = f"Computing quartiles and saving outliers..."
        self.progressBar.setFormat(msg)
        
        quartiles = [0.25, 0.5, 0.75]
        n_quartiles = len(quartiles)
        quartile_names = ['Q1', 'median', 'Q2', 'minimum', 'maximum']
        var = self.data_vars.currentText()

        # Group by day of year to compute quartiles
        unique_diffs = np.sort(np.unique(np.diff(self.ts.data[var].time.dt.month.values)))
        if unique_diffs[0] == -11 and unique_diffs[1] == 1:
            _idx = 'time.month'
            _timeName = 'month'
        else:
            _idx = 'time.dayofyear'
            _timeName = 'DoY'
        grouped_by_doy = self.ts.data[var].fillna(0).time.groupby(_idx)
        # Get the number of time steps
        n_time_steps = len(grouped_by_doy.groups.keys())
        # rows and cols
        rows, cols = self.ts.data[var].shape[1:]
        # Create output array
        # Layers
        # 0 - Q1
        # 1 - Median
        # 2 - Q3
        # 3 - Q1 - 1.5 * IQR
        # 4 - Q3 + 1.5 * IQR
        q_data = np.zeros((n_quartiles+2, n_time_steps, rows, cols))

        # Store Days of Year
        doys = []

        for i, (doy, _times) in enumerate(grouped_by_doy):
            self.progressBar.setValue(int((i/len(grouped_by_doy))*100))

            doys.append(doy)

            # Get time series for DoY
            ts_doy = self.ts.data[var].sel(time=_times.data)
            # Get quartiles for DoY
            q_data[0:3,i] = np.quantile(ts_doy, q=quartiles, axis=0)

            # Get IQR
            iqr = q_data[2,i] - q_data[0,i]
            # Get boundaries
            q_data[3,i] = q_data[0,i] - (1.5 * iqr)
            q_data[4,i] = q_data[2,i] + (1.5 * iqr)

            # Upper boundary outliers
            upper_boundary_outliers = ts_doy < q_data[3,i]
            upper_boundary_outliers.attrs = ts_doy.attrs
            if _timeName == 'month':
                _time = f'{doy:02d}'
            else:
                _time = f'{doy:03d}'
            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_upper_boundary_outliers_{_timeName}_{_time}.tif')

            save_dask_array(fname=fname,
                            data=upper_boundary_outliers,
                            data_var=var, method=None,
                            n_workers=4)

            # Lower boundary outliers
            lower_boundary_outliers = ts_doy < q_data[4,i]
            lower_boundary_outliers.attrs = ts_doy.attrs
            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_lower_boundary_outliers_DoY_{doy:03d}.tif')

            save_dask_array(fname=fname,
                            data=lower_boundary_outliers,
                            data_var=var, method=None,
                            n_workers=4)

        # Save quartiles
        self.progressBar.setValue(0)
        msg = f"Saving quartiles..."
        self.progressBar.setFormat(msg)

        for i, quartile_name in enumerate(quartile_names):
            self.progressBar.setValue(int((i/len(quartile_names))*100))

            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_climatology_quartile_{quartile_name}.tif')

            tmp_ds = xr.zeros_like(self.ts.climatology_mean)
            tmp_ds.data = q_data[i]

            save_dask_array(fname=fname, data=tmp_ds,
                            data_var=var, method=None,
                            n_workers=4)

        # Save climatology and per-year standard anomalies
        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_climatology_mean.tif')
        save_dask_array(fname=fname, data=self.ts.climatology_mean,
                data_var=var, method=None, n_workers=4)
        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_climatology_std.tif')
        save_dask_array(fname=fname, data=self.ts.climatology_std,
                data_var=var, method=None, n_workers=4)

        self.progressBar.setValue(0)
        msg = f"Saving climatologies and per-year anomalies..."
        self.progressBar.setFormat(msg)

        grouped_by_year = self.ts.data[var].time.groupby("time.year")

        for i, (_year, _times) in enumerate(grouped_by_year):
            self.progressBar.setValue(int((i/len(grouped_by_year))*100))

            # Get time series for year
            ts_year = self.ts.data[var].sel(time=_times.data)

            # Anomalies (only for full years)
            if not len(ts_year.time) == n_time_steps:
                continue

            anomalies = (ts_year - self.ts.climatology_mean.data) \
                    / self.ts.climatology_std.data

            fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_anomalies{_year}.tif')

            anomalies.attrs = ts_year.attrs
            save_dask_array(fname=fname, data=anomalies,
                    data_var=var, method=None, n_workers=4)

        self.progressBar.setValue(0)
        self.progressBar.setEnabled(False)
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** Climatology *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    def on_pbOverlay_click(self):
        """
        EXPERIMENTAL
        Overlay a specific geometry on maps
        """
        fname = open_file_dialog(dialog_type = 'open_specific',
                data_format = 'Shapefile',
                extension = 'shp')

        # If there is no selection
        if fname == '':
            return None

        # If file does not exists
        if os.path.exists(fname) is False:
            return None

        # Open file
        spatial_reference = self.get_shapefile_spatial_reference(fname)

        # Get projection from first data variable
        for key in self.ts.data.data_vars:
            proj4_string = getattr(self.ts.data, key).crs
            break

        # If projection is Sinusoidal
        srs = get_projection(proj4_string)
        if srs.GetAttrValue('PROJECTION') == 'Sinusoidal':
            # Get ellipsoid/datum parameters
            globe=ccrs.Globe(ellipse=None,
                semimajor_axis=spatial_reference.GetSemiMajor(),
                semiminor_axis=spatial_reference.GetSemiMinor())

            self.shapefile_projection = ccrs.Sinusoidal(globe=globe)
            #ccrs.CRS(spatial_reference.ExportToProj4())
        else:
            globe = ccrs.Globe(ellipse='WGS84')
            self.shapefile_projection = ccrs.Mollweide(globe=globe)

        try:
            shape_feature = ShapelyFeature(cReader(fname).geometries(),
                self.shapefile_projection, facecolor='none')

            for _axis in [self.left_p, self.right_p]:
                _axis.add_feature(shape_feature,
                        edgecolor='gray')
        except:
            return None

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    @log("ficheroLog.log")
    def on_pbAnomalies_click(self):
        """
        Computes the climatolgy and anomalies for the selected year
        and shows the correspinding plot
        """
        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        start = time.time()
        tiempo_inicial = datetime.now()

        first_run = False

        # The climatology method will create two datasets:
        #   ts.climatology_mean
        #   ts.climatology_std
        if self.ts.climatology_mean is None and \
            self.ts.climatology_std is None:

            # Compute climatology
            self.ts.climatology()

            with ProgressBar():
                self.ts.climatology_mean = self.ts.climatology_mean.compute()
                self.ts.climatology_std = self.ts.climatology_std.compute()

            self.climatology_year = self.years.currentText()
            first_run = True

        if self.climatology_year is None or \
            self.climatology_year != self.years.currentText() or \
            first_run is True:

            if self.ts.climatology_mean.shape[0] != self.single_year_ds.shape[0]:
                # Standard cursor
                QtWidgets.QApplication.restoreOverrideCursor()

                message_text = (f'Year {self.years.currentText()} does not '
                                f'have same number of observations as the '
                                f'climatology. Anomalies cannot be computed.')
                self.message_box(message_text)
                return None

            # Anomalies
            anomalies = (self.single_year_ds - self.ts.climatology_mean.data) \
                            / self.ts.climatology_std.data

            with ProgressBar():
                self.anomalies = anomalies.compute()

            first_run = False

        self.__plotAnomalies()

        # Output file name
        #smoothing_method = self.smoothing_methods.selectedItems()[0].text()
        #_fname, _ext = os.path.splitext(self.fname)
        #output_fname = f'{_fname}.{smoothing_method}.tif'

        #smoother = Smoothing(fname=self.fname, output_fname=output_fname,
        #        smoothing_methods=[smoothing_method])

        #smoother.smooth()
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** Anomalies *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @pyqtSlot()
    def __plotAnomalies(self):
        dialog = PlotAnomalies(self)
        self.dialogs.append(dialog)
        dialog._plot(self.anomalies, self.years.currentText())
        dialog.show()

    def __fill_year(self):
        """
        Fill years list based on years in the dataset
        """
        # Get unique years from data
        times = getattr(self.ts.data, self.data_vars.currentText()).time
        times = np.unique(times.dt.year.data).tolist()

        # We need a list of strings to be added to the widget
        times = list(map(str, times))
        self.years.addItems(times)

        # Set default year as first full year
        if len(times) > 1:
            current_year = times[1]
            self.years.setCurrentIndex(1)
        else:
            current_year = times[0]

        time_slice = slice(f"{current_year}-01-01",
                           f"{current_year}-12-31",)

        self.single_year_ds = getattr(self.ts.data,
                self.data_vars.currentText()).sel(time=time_slice)

    def __fill_bandwidth(self):
        """
        Fill bandwidth combo box with 3 to max numbers of time steps
        in a single calendar year
        """
        n_obs_single_year = self.single_year_ds.shape[0]

        bandwidth = list(map(str, np.arange(3, n_obs_single_year + 1)))

        self.bandwidth.addItems(bandwidth)
        self.bandwidth.setCurrentIndex(len(bandwidth)-1)

    def __fill_model(self):
        """
        Fill time series decompostion model
        """
        _models = ['additive', 'multiplicative']

        return _models

    def __fill_data_variables(self):
        """
        Fill the data variables dropdown list
        """
        data_vars = []
        for data_var in self.ts.data.data_vars:
            data_vars.append(data_var)

        return data_vars

    def __fill_time_steps(self):
        """
        Fill the time steps dropdown list
        """
        time_steps = np.datetime_as_string(
                self.single_year_ds.time.data, 'm').tolist()

        return time_steps

    def on_click(self, event):
        """
        Event handler
        """
        # Event does not apply for time series plot
        if not type(event) == QtGui.QKeyEvent:
            if event.inaxes not in [self.left_p, self.right_p]:
                return

        # Clear subplots
        self.observed.clear()
        self.trend_p.clear()
        self.seasonal_p.clear()
        self.resid_p.clear()
        self.climatology.clear()

        # Delete last reference point
        if len(self.left_p.lines) > 0:
            del self.left_p.lines[0]
            del self.right_p.lines[0]

        # Check whether the x and y data are coming from the user or
        # from the map plot click event
        if type(event) == QtGui.QKeyEvent:
            location = self.txtLocation.text()
            # Validate coordinates and use the native CRS to plot data
            coords = validate_coordinates(location, self.left_ds)
            if coords[0] is False:
                message_text = (f'Coordinates {location} in WGS84 are not  '
                                f'within the bounding box of the data.')
                self.message_box(message_text)
                return None

            xdata = coords[1][0]
            ydata = coords[1][1]

        else:
            xdata = event.xdata
            ydata = event.ydata
            _xdata, _ydata = transform_to_wgs84(xdata, ydata,
                                 self.left_ds.crs)
            # Set location text
            self.txtLocation.setText(f'{_ydata:.9f},{_xdata:.9f}')

        # Draw a point as a reference
        self.left_p.plot(xdata, ydata,
                marker='o', color='red', markersize=7, alpha=0.7)
        self.right_p.plot(xdata, ydata,
                marker='o', color='red', markersize=7, alpha=0.7)

        # Non-masked data
        left_plot_sd = self.left_ds.sel(longitude=xdata,
                                        latitude=ydata,
                                        method='nearest')
        # Sinlge year dataset
        single_year_ds = self.single_year_ds.sel(longitude=xdata,
                                                 latitude=ydata,
                                                 method='nearest')

        if left_plot_sd.chunks is not None:
            left_plot_sd = left_plot_sd.compute()
            single_year_ds = single_year_ds.compute()

        ts_df = left_plot_sd.to_dataframe()

        # Mann-Kendall test
        _mk_test = mk_test(left_plot_sd.data, _round=3)

        # Observations + peaks and valleys
        self.observed.plot(left_plot_sd.time, left_plot_sd.data,
                label=f'Observed {_mk_test}')

        distance = int(np.ceil(len(self.single_year_ds.time) / 4))
        peaks, _ = find_peaks(left_plot_sd.data, distance=distance)
        self.observed.plot(left_plot_sd.time[peaks],
                left_plot_sd[peaks],
                label=f'Peaks [{peaks.shape[0]}]',
                marker='x', color='C1', alpha=0.3)

        valleys, _ = find_peaks(left_plot_sd.data*(-1), distance=distance)
        self.observed.plot(left_plot_sd.time[valleys],
                left_plot_sd[valleys],
                label=f'Valleys, [{valleys.shape[0]}]',
                marker='x', color='C2', alpha=0.3)

        # Seasonal decompose
        period = int(self.bandwidth.currentText())
        nobs = len(left_plot_sd)

        # TODO interpolate and extrapolate trend
        trend = left_plot_sd.rolling(time=period, min_periods=1,
                center=True).mean()
        self.trend_p.plot(left_plot_sd.time.data, trend.data,
                label=f'Trend (window = {period})')

        #   Check if data is monthly to group by month
        unique_diffs = np.sort(np.unique(np.diff(left_plot_sd.time.dt.month.values)))
        if unique_diffs[0] == -11 and unique_diffs[1] == 1:
            _idx = 'time.month'
        else:
            _idx = 'time.dayofyear'
        period_averages = left_plot_sd.groupby(_idx).mean()

        if self.model.currentText()[0] == 'a':
            period_averages -= period_averages.mean(axis=0)
            seasonal = np.tile(period_averages.T, nobs // period + 1).T[:nobs]
            resid = (left_plot_sd - trend) - seasonal
        else:
            period_averages /= period_averages.mean(axis=0)
            seasonal = np.tile(period_averages.T, nobs // period + 1).T[:nobs]
            resid = left_plot_sd / seasonal / trend

        self.seasonal_p.plot(left_plot_sd.time.data, seasonal,
                label='Seasonality')
        self.resid_p.plot(left_plot_sd.time.data, resid.data,
                label='Residuals')

        # Set the same y limits from observed data
        self.trend_p.set_ylim(self.observed.get_ylim())

        # Climatology
        #   check if data is monthly to group by month
        unique_diffs = np.sort(np.unique(np.diff(ts_df.index.month.values)))
        if unique_diffs[0] == -11 and unique_diffs[1] == 1:
            _idx = ts_df.index.month
        else:
            _idx = ts_df.index.dayofyear

        sbn.boxplot(_idx,
                ts_df[self.data_vars.currentText()], ax=self.climatology)
        # Plot year to analyse
        single_year_df = single_year_ds.to_dataframe()
        sbn.stripplot(x=single_year_df.index.dayofyear,
                y=single_year_df[self.data_vars.currentText()],
                color='red', marker='o', size=7, alpha=0.7,
                ax=self.climatology)

        self.climatology.tick_params(axis='x', rotation=70)

        # Change point
        r_vector = FloatVector(trend.data)
        #changepoint_r = self.cpt.cpt_mean(r_vector)
        #changepoints_r = self.cpt.cpt_var(r_vector, method='PELT',
        #        penalty='Manual', pen_value='2*log(n)')
        changepoints_r = self.cpt.cpt_meanvar(r_vector,
                test_stat='Normal', method='BinSeg', penalty="SIC")
        changepoints = numpy2ri.rpy2py(self.cpt.cpts(changepoints_r))

        if changepoints.shape[0] > 0:
            # Plot vertical line where the changepoint was found
            for i, i_cpt in enumerate(changepoints):
                i_cpt = int(i_cpt) + 1
                cpt_index = self.seasonal_decompose.trend.index[i_cpt]
                if i == 0 :
                    self.trend_p.axvline(cpt_index, color='black',
                            lw='1.0', label='Change point')
                else:
                    self.trend_p.axvline(cpt_index, color='black', lw='1.0')

        # Legend
        self.observed.legend(loc='best', fontsize='small',
                fancybox=True, framealpha=0.5)
        self.observed.set_title('Time series decomposition')
        self.trend_p.legend(loc='best', fontsize='small',
                fancybox=True, framealpha=0.5)
        self.seasonal_p.legend(loc='best', fontsize='small',
                fancybox=True, framealpha=0.5)
        self.resid_p.legend(loc='best', fontsize='small',
                fancybox=True, framealpha=0.5)

        #self.climatology.legend([self.years.value], loc='best',
        self.climatology.legend(loc='best',
                fontsize='small', fancybox=True, framealpha=0.5)

        self.climatology.set_title('Climatology')

        # Grid
        self.observed.grid(axis='both', alpha=.3)
        self.trend_p.grid(axis='both', alpha=.3)
        self.seasonal_p.grid(axis='both', alpha=.3)
        self.resid_p.grid(axis='both', alpha=.3)
        self.climatology.grid(axis='both', alpha=.3)

        # Redraw plot
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    @pyqtSlot(int)
    def __on_years_change(self,  index):
        """
        Handles a change in the years to display
        """
        if len(self.years.currentText()) == 0 or \
                self.left_imshow is None or \
                self.right_imshow is None:
            return None

        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)

        current_year = self.years.currentText()

        time_slice = slice(f"{current_year}-01-01",
                           f"{current_year}-12-31",)
        self.single_year_ds = getattr(self.ts.data,
                self.data_vars.currentText()).sel(time=time_slice)

        # Update time steps
        self.time_steps_left.clear() ; self.time_steps_right.clear()
        self.time_steps_left.addItems(self.__fill_time_steps())
        self.time_steps_right.addItems(self.__fill_time_steps())

        # Update images with current year
        self.__update_imshow()

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @pyqtSlot(int)
    def __on_time_steps_left_change(self, index):
        """
        Handles a change in the time step to display
        """
        if len(self.time_steps_left.currentText()) == 0 or \
                self.left_imshow is None:
            return None

        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)

        self.left_imshow.set_data(self.single_year_ds.data[index])

        # Set titles
        self.left_p.set_title(self.time_steps_left.currentText())

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @pyqtSlot(int)
    def __on_time_steps_right_change(self, index):
        """
        Handles a change in the time step to display
        """
        if len(self.time_steps_right.currentText()) == 0 or \
                self.right_imshow is None:
            return None

        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)

        self.right_imshow.set_data(self.single_year_ds.data[index])

        # Set titles
        self.right_p.set_title(self.time_steps_right.currentText())

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    def __update_imshow(self):
        """
        Update images shown as imshow plots
        """
        self.left_imshow = self.single_year_ds[0].plot.imshow(
                cmap=self.cmap, ax=self.left_p, add_colorbar=False,
                transform=self.projection)

        self.right_imshow = self.single_year_ds[0].plot.imshow(
                cmap=self.cmap, ax=self.right_p, add_colorbar=False,
                transform=self.projection)

        self.left_p.set_aspect('equal')
        self.right_p.set_aspect('equal')

    def __populate_plots(self):
        """
        Populate plots
        """
        # Create plot
        self.left_ds = getattr(self.ts.data, self.data_vars.currentText())
        self.right_ds = getattr(self.ts.data, self.data_vars.currentText())

        # Show images
        self.__update_imshow()

        # Turn off axis
        self.left_p.axis('off')
        self.right_p.axis('off')
        self.fig.canvas.draw_idle()

        # Connect the canvas with the event
        cid = self.fig.canvas.mpl_connect('button_press_event',
                                          self.on_click)

        # Plot the centroid
        _layers, _rows, _cols = self.left_ds.shape

        # Seasonal decompose
        left_plot_sd = self.left_ds[:, int(_rows / 2), int(_cols / 2)]
        ts_df = left_plot_sd.to_dataframe()
        self.seasonal_decompose = seasonal_decompose(
                ts_df[self.data_vars.currentText()],
                model=self.model.currentText(),
                freq=self.single_year_ds.shape[0],
                extrapolate_trend='freq')

        # Plot seasonal decompose
        self.seasonal_decompose.observed.plot(ax=self.observed)
        self.seasonal_decompose.trend.plot(ax=self.trend_p)
        self.seasonal_decompose.seasonal.plot(ax=self.seasonal_p)
        self.seasonal_decompose.resid.plot(ax=self.resid_p)

        # Climatology
        #   check if data is monthly to group by month
        unique_diffs = np.sort(np.unique(np.diff(ts_df.index.month.values)))
        if unique_diffs[0] == -11 and unique_diffs[1] == 1:
            _idx = ts_df.index.month
        else:
            _idx = ts_df.index.dayofyear

        sbn.boxplot(_idx,
                ts_df[self.data_vars.currentText()],
                ax=self.climatology)
        self.climatology.tick_params(axis='x', rotation=70)

        # Needed in order to use a tight layout with Cartopy axes
        self.fig.canvas.draw()
        plt.tight_layout()

    def __create_plot_objects(self):
        """
        Create plot objects
        """
        years_fmt = mdates.DateFormatter('%Y')

        # Get projection from first data variable
        for key in self.ts.data.data_vars:
            proj4_string = getattr(self.ts.data, key).crs
            break

        # If projection is Sinusoidal
        srs = get_projection(proj4_string)
        if srs.GetAttrValue('PROJECTION') == 'Sinusoidal':
            globe=ccrs.Globe(ellipse=None,
                semimajor_axis=6371007.181,
                semiminor_axis=6371007.181)

            self.projection = ccrs.Sinusoidal(globe=globe)
        else:
            globe = ccrs.Globe(ellipse='WGS84')
            self.projection = ccrs.Mollweide(globe=globe)

        # Figure
        self.fig = plt.figure(figsize=(11.0, 6.0))

        # subplot2grid((rows,cols), (row,col)
        # Left, right and climatology plots
        self.left_p = plt.subplot2grid((4, 4), (0, 0), rowspan=2,
                projection=self.projection)
        self.right_p = plt.subplot2grid((4, 4), (0, 1), rowspan=2,
                sharex=self.left_p, sharey=self.left_p,
                projection=self.projection)

        if self.projection is not None:
            for _axis in [self.left_p, self.right_p]:
                _axis.coastlines(resolution='10m', color='white')
                _axis.add_feature(cfeature.BORDERS, edgecolor='white')
                _axis.gridlines()

        self.climatology = plt.subplot2grid((4, 4), (2, 0),
                rowspan=2, colspan=2)

        # Time series plot
        self.observed = plt.subplot2grid((4, 4), (0, 2), colspan=2)
        self.observed.xaxis.set_major_formatter(years_fmt)

        self.trend_p = plt.subplot2grid((4, 4), (1, 2), colspan=2,
                sharex=self.observed)
        self.seasonal_p = plt.subplot2grid((4, 4), (2, 2), colspan=2,
                sharex=self.observed)
        self.resid_p = plt.subplot2grid((4, 4), (3, 2), colspan=2,
                sharex=self.observed)

    def _plot(self, cmap='viridis', dpi=72):
        """
        From the TATSSI Time Series Analysis object plots:
            - A single time step of the selected variable
            - Per-pixel time series with user selected smoothing
        """
        # Default colormap
        self.cmap = cmap

        # Set plot variables
        self.__set_variables()

        # Set plot on the plot widget
        self.plotWidget = FigureCanvas(self.fig)
        # Set focus
        self.plotWidget.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.plotWidget.setFocus()
        # Connect the canvas with the event
        self.plotWidget.mpl_connect('button_press_event',
                self.on_click)

        lay = QtWidgets.QVBoxLayout(self.content_plot)
        lay.setContentsMargins(0, 70, 0, 0)
        lay.addWidget(self.plotWidget)

        # Add toolbar
        font = QFont()
        font.setPointSize(12)

        toolbar = NavigationToolbar(self.plotWidget, self)
        toolbar.setFont(font)

        self.addToolBar(QtCore.Qt.BottomToolBarArea, toolbar)

    @log("ficheroLog.log")
    def on_pbCPD_click(self):
        """
        Compute change points on the detrended time series
        """
        # Wait cursor
        QtWidgets.QApplication.setOverrideCursor(Qt.WaitCursor)
        start = time.time()
        tiempo_inicial = datetime.now()

        msg = f"Identifying change points..."
        self.progressBar.setEnabled(True)
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        # Compute first the trend
        period = int(self.bandwidth.currentText())
        nobs = len(self.left_ds)

        # Get data type
        dtype = self.left_ds.dtype

        # Get trend based on a moving window
        trend = self.left_ds.rolling(time=period, min_periods=1,
                center=True).mean().astype(dtype)
        trend = trend.compute()
        trend.attrs = self.left_ds.attrs

        # Output data
        output = xr.zeros_like(trend).astype(np.int16).load()
        output.attrs = self.left_ds.attrs

        layers, rows, cols = trend.shape

        for x in range(cols):
            self.progressBar.setValue(int((x / cols) * 100))
            for y in range(rows):
                _data = trend[:,y,x]
                r_vector = FloatVector(_data)

                #changepoint_r = self.cpt.cpt_mean(r_vector)
                #changepoints_r = self.cpt.cpt_var(r_vector, method='PELT',
                #        penalty='Manual', pen_value='2*log(n)')

                # CPD methods
                _method = 'BinSeg'
                _penalty = 'SIC'

                changepoints_r = self.cpt.cpt_meanvar(r_vector,
                        test_stat='Normal', method=_method, penalty=_penalty)

                changepoints = numpy2ri.rpy2py(
                        self.cpt.cpts(changepoints_r)).astype(int)

                if changepoints.shape[0] > 0:
                    output[changepoints+1, y, x] = True

        fname = (f'{os.path.splitext(self.fname)[0]}'
                     f'_change_points.tif')

        msg = f"Saving change points..."
        self.progressBar.setFormat(msg)
        self.progressBar.setValue(1)

        save_dask_array(fname=fname, data=output,
                data_var=self.data_vars.currentText(), method=None,
                n_workers=4, progressBar=self.progressBar)

        self.progressBar.setValue(0)
        self.progressBar.setEnabled(False)
        end = time.time()
        tiempo_final= datetime.now()
        duracion=(end - start)/60.0
        print("************** CPD *********")
        print("Start:", tiempo_inicial," End: ", tiempo_final, " @minutes: ", duracion)

        # Standard cursor
        QtWidgets.QApplication.restoreOverrideCursor()

    @staticmethod
    def message_box(message_text):
        dialog = QtWidgets.QMessageBox()
        dialog.setIcon(QtWidgets.QMessageBox.Critical)
        dialog.setText(message_text)
        dialog.addButton(QtWidgets.QMessageBox.Ok)
        dialog.exec()

        return None

    @staticmethod
    def get_shapefile_spatial_reference(fname):
        driver = ogr.GetDriverByName('ESRI Shapefile')
        dataset = driver.Open(fname)

        layer = dataset.GetLayer()
        spatialRef = layer.GetSpatialRef()

        return spatialRef

if __name__ == "__main__":

    fname = '/home/glopez/Projects/TATSSI/data/MOD13A2.006/1_km_16_days_EVI/interpolated/MOD13A2.006._1_km_16_days_EVI.linear.smoothn.int16.tif'

    app = QtWidgets.QApplication([])
    window = TimeSeriesAnalysisUI(fname=fname)
    app.exec_()

