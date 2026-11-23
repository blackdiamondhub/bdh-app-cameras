from bdh_sdk import PluginSpec, SettingsPage, Tile
from bdh_sdk.plugins import LIFECYCLE_ALWAYS, NETWORK_LAN

spec = PluginSpec(
    id="cameras",
    app="cameras",
    urls="cameras.urls",
    # Live video and talk, relayed to go2rtc/aec (which only listen on loopback).
    websocket_routes="cameras.routing.websocket_urlpatterns",
    status="cameras.homepage.status",
    tiles=(
        Tile(
            title="Cameras",
            url_name="cameras",
            icon="cameras/hub/icon.svg",
            section="more",
            subtitle="Outdoor cameras",
            order=20,
        ),
    ),
    # One page (NVRs, events, pop-ups, cameras page, advanced); it links to the
    # NVRs and Pop-ups pages. The old flat settings (show_<kind>, keep_open,
    # popup_seconds, video_transport) are still read until it's saved.
    settings_pages=(
        SettingsPage("Cameras", "cameras_settings",
                     subtitle="NVRs, events, pop-ups and video",
                     icon="fas fa-video"),
    ),
    api_version=2,
    # Pop-ups: the trigger picker lists HA entities, any of them can trigger
    # a camera, and a detection or trigger pushes a hub alert.
    permissions=("alerts", "ha.read:*", "events.subscribe:+/state/#"),
    # The UniFi Protect consoles and the go2rtc and aec companions are on the LAN.
    network=NETWORK_LAN,
    # The Protect event workers keep a websocket per site open.
    lifecycle=LIFECYCLE_ALWAYS,
    # Scrubbing decodes Protect snapshots in bursts on top of a ~70-100 MiB
    # baseline; 128 left no headroom and the OOM killer took the app mid-scrub.
    memory_mb=192,
    # aec: echo cancellation, so talking to a doorbell isn't heard back.
    companions=("go2rtc", "aec"),
)