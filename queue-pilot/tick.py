"""One read-only queue poll producing local reservations, not launch authority."""

from github_queue import discover, snapshot


def poll_tick(admission, get, *, not_before, clock):
    """Project all discovered runs before changing local reservation state.

    get and clock are supplied by the caller. No authentication, write API,
    start loop, claim, or VM operation is performed here. A later claimant
    still needs a fresh exact run snapshot.
    """
    profile = admission.profile
    run_ids = discover(get, profile=profile, not_before=not_before, clock=clock)
    snapshots = tuple(snapshot(get, run_id, profile=profile, not_before=not_before, clock=clock)
                      for run_id in run_ids)
    return tuple(admission.accept_batch(snapshots, int(clock())))
