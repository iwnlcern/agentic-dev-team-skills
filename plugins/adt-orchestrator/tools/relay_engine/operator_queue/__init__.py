"""Machine-wide operator queue library."""

__all__ = ('undo_step', 'UndoQueueResult', 'UndoBlocked', 'BlockingItem')


def __dir__():
    return sorted(set(globals()) | set(__all__))


def __getattr__(name):
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import records

    return {
        'undo_step': records.undo_step,
        'UndoQueueResult': records.UndoQueueResult,
        'UndoBlocked': records.UndoBlocked,
        'BlockingItem': records.BlockingItem,
    }[name]
