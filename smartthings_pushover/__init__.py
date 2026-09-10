"""Forward Samsung washer and dryer state changes to Pushover.

Talks to the appliance directly over CoAP-DTLS on the LAN using the
smartthings-local library; no SmartThings cloud, no MQTT broker.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("smartthings-pushover")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+unknown"
