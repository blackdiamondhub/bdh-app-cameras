from bdh_sdk import OWNER, Status

from . import policy
from .protect_api import get_protect_cameras


def status(mode=None):
    # Match the cameras page: guests only count the cameras they see.
    sites = get_protect_cameras()
    if mode != OWNER:
        sites = policy.guest_sites(sites)
    count = sum(len(site["cameras"]) for site in sites)
    if count == 0:
        return None
    return Status(f"{count} camera{'s' if count != 1 else ''}")
