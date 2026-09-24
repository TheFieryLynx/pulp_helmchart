"""Keep setuptools' build staging area free of stale files from prior builds."""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class CleanBuildPy(build_py):
    def run(self):
        staging = Path(self.build_lib).absolute()
        default_staging = (Path.cwd() / "build" / "lib").absolute()
        if staging == default_staging and staging.exists():
            shutil.rmtree(staging)
        super().run()


setup(cmdclass={"build_py": CleanBuildPy})
