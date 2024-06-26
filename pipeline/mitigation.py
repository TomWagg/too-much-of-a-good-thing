import argparse
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm
from matplotlib.collections import PatchCollection
import time

from rubin_sim.utils import LsstCameraFootprint
import os.path

from multiprocessing import Pool
from functools import partial

from collections import defaultdict

import thor
from thor.backend import PYOORB
backend = PYOORB()

import sys
sys.path.append("../src")

from variant_orbits import variant_orbit_ephemerides
from scheduling import get_LSST_schedule
from magnitudes import convert_colour_mags

NIGHT_ZERO = 60796


def filter_tracklets(df, min_obs=2, min_arc=1, max_time=90):
    init = SkyCoord(ra=df["RA_deg"].iloc[0], dec=df["Dec_deg"].iloc[0], unit="deg")
    final = SkyCoord(ra=df["RA_deg"].iloc[-1], dec=df["Dec_deg"].iloc[-1], unit="deg")

    return np.logical_and.reduce((len(df) >= min_obs,
                                  init.separation(final).to(u.arcsecond).value > min_arc,
                                  df["mjd_utc"].diff().min() * 1440 < max_time))


def get_detection_probabilities(night_start, detection_window=15, min_nights=3,
                                schedule_type="predicted", pool_size=48,
                                in_path="/epyc/projects/neocp-predictions/output/synthetic_obs/",
                                out_path="/epyc/projects/neocp-predictions/output/mitigation_results/",
                                fov_map_path="/epyc/ssd/users/tomwagg/rubin_sim_data/maf/fov_map.npz",
                                save_results=True):
    """Get the probability that LSST will detect each object that was observed in a particular night

    Parameters
    ----------
    night_start : `int`
        Night of the initial observations
    detection_window : `int`, optional
        How many days in the detection window, by default 15
    min_nights : `int`, optional
        Minimum number of nights on which observations need to have occurred, by default 3
    pool_size : `int`, optional
        How many workers to put in the multiprocessing pool, by default 48
    save_results : `bool`, optional
        Whether to save the results to a file, by default True

    Returns
    -------
    probs : `list`
        Estimated probability that each object will be detected by LSST alone
    unique_objs : `list`
        List of unique hex ids that have digest2 > 65 that were observed on `night_start`
    """
    lap = time.time()

    if not os.path.exists(os.path.join(in_path, f"filtered_night_{night_start:04d}_with_scores.h5")):
        print(f"Night {night_start} does not exist")
        return None, None

    # create a list of nights in the detection window and get schedule for them
    night_list = list(range(night_start, night_start + detection_window))

    if schedule_type == "predicted":
        full_schedule = get_LSST_schedule(night=night_start, schedule_type=schedule_type,
                                          night_zero=NIGHT_ZERO,
                                          schedule_path=in_path.replace("synthetic_obs", "predicted_schedules"))
    else:
        full_schedule = get_LSST_schedule(night=(night_start, night_start + detection_window - 1),
                                          schedule_type=schedule_type, night_zero=NIGHT_ZERO,
                                          schedule_path=in_path.replace("synthetic_obs", "predicted_schedules"))

    # offset the schedule by one row and re-merge to get the previous night column
    shifted = full_schedule.shift()
    full_schedule["previousNight"] = shifted["night"]

    # calculate the length of each night in days
    night_lengths = np.zeros(detection_window)
    for i, night in enumerate(night_list):
        mask = full_schedule["night"] == night

        # ignore nights that have no observations (bad weather/downtime)
        if not full_schedule[mask].empty:
            night_lengths[i] = full_schedule[mask].iloc[-1]["observationStartMJD"]\
                - full_schedule[mask].iloc[0]["observationStartMJD"]

    # get the first/last visit from each night
    night_transition = full_schedule["night"] != full_schedule["previousNight"]
    first_visit_times = full_schedule[night_transition]["observationStartMJD"].values.astype(float)

    print(f"[{time.time() - lap:1.1f}s] Schedule is loaded in and ready!")
    lap = time.time()

    obs_dfs = [pd.read_hdf(os.path.join(in_path, f"filtered_night_{i:04d}_with_scores.h5")).sort_values("FieldMJD_TAI")
                for i in range(max(night_start - detection_window + 1, 0), night_list[-1])
                if os.path.exists(os.path.join(in_path, f"filtered_night_{i:04d}_with_scores.h5"))]
    all_obs = pd.concat(obs_dfs)

    print(f"[{time.time() - lap:1.1f}s] Observation files read in")
    lap = time.time()

    # get the sorted observations for the start night (that have digest2 > 65, >= 3 obs)
    sorted_obs = all_obs[(all_obs["night"] == night_start)
                         & (all_obs["scores"] >= 65)
                         & (all_obs["n_obs"] >= 3)
                         & (all_obs["ang_vel"] < 1.5)].sort_values(["ObjID", "FieldMJD_TAI"])
    unique_objs = sorted_obs['hex_id'].unique()

    # work out which objects would have already been found before tonight and remove them
    # note: the reduced_nights decreases the array size before the `isin` call
    detection_nights = pd.read_hdf(os.path.join(in_path, f"findable_obs_year_1.h5"))
    reduced_nights = detection_nights.loc[list(set(detection_nights.index).intersection(set(unique_objs)))]
    already_found_ids = reduced_nights[reduced_nights < night_start].index
    sorted_obs = sorted_obs[~np.isin(sorted_obs["hex_id"], already_found_ids)]
    unique_objs = sorted_obs["hex_id"].unique()

    # get the prior observations that occurred in the past detection window that could possibly contribute
    all_obs = all_obs[(all_obs["night"] > night_start - detection_window) & (all_obs["night"] < night_start)]
    prior_obs = all_obs[all_obs.index.isin(unique_objs)]

    print(f"[{time.time() - lap:1.1f}s] Masks applied to observation files")
    lap = time.time()

    # create a (default)dict of the nights on which observations occurred
    if prior_obs.empty:
        prior_obs_nights = defaultdict(list)
    else:
        dd = defaultdict(list)
        s = prior_obs.groupby("hex_id").apply(lambda x: list(x["night"].unique()))
        prior_obs_nights = s.to_dict(into=dd)

    print(f"[{time.time() - lap:1.1f}s] Everything is prepped and ready for probability calculations")
    lap = time.time()

    print(f"Starting pool for {len(unique_objs)} objects with {pool_size} workers...")
    sorted_obs.set_index("hex_id", inplace=True)

    # calculate detection probabilities
    with Pool(pool_size) as pool:
        probs = pool.map(partial(probability_from_id, sorted_obs=sorted_obs,
                                 distances=np.logspace(-1, 1, 51) * u.AU,
                                 radial_velocities=np.linspace(-50, 10, 21) * u.km / u.s,
                                 prior_obs_nights=prior_obs_nights,
                                 first_visit_times=first_visit_times, full_schedule=full_schedule,
                                 night_lengths=night_lengths, night_list=night_list,
                                 detection_window=detection_window, min_nights=min_nights,
                                 fov_map_path=fov_map_path), unique_objs)

    print(f"Finished with the pool! [{time.time() - lap:1.1f}s]")

    if save_results:
        np.save(os.path.join(out_path, f"night{night_start}_probs.npy"), (probs, unique_objs))

    return probs, unique_objs


def probability_from_id(hex_id, sorted_obs, distances, radial_velocities, prior_obs_nights, first_visit_times,
                        full_schedule, night_lengths, night_list, detection_window=15, min_nights=3,
                        ret_joined_table=False, verbose=False,
                        fov_map_path="/epyc/ssd/users/tomwagg/rubin_sim_data/maf/fov_map.npz"):
    """Get the probability of an object with a particular ID of being detected by LSST alone given
    observations on a single night.

    Parameters
    ----------
    hex_id : `str`
        ID of the object (in hex format)
    sorted_obs : `pandas DataFrame`
        DataFrame of the sorted observations from the initial night
    distances : `list`
        List of distances to consider
    radial_velocities : `list`
        List of radial velocities to consider
    prior_obs_nights : `defaultdict`
        Dict of lists of nights prior to the observation window in which at least 2 observations occurred
    first_visit_times : `list`
        Times at which each night has its first visit
    full_schedule : `pandas DataFrame`
        Full schedule of visits for the entire detection window
    night_lengths : `list`
        Length of each night of observations in days
    night_list : `list`
        List of the nights in the detection window
    detection_window : `int`, optional
        Length of the detection window in days, by default 15
    min_nights : `int`, optional
        Minimum number of nights required for a detection, by default 3

    Returns
    -------
    probs : `list`
        Estimated probability that the object will be detected by LSST alone
    """
    # get the matching rows and ephemerides for start of each night
    rows = sorted_obs.loc[hex_id]
    reachable_schedule = get_reachable_schedule(rows, first_visit_times, night_list,
                                                night_lengths, full_schedule)
    
    # if nothing is reachable then instantly return 0
    if len(reachable_schedule) == 0:
        return 0.0

    v_mags = [convert_colour_mags(r["observedTrailedSourceMag"],
                                  in_colour=r["optFilter"], out_colour="V") for _, r in rows.iterrows()]
    apparent_mag = np.mean(v_mags)

    # get the orbits for the entire reachable schedule with the grid of distances and RVs
    ephemerides = variant_orbit_ephemerides(ra=rows.iloc[0]["AstRA(deg)"] * u.deg,
                                            dec=rows.iloc[0]["AstDec(deg)"] * u.deg,
                                            ra_end=rows.iloc[-1]["AstRA(deg)"] * u.deg,
                                            dec_end=rows.iloc[-1]["AstDec(deg)"] * u.deg,
                                            delta_t=(rows.iloc[-1]["FieldMJD_TAI"] - rows.iloc[0]["FieldMJD_TAI"]) * u.day,
                                            obstime=Time(rows.iloc[0]["FieldMJD_TAI"], format="mjd"),
                                            distances=distances,
                                            radial_velocities=radial_velocities,
                                            apparent_mag=apparent_mag,
                                            eph_times=Time(reachable_schedule["observationStartMJD"].values.astype(float),
                                                            format="mjd"),
                                            only_neos=True,
                                            num_jobs=1)
    ephemerides["orbit_id"] = ephemerides["orbit_id"].astype(int)
    orbit_ids = ephemerides["orbit_id"].unique()

    # merge the orbits with the schedule
    joined_table = pd.merge(ephemerides, reachable_schedule,
                            left_on="mjd_utc", right_on="observationStartMJD")
    
    # compute filter magnitudes
    mag_in_filter = np.ones(len(joined_table)) * np.inf
    for filter_letter in "ugrizy":
        filter_mask = joined_table["filter"] == filter_letter
        if filter_mask.any():
            mag_in_filter[filter_mask] = convert_colour_mags(joined_table[filter_mask]["VMag"],
                                                             out_colour=filter_letter,
                                                             in_colour="V", convention="LSST",
                                                             asteroid_type="C")
    joined_table["mag_in_filter"] = mag_in_filter

    # work out which are bright enough to be detected
    bright_enough = joined_table["mag_in_filter"] < joined_table["fiveSigmaDepth"]

    # next we want only objects that are in the camera footprint
    camera = LsstCameraFootprint(footprint_file=fov_map_path)
    in_footprint = np.repeat(False, len(joined_table))

    # loop over each of the unique field times
    unique_field_times = joined_table["mjd_utc"].unique()
    for field_time in unique_field_times:
        # get just the table for this time
        time_mask = joined_table["mjd_utc"] == field_time
        field_table = joined_table[time_mask]
        
        # camera returns indices so we can convert that to a mask like so
        all_inds = np.arange(len(field_table))
        observed_inds = camera(field_table["RA_deg"].values, field_table["Dec_deg"].values,
                               field_table["fieldRA"].iloc[0], field_table["fieldDec"].iloc[0],
                               field_table["rotSkyPos"].iloc[0])
        observed_mask = np.isin(all_inds, observed_inds)
        
        # add that mask to the overall one
        in_footprint[time_mask] = observed_mask

    # combine the masks into a single observed boolean
    joined_table["observed"] = np.logical_and(in_footprint, bright_enough)

    # return if nothing got observed
    if not joined_table["observed"].any():
        if ret_joined_table:
            return 0.0, joined_table
        else:
            return 0.0

    # remove any nights that don't match requirements (min_obs, min_arc, max_time)
    df = joined_table[joined_table["observed"]]
    mask = df.groupby(["orbit_id", "night"]).apply(filter_tracklets)
    df_multiindex = df.set_index(["orbit_id", "night"]).sort_index()
    filtered_obs = df_multiindex.loc[mask[mask].index].reset_index()

    # decide whether each orbit is findable
    N_ORB = len(orbit_ids)
    findable = np.repeat(False, N_ORB)
    for i, orbit_id in enumerate(orbit_ids):
        this_orbit = filtered_obs[filtered_obs["orbit_id"] == orbit_id]

        # if the orbit actually exists (if it hasn't been filtered out)
        if not this_orbit.empty:
            # combine any prior observations with the predicted ones
            combined_nights = np.concatenate((prior_obs_nights[hex_id], this_orbit["night"]))
            unique_nights = np.sort(np.unique(combined_nights))

            # check how many nights it is observed on and require the min nights
            if len(unique_nights) >= min_nights:
                # find every window size composed of `min_nights` contiguous nights
                diff_nights = np.diff(unique_nights)
                window_sizes = np.array([sum(diff_nights[i:i + min_nights - 1])
                                        for i in range(len(diff_nights) - min_nights + 2)])

                # record whether any are short enough
                findable[i] = any(window_sizes <= detection_window)

    # return the fraction of orbits that are findable
    prob = findable.astype(int).sum() / N_ORB
    if verbose:
        print(hex_id, prob)

    if ret_joined_table:
        return prob, joined_table
    else:
        return prob


def get_reachable_schedule(rows, first_visit_times, night_list, night_lengths, full_schedule):
    start_orbits = variant_orbit_ephemerides(ra=rows.iloc[0]["AstRA(deg)"] * u.deg,
                                             dec=rows.iloc[0]["AstDec(deg)"] * u.deg,
                                             ra_end=rows.iloc[-1]["AstRA(deg)"] * u.deg,
                                             dec_end=rows.iloc[-1]["AstDec(deg)"] * u.deg,
                                             delta_t=(rows.iloc[-1]["FieldMJD_TAI"]
                                                      - rows.iloc[0]["FieldMJD_TAI"]) * u.day,
                                             obstime=Time(rows.iloc[0]["FieldMJD_TAI"], format="mjd"),
                                             distances=[1] * u.AU,
                                             radial_velocities=[2] * u.km / u.s,
                                             eph_times=Time(first_visit_times, format="mjd"),
                                             num_jobs=1)

    # create some nominal field size
    FIELD_SIZE = 2.1 * 5

    # mask the schedule to things that can be reached on each night
    masked_schedules = [pd.DataFrame() for i in range(len(night_list))]
    for j in range(len(start_orbits)):
        delta_ra = start_orbits.loc[j]["vRAcosDec"] / np.cos(start_orbits.loc[j]["Dec_deg"] * u.deg)\
            * night_lengths[j]
        delta_dec = start_orbits.loc[j]["vDec"] * night_lengths[j]

        ra_lims = sorted([start_orbits.loc[j]["RA_deg"], start_orbits.loc[j]["RA_deg"] + delta_ra.value])
        ra_lims = [ra_lims[0] - FIELD_SIZE, ra_lims[-1] + FIELD_SIZE]
        dec_lims = sorted([start_orbits.loc[j]["Dec_deg"], start_orbits.loc[j]["Dec_deg"] + delta_dec])
        dec_lims = [dec_lims[0] - FIELD_SIZE, dec_lims[-1] + FIELD_SIZE]

        night = (start_orbits.loc[j]["mjd_utc"] - 0.5).astype(int) - NIGHT_ZERO

        mask = full_schedule["night"] == night
        within_lims = ((full_schedule[mask]["fieldRA"] > ra_lims[0])
                       & (full_schedule[mask]["fieldRA"] < ra_lims[1])
                       & (full_schedule[mask]["fieldDec"] > dec_lims[0])
                       & (full_schedule[mask]["fieldDec"] < dec_lims[1]))
        masked_schedules[night_list.index(night)] = full_schedule[mask][within_lims]
    # combine into a single reachable schedule
    return pd.concat(masked_schedules)


def first_last_pos_from_id(hex_id, sorted_obs, s3m_cart, distances, radial_velocities,
                           first_visit_times, last_visit_times):
    rows = sorted_obs.loc[hex_id]

    eph_times = Time(np.sort(np.concatenate([first_visit_times, last_visit_times])), format="mjd")

    ephemerides = variant_orbit_ephemerides(ra=rows.iloc[0]["AstRA(deg)"] * u.deg,
                                       dec=rows.iloc[0]["AstDec(deg)"] * u.deg,
                                       ra_end=rows.iloc[-1]["AstRA(deg)"] * u.deg,
                                       dec_end=rows.iloc[-1]["AstDec(deg)"] * u.deg,
                                       delta_t=(rows.iloc[-1]["FieldMJD_TAI"] - rows.iloc[0]["FieldMJD_TAI"]) * u.day,
                                       obstime=Time(rows.iloc[0]["FieldMJD_TAI"], format="mjd"),
                                       distances=distances,
                                       radial_velocities=radial_velocities,
                                       eph_times=eph_times,
                                       only_neos=True)
    ephemerides["orbit_id"] = ephemerides["orbit_id"].astype(int)

    item = s3m_cart[s3m_cart["hex_id"] == hex_id]
    orb_class = thor.Orbits(orbits=np.atleast_2d(np.concatenate(([item["x"], item["y"], item["z"]],
                                                                 [item["vx"], item["vy"], item["vz"]]))).T,
                            epochs=Time(item["t_0"], format="mjd"))
    truth = backend.generateEphemeris(orbits=orb_class, observers={"I11": Time(eph_times, format="mjd")})

    return ephemerides, truth


def plot_LSST_schedule_with_orbits(schedule, reachable_schedule, ephemerides, joined_table,
                                   truth, night, hex_id,
                                   colour_by="distance", lims="full_schedule", field_radius=2.1, s=10,
                                   filter_mask="all", show_mag_labels=False,
                                   fig=None, ax=None, show=True, ax_labels=True, cbar=True):
    # create the figure with equal aspect ratio
    if fig is None or ax is None:
        fig, ax = plt.subplots(figsize=(20, 10))
    ax.set_aspect("equal")

    # check that there were observations in this night
    ephemerides["night"] = (ephemerides["mjd_utc"] - 0.5).astype(int) - NIGHT_ZERO
    mask = ephemerides["night"] == night
    if not np.any(mask):
        if lims == "full_schedule":
            ax.set_xlim(schedule["fieldRA"].min() - 3,
                        schedule["fieldRA"].max() + 3)
            ax.set_ylim(schedule["fieldDec"].min() - 3,
                        schedule["fieldDec"].max() + 3)
        print("Warning: No observations in this night")
        return None, None

    # plot each schedule with difference colours and widths
    for table, colour, lw in zip([schedule, reachable_schedule], ["black", "#000000"], [1, 1]):
        # ensure you only get the current night
        table_mask = table["night"] == night

        # filter table (pun intended)
        if filter_mask != "all":
            table_mask &= table["filter"] == filter_mask

        ra_field = table["fieldRA"][table_mask]
        dec_field = table["fieldDec"][table_mask]
        patches = [plt.Circle(center, field_radius) for center in np.transpose([ra_field, dec_field])]
        coll = PatchCollection(patches, edgecolors=colour, facecolors="none", linewidths=lw)
        ax.add_collection(coll)

        # only do the following for the full schedule
        if colour == "black":
            # annotate the number of fields in this night
            ax.annotate(f"{len(table[table_mask])} fields", xy=(0.98, 0.98), xycoords="axes fraction",
                        ha="right", va="top", fontsize=20)

            # get the true observations for this night
            obs_dfs = [pd.read_hdf(f"../neocp/neo/filtered_visit_scores_{i:03d}.h5").sort_values("FieldMJD_TAI")[["FieldMJD_TAI", "night", "observedTrailedSourceMag", "filter"]]
               for i in [0, 1]]
            all_obs = pd.concat(obs_dfs)
            all_obs.reset_index(inplace=True)
            nightly_obs = all_obs[(all_obs["night"] == night) & (all_obs["hex_id"] == hex_id)]

            # if there were observations on this night
            if not nightly_obs.empty:
                # work out in which fields the detections occurred
                det_times = nightly_obs["FieldMJD_TAI"].values
                field_times = table[table_mask]["observationStartMJD"]
                ids = [(det_time - field_times[field_times <= det_time]).idxmin() for det_time in det_times]
                det_fields = table.loc[ids]

                # add an inner circle marking the detection
                ra_field = det_fields["fieldRA"]
                dec_field = det_fields["fieldDec"]
                patches = [plt.Circle(center, field_radius * 0.8) for center in np.transpose([ra_field, dec_field])]
                coll = PatchCollection(patches, edgecolors="#13f2a8", facecolors="none", linewidths=2)
                ax.add_collection(coll)

                # add an annotation writing the total number of observations
                ax.annotate(f"{len(det_times)} observations", xy=(0.98, 0.93), xycoords="axes fraction",
                            ha="right", va="top", fontsize=20, color="#13f2a8")

            # if we want to see the magnitude labels
            if show_mag_labels:

                print("Previous magnitudes:", night)
                prev_mags = all_obs[np.logical_and(all_obs["hex_id"] == hex_id, all_obs["night"] <= night)].copy()
                prev_v_band = [convert_colour_mags(r["observedTrailedSourceMag"], in_colour=r["filter"], out_colour="V") for i, r in prev_mags.iterrows()]
                prev_mags["VMag"] = prev_v_band
                print(prev_mags)


                # build a dictionary of magnitude labels based on field position
                mag_labels = {}
                for _, visit in table[table_mask].iterrows():
                    # create tuple of position for dict key
                    xy = (visit["fieldRA"], visit["fieldDec"])

                    v_mag = convert_colour_mags(visit["fiveSigmaDepth"], out_colour="V", in_colour=visit["filter"])

                    # append or create each dict item
                    if xy in mag_labels:
                        mag_labels[xy] += f'\n{visit["filter"]}{visit["fiveSigmaDepth"]:.2f},v{v_mag:.2f}'
                    else:
                        mag_labels[xy] = f'{visit["filter"]}{visit["fiveSigmaDepth"]:.2f},v{v_mag:.2f}'

                # go through each unique field position and add an annotation
                for xy, label in mag_labels.items():
                    ax.annotate(label, xy=xy, ha="center", va="center", fontsize=8)
        
        # if we are doing the reachable schedule
        if colour == "tab:green":
            pred_night_detections = joined_table[(joined_table["night"] == night)
                                           & joined_table["observed"]]
            det_times = pred_night_detections["mjd_utc"].unique()
            if len(det_times) > 0:
                field_times = table[table_mask]["observationStartMJD"]
                ids = [(det_time - field_times[field_times <= det_time]).idxmin() for det_time in det_times]
                det_fields = table.loc[ids]

                # add an inner circle marking the detection
                ra_field = det_fields["fieldRA"]
                dec_field = det_fields["fieldDec"]
                patches = [plt.Circle(center, field_radius * 0.6) for center in np.transpose([ra_field, dec_field])]
                coll = PatchCollection(patches, edgecolors="tab:green", facecolors="none", linewidths=2, linestyle="dotted")
                ax.add_collection(coll)

    # if colouring by orbit then just use a plain old colourbar
    if colour_by == "orbit":
        ax.scatter(ephemerides["RA_deg"][mask], ephemerides["Dec_deg"][mask],
                   s=s, alpha=1, c=ephemerides["orbit_id"][mask])
        scatter = ax.scatter(truth["RA_deg"][mask], truth["Dec_deg"][mask], s=s * 10, c="tab:red")
    # if distance then use a log scale for the colourbar
    elif colour_by == "distance":
        log_dist_from_earth = np.log10(ephemerides["delta_au"])

        boundaries = np.arange(-1, 1.1 + 0.2, 0.2)
        norm = BoundaryNorm(boundaries, plt.cm.plasma_r.N, clip=True)

        for orb in ephemerides[mask]["orbit_id"].unique():
            more_mask = ephemerides[mask]["orbit_id"] == orb
            ax.plot(ephemerides["RA_deg"][mask][more_mask], ephemerides["Dec_deg"][mask][more_mask],
                    color=plt.cm.plasma_r(norm(log_dist_from_earth[mask][more_mask].iloc[0])))

        scatter = ax.scatter(ephemerides["RA_deg"][mask], ephemerides["Dec_deg"][mask], s=s,
                             c=log_dist_from_earth[mask], norm=norm, cmap="plasma_r")

        if cbar:
            fig.colorbar(scatter, label="Log Geocentric Distance [AU]")

        scatter = ax.scatter(truth["RA_deg"][mask], truth["Dec_deg"][mask], s=s, c="#13f2a8", marker="x")
        ax.plot(truth["RA_deg"][mask], truth["Dec_deg"][mask], color="#13f2a8")
    else:
        raise ValueError("Invalid value for colour_by")

    # if limited by the schedule then adjust the limits
    if lims in ["schedule", "reachable"]:
        table = schedule if lims == "schedule" else reachable_schedule
        table_mask = table["night"] == night
        if filter_mask != "all":
            table_mask &= table["filter"] == filter_mask

        if not table[table_mask].empty:
            ax.set_xlim(table[table_mask]["fieldRA"].min() - 3,
                        table[table_mask]["fieldRA"].max() + 3)
            ax.set_ylim(table[table_mask]["fieldDec"].min() - 3,
                        table[table_mask]["fieldDec"].max() + 3)
    elif lims == "full_schedule":
        ax.set_xlim(schedule["fieldRA"].min() - 3,
                    schedule["fieldRA"].max() + 3)
        ax.set_ylim(schedule["fieldDec"].min() - 3,
                    schedule["fieldDec"].max() + 3)
    elif lims == "orbits":
        ax.set_xlim(ephemerides["RA_deg"][mask].min() - 3,
                    ephemerides["RA_deg"][mask].max() + 3)
        ax.set_ylim(ephemerides["Dec_deg"][mask].min() - 3,
                    ephemerides["Dec_deg"][mask].max() + 3)
    else:
        raise ValueError("Invalid input for lims")

    # label the axes, add a grid, show the plot
    if ax_labels:
        ax.set_xlabel("Right Ascension [deg]")
        ax.set_ylabel("Declination [deg]")
    ax.grid()

    if show:
        plt.show()

    return fig, ax


def main():

    parser = argparse.ArgumentParser(description='Calculate self-follow-up probability for a night of obs')
    parser.add_argument('-i', '--in-path',
                        default="/epyc/projects/neocp-predictions/current_criteria/",
                        type=str, help='Path to the folder containing folders of filtered visits')
    parser.add_argument('-o', '--out-path', default="/epyc/projects/neocp-predictions/mitigation_algorithm/latest_runs/", type=str,
                        help='Path to folder in which to place output')
    parser.add_argument('-f', '--fov-map-path', default="/epyc/ssd/users/tomwagg/rubin_sim_data/maf/fov_map.npz", type=str,
                        help='Path to fov_map file')
    parser.add_argument('-s', '--start-night', default=0, type=int,
                        help='First night to run')
    parser.add_argument('-mn', '--min-nights', default=3, type=int,
                        help='Minimum number of nights to get detection')
    parser.add_argument('-w', '--detection-window', default=15, type=int,
                        help='Length of detection window in nights')
    parser.add_argument('-p', '--pool-size', default=28, type=int,
                        help='How many CPUs to use')
    parser.add_argument('-S', '--save-results', action="store_true",
                        help="Whether to save results")
    args = parser.parse_args()

    get_detection_probabilities(night_start=args.start_night,
                                detection_window=args.detection_window, min_nights=args.min_nights,
                                schedule_type="predicted", pool_size=args.pool_size, in_path=args.in_path,
                                out_path=args.out_path, fov_map_path=args.fov_map_path,
                                save_results=args.save_results)


if __name__ == "__main__":
    main()
