from contextlib import contextmanager
from echo import delay_callback, CallbackProperty
import numpy as np

from astropy import units as u
from glue.viewers.profile.state import ProfileViewerState
from glue_jupyter.bqplot.image.state import BqplotImageViewerState
from glue.viewers.matplotlib.state import DeferredDrawCallbackProperty as DDCProperty

from jdaviz.utils import get_reference_image_data
from jdaviz.core.unit_conversion_utils import (all_flux_unit_conversion_equivs,
                                               flux_conversion_general,
                                               spectral_axis_conversion)

__all__ = ['FreezableState', 'FreezableProfileViewerState', 'FreezableBqplotImageViewerState']


class FreezableState:
    _frozen_state = []

    def __setattr__(self, k, v):
        if k[0] == '_' or k not in self._frozen_state:
            super().__setattr__(k, v)
        elif getattr(self, k) is None:
            # still allow Nones to be updated to initial values
            super().__setattr__(k, v)


class FreezableProfileViewerState(ProfileViewerState, FreezableState):
    show_uncertainty = DDCProperty(False, docstring='Whether to show data uncertainties')

    def _reset_x_limits(self, *event):
        # override glue's _reset_x_limits to account for all layers,
        # not just reference data (_reset_y_limits already does so)
        # This is essentially copied directly from Glue's
        # ProfileViewerState._reset_y_limits but modified for x-limits
        if self.reference_data is None or self.x_att_pixel is None:
            return

        x_min, x_max = np.inf, -np.inf
        for layer in self.layers:
            try:
                profile = layer.profile
            except Exception:  # nosec
                # e.g. incompatible subset
                continue
            if profile is not None:
                x, y = profile
                if len(x) > 0:
                    x_min = min(x_min, np.nanmin(x))
                    x_max = max(x_max, np.nanmax(x))

        if not np.all(np.isfinite([x_min, x_max])):
            return

        with delay_callback(self, 'x_min', 'x_max'):
            self.x_min = x_min
            self.x_max = x_max

    def _convert_units_x_limits(self, old_unit, new_unit):
        # override glue's _convert_units_x_limits to account
        # for spectral axis conversions that are not supported by glue.

        if self.x_min is None or self.x_max is None:
            return

        if old_unit is None or new_unit is None:
            self._reset_x_limits()
            return

        x_lims_new = spectral_axis_conversion([self.x_min, self.x_max],
                                              old_unit, new_unit)

        self.x_min = np.nanmin(x_lims_new)
        self.x_max = np.nanmax(x_lims_new)

    def _convert_units_y_limits(self, old_unit, new_unit):
        # override glue's _convert_units_y_limits to account
        # for equivalencies.  This converts all four corners
        # of the limits to set new limits that contain those
        # same corners

        if old_unit != new_unit:
            if old_unit is None or new_unit is None:
                self._reset_y_limits()
                return
            x_corners = np.array([self.x_min, self.x_min, self.x_max, self.x_max])
            y_corners = np.array([self.y_min, self.y_max, self.y_min, self.y_max])
            spectral_axis = x_corners * u.Unit(self.x_display_unit)

            # NOTE: this uses the scale factor from the first found layer.  We may want to
            # generalize this to iterate over all scale factors if/when we support multiple
            # flux cubes (with potentially different pixel scale factors).
            eqv = None
            for layer in self.layers:
                if psc := getattr(layer.layer, 'meta', {}).get('_pixel_scale_factor', None):  # noqa
                    spectral_axis.info.meta = {'_pixel_scale_factor',
                                               psc}
                    eqv = all_flux_unit_conversion_equivs(pixar_sr=psc,
                                                          cube_wave=spectral_axis)
                    break
            else:
                spectral_axis.info.meta = {}
                eqv = all_flux_unit_conversion_equivs(cube_wave=spectral_axis)
                spectral_axis = None

            y_corners_new = flux_conversion_general(y_corners, old_unit, new_unit, eqv, with_unit=False)  # noqa

            with delay_callback(self, 'y_min', 'y_max'):
                self.y_min = np.nanmin(y_corners_new)
                self.y_max = np.nanmax(y_corners_new)


class FreezableBqplotImageViewerState(BqplotImageViewerState, FreezableState):
    linked_by_wcs = False

    zoom_radius = CallbackProperty(1.0, docstring="Zoom radius")
    zoom_center_x = CallbackProperty(0.0, docstring='x-coordinate of center of zoom box')
    zoom_center_y = CallbackProperty(0.0, docstring='y-coordinate of center of zoom box')

    def __init__(self, *args, **kwargs):
        self.wcs_only_layers = []  # For Imviz rotation use.
        self._during_zoom_sync = False
        self.add_callback('zoom_radius', self._set_zoom_radius_center)
        self.add_callback('zoom_center_x', self._set_zoom_radius_center)
        self.add_callback('zoom_center_y', self._set_zoom_radius_center)
        for attr in ('x_min', 'x_max', 'y_min', 'y_max'):
            self.add_callback(attr, self._set_axes_lim)
        super().__init__(*args, **kwargs)

    def _set_viewer(self, viewer):
        self._viewer = viewer
        self._set_axes_lim()

    @contextmanager
    def during_zoom_sync(self):
        self._during_zoom_sync = True
        try:
            yield
        except Exception:
            self._during_zoom_sync = False
            raise
        self._during_zoom_sync = False

    def _set_zoom_radius_center(self, *args):
        if self._during_zoom_sync or not hasattr(self, '_viewer'):
            return

        # When WCS-linked (displayed on the sky): zoom_center_x/y and zoom_radius are in sky units,
        # x/y_min/max are in pixels of the WCS-only layer
        if self.linked_by_wcs:
            image, i_ref = get_reference_image_data(self._viewer.jdaviz_app, self._viewer.reference)
            ref_wcs = image.coords
            cr = ref_wcs.world_to_pixel_values((self.zoom_center_x, self.zoom_center_x+abs(self.zoom_radius)),  # noqa
                                               (self.zoom_center_y, self.zoom_center_y))
            center_x, center_xr = cr[0]
            center_y, _ = cr[1]
            radius = abs(center_xr - center_x)
        else:
            center_x, center_y = self.zoom_center_x, self.zoom_center_y
            radius = abs(self.zoom_radius)
        # now center_x/y and radius are in pixel units of the reference data, so can be used to
        # update limits

        with self.during_zoom_sync():
            x_min = center_x - radius
            x_max = center_x + radius
            y_min = center_y - radius
            y_max = center_y + radius
            self.x_min, self.x_max, self.y_min, self.y_max = x_min, x_max, y_min, y_max

            self._adjust_limits_aspect()

    def _set_axes_aspect_ratio(self, axes_ratio):
        # when aspect-ratio is changed (changing viewer.shape), ensure zoom/center are synced
        # with zoom-limits
        super()._set_axes_aspect_ratio(axes_ratio)
        self._set_axes_lim()

    def _set_axes_lim(self, *args):
        if self._during_zoom_sync or not hasattr(self, '_viewer'):
            return
        if None in (self.x_min, self.x_max, self.y_min, self.y_max):
            return

        # When WCS-linked (displayed on the sky): zoom_center_x/y and zoom_radius are in sky units,
        # x/y_min/max are in pixels of the WCS-only layer
        if self.linked_by_wcs:
            image, i_ref = get_reference_image_data(self._viewer.jdaviz_app, self._viewer.reference)
            ref_wcs = image.coords
            lims = ref_wcs.pixel_to_world_values((self.x_min, self.x_max), (self.y_min, self.y_max))
            x_min, x_max = lims[0]
            y_min, y_max = lims[1]
        else:
            x_min, y_min = self.x_min, self.y_min
            x_max, y_max = self.x_max, self.y_max
        # now x_min/max, y_min/max are in axes units (degrees if WCS-linked, pixels otherwise)

        with self.during_zoom_sync():
            self.zoom_radius = abs(0.5 * min(x_max - x_min, y_max - y_min))
            self.zoom_center_x = 0.5 * (x_max + x_min)
            self.zoom_center_y = 0.5 * (y_max + y_min)

    def _get_reset_limits(self, return_as_world=False):
        wcs_success = False
        if self.linked_by_wcs and self.reference_data.coords is not None:
            x_min, x_max = np.inf, -np.inf
            y_min, y_max = np.inf, -np.inf

            for layer in self.layers:
                if not layer.visible:
                    continue

                data = next((x for x in self.data_collection if x.label == layer.layer.data.label))
                if data.coords is None:
                    # if no layers have coords, then wcs_success will remain
                    # false and limits will fallback based on pixel limit
                    continue

                pixel_ids = layer.layer.pixel_component_ids
                world_bottom_left = data.coords.pixel_to_world(0, 0)
                world_top_right = data.coords.pixel_to_world(
                    layer.layer.data.shape[pixel_ids[1].axis] - 1,
                    layer.layer.data.shape[pixel_ids[0].axis] - 1
                )

                if return_as_world:
                    x_min = min(x_min, world_bottom_left.ra.value)
                    x_max = max(x_max, world_top_right.ra.value)
                    y_min = min(y_min, world_bottom_left.dec.value)
                    y_max = max(y_max, world_top_right.dec.value)
                else:
                    x_bl, y_bl = self.reference_data.coords.world_to_pixel(world_bottom_left)
                    x_tr, y_tr = self.reference_data.coords.world_to_pixel(world_top_right)

                    x_pix_min = min(x_bl, x_tr)
                    x_pix_max = max(x_bl, x_tr)
                    y_pix_min = min(y_bl, y_tr)
                    y_pix_max = max(y_bl, y_tr)

                    x_min = min(x_min, x_pix_min - 0.5)
                    x_max = max(x_max, x_pix_max + 0.5)
                    y_min = min(y_min, y_pix_min - 0.5)
                    y_max = max(y_max, y_pix_max + 0.5)
                wcs_success = True

        if not wcs_success:
            x_min, x_max = -0.5, -np.inf
            y_min, y_max = -0.5, -np.inf
            for layer in self.layers:
                if not layer.visible or layer.layer.data.ndim == 1:
                    continue
                pixel_ids = layer.layer.pixel_component_ids
                pixel_id_x = [comp for comp in pixel_ids if comp.label.endswith('[x]')][0]
                pixel_id_y = [comp for comp in pixel_ids if comp.label.endswith('[y]')][0]

                x_max = max(x_max, layer.layer.data.shape[pixel_id_x.axis] - 0.5)
                y_max = max(y_max, layer.layer.data.shape[pixel_id_y.axis] - 0.5)

        return x_min, x_max, y_min, y_max

    def reset_limits(self, *event):
        # TODO: use consistent logic for all image viewers by removing this if-statement
        # if/when WCS linking is supported (i.e. in cubeviz)
        if getattr(self, '_viewer', None) is not None and self._viewer.jdaviz_app.config != 'imviz':
            return super().reset_limits(*event)
        if self.reference_data is None:  # Nothing to do
            return

        x_min, x_max, y_min, y_max = self._get_reset_limits()

        # If any bound wasn't set to a real value, don't update
        if np.any(~np.isfinite([x_min, x_max, y_min, y_max])):
            return

        with delay_callback(self, 'x_min', 'x_max', 'y_min', 'y_max'):
            self.x_min, self.x_max, self.y_min, self.y_max = x_min, x_max, y_min, y_max
            # We need to adjust the limits in here to avoid triggering all
            # the update events then changing the limits again.
            self._adjust_limits_aspect()
