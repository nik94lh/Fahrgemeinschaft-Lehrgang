"""Version shared by the BMS header and release process."""

BMS_VERSION = "0.9.2"


def version_context(_request):
    return {"bms_version": BMS_VERSION}
