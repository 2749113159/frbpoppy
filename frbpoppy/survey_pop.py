"""Class to generate a survey population of FRBs."""
from copy import deepcopy
from tqdm import tqdm
import numpy as np

from frbpoppy.misc import pprint
from frbpoppy.population import Population
from frbpoppy.rates import Rates


class SurveyPopulation(Population):
    """Class to create a survey population of FRBs."""

    def __init__(self, cosmic_pop, survey, scat=False, scin=False,
                 mute=False, scale_by_area=True):
        """
        Run a survey to detect FRB sources.

        Args:
            cosmic_pop (Population): Population class of FRB sources to observe
            survey (Survey): Survey class with which to observe
            scat (bool, optional): Whether to include scattering in signal to
                noise calculations.
            scin (bool, optional): Whether to apply scintillation to
                observations.
            mute (bool): Whether to suppress printing to terminal
            scale_by_area (bool): Whether to scale detection rates to the sky
                area visible to a survey. Only relevant for one-offs.
        """
        if not mute:
            pprint(f'Surveying {cosmic_pop.name} with {survey.name}')
        # Stops RuntimeWarnings about nan values
        np.warnings.filterwarnings('ignore')

        # Check whether CosmicPopulation has been generated
        try:
            if cosmic_pop.frbs.ra is None:
                m = 'You may have forgotten to generate your CosmicPopulation'
                raise ValueError(m)
        except AttributeError:
            m = 'You probably switched the population and survey in the'
            m += 'input of SurveyPopulation'
            raise ValueError(m)
        # Set up population
        Population.__init__(self)

        # Set attributes
        self.name = f'{cosmic_pop.name}_{survey.name}'
        self.vol_co_max = cosmic_pop.vol_co_max
        self.n_days = cosmic_pop.n_days
        self.repeaters = cosmic_pop.repeaters
        self.frbs = deepcopy(cosmic_pop.frbs)
        self.source_rate = Rates('source')
        if self.repeaters:
            self.burst_rate = Rates('burst')
        self.scat = scat
        self.scin = scin
        self.survey = survey
        self.scale_by_area = scale_by_area

        # Set survey attributes if not available
        if survey.n_days is None:
            survey.n_days = self.n_days

        # Calculations differ for repeaters
        if self.repeaters is True and scin is True:
            m = 'Scintillation is currently not implemented for '
            m += 'RepeaterPopulations'
            raise ValueError(m)

        # For convenience
        frbs = self.frbs
        sr = self.source_rate
        sr.tot = cosmic_pop.n_srcs
        if self.repeaters:
            br = self.burst_rate
            br.tot = self.frbs.time.size

            # Bursts which are too late have already been removed
            self.n_brst_pr_src = np.count_nonzero(~np.isnan(self.frbs.time), 1)
            br.late += br.tot - np.sum(self.n_brst_pr_src)
            sr.late += sr.tot - len(self.n_brst_pr_src)

        # Check whether source is in region
        region_mask = survey.in_region(frbs.ra, frbs.dec, frbs.gl, frbs.gb)
        frbs.apply(region_mask)

        # Keep track of detection numbers
        sr.out = np.sum(~region_mask)
        if self.repeaters:
            br.out = np.sum(self.n_brst_pr_src[~region_mask])
            self.n_brst_pr_src = self.n_brst_pr_src[region_mask]

        # Calculate dispersion measure across single channel
        frbs.t_dm = survey.calc_dm_smear(frbs.dm)

        # Set scattering timescale
        if scat:
            frbs.t_scat = survey.calc_scat(frbs.dm)

        # Calculate total temperature
        frbs.T_sky, frbs.T_sys = survey.calc_Ts(frbs.gl, frbs.gb)

        # Calculate effective pulse width
        frbs.w_eff = survey.calc_w_eff(frbs.w_arr, frbs.t_dm, frbs.t_scat)

        # Calculate peak flux density
        frbs.s_peak = survey.calc_s_peak(frbs.si,
                                         frbs.lum_bol,
                                         frbs.z,
                                         frbs.dist_co,
                                         frbs.w_arr,
                                         frbs.w_eff,
                                         f_low=cosmic_pop.f_min,
                                         f_high=cosmic_pop.f_max)

        # Calculations differ whether dealing with repeaters or not
        if self.repeaters:
            self.det_repeaters()
        else:
            self.det_oneoffs()

        # Prevent additional memory usage
        self.survey = None

    def det_oneoffs(self):
        """Detect one-off frbs."""
        frbs = self.frbs
        survey = self.survey

        # Account for beam offset
        int_pro, offset = survey.calc_beam(shape=frbs.s_peak.shape)
        frbs.s_peak *= int_pro
        frbs.offset = offset  # [deg]

        # Calculate fluence [Jy*ms]
        frbs.fluence = survey.calc_fluence(frbs.s_peak, frbs.w_eff)

        # Calculate Signal to Noise Ratio
        frbs.snr = survey.calc_snr(frbs.s_peak, frbs.w_eff, frbs.T_sys)

        # Add scintillation
        if self.scin:

            # Ensure scattering has been calculated
            if not isinstance(frbs.t_scat, np.ndarray):
                frbs.t_scat = survey.calc_scat(frbs.dm)

            # Calculate signal to noise ratio after scattering
            frbs.snr = survey.calc_scint(frbs.t_scat, frbs.dist_co, frbs.gl,
                                         frbs.gb, frbs.snr)

        # Check whether frbs would be above detection threshold
        snr_mask = (frbs.snr >= survey.snr_limit)
        frbs.apply(snr_mask)
        self.source_rate.faint = len(snr_mask) - np.count_nonzero(snr_mask)

        # Distant frbs are redshifted out of your observing time
        limit = 1/(1+frbs.z)
        rate_mask = np.random.random(len(frbs.z)) <= limit
        frbs.apply(rate_mask)
        self.source_rate.late = np.size(rate_mask)
        self.source_rate.late -= np.count_nonzero(rate_mask)

        self.source_rate.det = len(frbs.snr)

        # Calculate detection rates
        if self.scale_by_area:
            self.calc_rates(survey)

    def det_repeaters(self):
        """Detect repeating frbs."""
        frbs = self.frbs
        survey = self.survey
        br = self.burst_rate
        sr = self.source_rate

        # Set up a tuple of pointings if not given
        survey.gen_pointings()

        # t_obs in fractional days
        t_obs = survey.t_obs / 86400

        # Array with times of each pointing
        max_t = survey.n_days
        times = np.arange(0, max_t+t_obs, t_obs)  # [days]
        lsts = times*360*(24/23.9344696) % 360  # Local sidereal time [deg]
        lsts += np.random.uniform(0, 360)  # Add random offset

        # Only keep bursts within survey time
        time_mask = (frbs.time <= times[-1])
        # n_brst_pr_src = np.count_nonzero(~np.isnan(self.frbs.time), 1)
        frbs.apply(time_mask)

        # Keep track of losses
        br.late += np.sum(self.n_brst_pr_src - np.count_nonzero(time_mask, 1))
        sr.late += len(time_mask) - len(self.frbs.time)

        # Prepare for iterating over time
        max_n_pointings = len(times) - 1

        # Initialize some necessary arrays
        if frbs.w_eff.ndim == 2 or frbs.lum_bol.ndim == 2:
            sim_shape = frbs.time  # 2D
        else:
            sim_shape = frbs.lum_bol  # 1D

        frbs.fluence = np.full_like(sim_shape, np.nan)
        frbs.snr = np.full_like(sim_shape, np.nan)

        # Have to loop over the observing times
        ra_p = survey.pointings[0]
        dec_p = survey.pointings[1]
        lst = lsts[:-1]
        self.srcs_not_in_pointing = np.ones_like(frbs.index, dtype=bool)
        self.srcs_not_bright = np.ones_like(frbs.index, dtype=bool)

        # Parameters needed for for-loop
        keep = ([], [])
        for i in tqdm(np.arange(max_n_pointings), desc='Pointings'):
            xy = self._iter_pointings(ra_p[i % survey.n_pointings],
                                      dec_p[i % survey.n_pointings],
                                      lst[i],
                                      times[i],
                                      times[i+1])
            keep[0].extend(xy[0])
            keep[1].extend(xy[1])

        sr.pointing = np.sum(self.srcs_not_in_pointing)
        # Already outside of pointing, so unknown whether too faint
        self.srcs_not_bright[self.srcs_not_in_pointing] = False

        sr.faint = np.sum(self.srcs_not_bright)

        # Create SNR mask
        snr_mask = np.zeros_like(frbs.snr, dtype=bool)
        snr_mask[keep] = True
        frbs.apply(snr_mask)

        # Keep track of detections
        self.burst_rate.det = np.count_nonzero(snr_mask)
        self.source_rate.det = len(np.unique(keep[0]))

        # Reduce matrices' size
        frbs.clean_up()

        # Calculate detection rates
        self.calc_rates(survey)

    def _iter_pointings(self, ra_pt, dec_pt, lst, t_min, t_max):
        frbs = self.frbs
        survey = self.survey

        # Which frbs are within the pointing time?
        # Essential that each row is sorted from low to high!
        # Returns col, row index arrays
        t_ix = fast_where(frbs.time, t_min, t_max)

        # What's the intensity of them in the beam?
        int_pro, dx, dy = survey.calc_beam(repeaters=True,
                                           ra=frbs.ra[t_ix[0]],
                                           dec=frbs.dec[t_ix[0]],
                                           ra_p=ra_pt,
                                           dec_p=dec_pt,
                                           lst=lst)

        # If not an intensity of zero, they were inside the beam pattern
        p_ix = ~np.isnan(int_pro)

        # Time & position
        tp_ix = (t_ix[0][p_ix], t_ix[1][p_ix])

        # Time & not position
        tnp_ix = (t_ix[0][~p_ix], t_ix[1][~p_ix])

        # Number of bursts per source
        tp_unique, n_bursts = np.unique(tp_ix[0], return_counts=True)

        # Add to outside of pointing count
        self.burst_rate.pointing += len(tnp_ix[0])
        self.srcs_not_in_pointing[tp_unique] = False

        # Ensure relevant masks
        s_peak_ix = tp_unique
        not_ix = np.unique(tnp_ix[0])
        int_pro = int_pro[p_ix]

        if frbs.s_peak.ndim == 2:
            s_peak_ix = tp_ix
            not_ix = tnp_ix
        if frbs.s_peak.ndim == 1:
            int_pro = int_pro[tp_unique]

        # Apply intensities to those bursts' s_peak
        frbs.s_peak[s_peak_ix] *= int_pro
        frbs.s_peak[not_ix] = np.nan

        w_eff_ix = tp_unique
        if frbs.w_eff.ndim == 2:
            w_eff_ix = tp_ix

        # Ensure dimensionality is correct
        s_peak = frbs.s_peak[s_peak_ix]
        w_eff = frbs.w_eff[w_eff_ix]
        if frbs.w_eff.ndim < frbs.s_peak.ndim:
            w_eff = np.repeat(w_eff, n_bursts)
        elif frbs.w_eff.ndim > frbs.s_peak.ndim:
            s_peak = np.repeat(s_peak, n_bursts)

        # Calculate fluence [Jy*ms]
        frbs.fluence[s_peak_ix] = survey.calc_fluence(s_peak, w_eff)

        # Construct masks with correct dimension
        if frbs.w_arr.ndim == 2:
            w_arr = frbs.w_arr[tp_ix]
        elif frbs.w_arr.ndim == 1:
            w_arr = frbs.w_arr[tp_unique]

        # Ensure entries are repeated for the number of bursts
        s_peak = frbs.s_peak[s_peak_ix]
        if frbs.w_arr.ndim < frbs.s_peak.ndim:
            w_arr = np.repeat(w_arr, n_bursts)
        elif frbs.w_arr.ndim > frbs.s_peak.ndim:
            s_peak = np.repeat(s_peak, n_bursts)

        # And system temperatures
        if frbs.T_sys.ndim == 1:
            if frbs.s_peak.ndim == 2:
                T_sys = np.repeat(frbs.T_sys[tp_unique], n_bursts)
            elif frbs.s_peak.ndim == 1:
                T_sys = frbs.T_sys[tp_unique]
        elif frbs.T_sys.ndim == 0:
            T_sys = frbs.T_sys

        # Caculate Signal to Noise Ratio
        frbs.snr[s_peak_ix] = survey.calc_snr(s_peak, w_arr, T_sys)

        # Add scintillation
        if self.scin:
            # Not been fully tested with repeaters
            # Might break due to differing dimensionality of dist and snr
            t_scat = frbs.t_scat[tp_unique]
            dist_co = frbs.dist_co[tp_unique]
            gl = frbs.gl[tp_unique]
            gb = frbs.gb[tp_unique]
            snr = frbs.snr[s_peak_ix]
            new_snr = survey.calc_scint(t_scat, dist_co, gl, gb, snr)
            frbs.snr[s_peak_ix] = np.repeat(new_snr, n_bursts)

        # Only keep those in time, in position and above the snr limit
        snr_m = (frbs.snr[s_peak_ix] > survey.snr_limit)
        s_peak_ix = (tp_ix[0][snr_m], tp_ix[1][snr_m])

        self.burst_rate.faint += len(tp_ix[0]) - len(s_peak_ix[0])
        self.srcs_not_bright[np.unique(s_peak_ix[0])] = False

        return s_peak_ix[0], s_peak_ix[1]

    def calc_rates(self, survey):
        """Calculate the relative detection rates."""
        # Calculate scaling factors for rates
        area_sky = 4*np.pi*(180/np.pi)**2   # In sq. degrees
        f_area = survey.beam_size * self.source_rate.tot
        inside = self.source_rate.det + self.source_rate.late
        inside += self.source_rate.faint
        if inside > 0:
            f_area /= (inside*area_sky)
        else:
            f_area = 1

        # Saving scaling factors
        self.source_rate.days = self.n_days
        self.source_rate.name = self.name
        self.source_rate.vol = self.source_rate.tot
        self.source_rate.vol /= self.vol_co_max * (365.25/self.n_days)

        if self.repeaters:
            self.burst_rate.days = self.n_days
            self.burst_rate.name = self.name
            self.burst_rate.vol = self.burst_rate.tot
            self.burst_rate.vol /= self.vol_co_max * (365.25/self.n_days)

        # If oneoffs, you'll want to scale by the area
        if not self.repeaters:
            self.source_rate.f_area = f_area
            self.source_rate.scale_by_area()


def fast_where(a, min_v, max_v):
    """Faster implementation of np.where(((a >= min_v) & (a <= max_v)))."""
    left = np.apply_along_axis(np.searchsorted, 1, a, min_v)
    right = np.apply_along_axis(np.searchsorted, 1, a, max_v)
    unique_rows = np.where(left <= right)[0]
    bursts_per_row = right[unique_rows] - left[unique_rows]
    rows = np.repeat(unique_rows, bursts_per_row)
    cols = np.zeros(np.sum(bursts_per_row), dtype=int)
    cum_bursts = np.cumsum(bursts_per_row)
    cum_bursts = np.insert(cum_bursts, 0, 0)
    for i, e in enumerate(cum_bursts[1:]):
        cols[cum_bursts[i]:e] = np.arange(left[unique_rows[i]],
                                          right[unique_rows[i]])
    return rows, cols
