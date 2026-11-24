"""The one place the application's name and version live.

The page footer, /api/health, the service log and `serve.py --version` all read it from here. Change it in this file
only, and describe the new version in CHANGELOG.md. Keep this file at the root for released updaters and installers.
"""

APP_NAME = "Fel's Voice Lab"
VERSION = "0.4.0"
# Where its releases are published: Check for updates and update.bat ask GitHub for the newest release here, tagged v<version>.
REPOSITORY = "Fel101/fels-voice-lab"
# The Lab's suite API: the number other apps of Fel's Studio read in /api/health's `fels` block (fels_suite.py, contract 1).
# It goes up only with a CHANGELOG line.
SUITE_API = 1
