"""Version shared by the BMS header and release process."""

BMS_VERSION = "0.20.0"


def version_context(_request):
    return {"bms_version": BMS_VERSION}
