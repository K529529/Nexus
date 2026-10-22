def group_events(events):
    """Group events by their ``user`` field.

    Returns a dict mapping each ``user`` value to the list of that user's
    events. Every event is copied verbatim, so all original fields are kept.

    Ordering: the previously agreed time-ordering semantics for this project
    are NOT recorded anywhere in this workspace (no spec, tests, or history),
    so none is applied and no assumption is made about which field holds a
    timestamp. Events therefore keep their input order within each group.
    Pass events pre-sorted if a specific time order is required.
    """
    grouped = {}
    for event in events:
        grouped.setdefault(event["user"], []).append(dict(event))
    return grouped
