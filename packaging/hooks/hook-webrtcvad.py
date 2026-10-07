# The voice extra installs webrtcvad from the `webrtcvad-wheels` distribution (it has prebuilt wheels
# for Windows); the stock hook copies the metadata of a distribution named `webrtcvad` and stops the
# build when that is missing. webrtcvad reads its version from whichever of the two is installed.
from PyInstaller.utils.hooks import copy_metadata

datas = []
for dist in ("webrtcvad-wheels", "webrtcvad"):
    try:
        datas = copy_metadata(dist)
        break
    except Exception:
        continue
