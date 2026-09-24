"""Task progress reports that also allow direct task-function tests."""

from contextlib import contextmanager

from pulpcore.plugin.models import ProgressReport, Task


class _NoTaskProgress:
    done = 0

    def increment(self):
        pass

    def save(self):
        pass

    def iter(self, values):
        yield from values


@contextmanager
def task_progress(message, code, total=None):
    """Use Pulp progress when running in a task; direct callers need no task row."""
    if Task.current() is None:
        yield _NoTaskProgress()
    else:
        with ProgressReport(message=message, code=code, total=total) as report:
            yield report
