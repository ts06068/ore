"""Include an already compiled console when building an ORE distribution.

Editable development installs can precede the frontend build. Release builds
are stricter: scripts/build_release.py requires and verifies the static assets.
"""
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        source = Path(self.root) / 'web' / 'dist'
        if (source / 'index.html').is_file():
            destination = 'ore/static' if self.target_name == 'wheel' else 'web/dist'
            build_data.setdefault('force_include', {})[str(source)] = destination
