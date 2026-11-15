from bdh_sdk import PluginSpec, Setting, SettingsPage, Tile
from bdh_sdk.plugins import BOOL, CHOICE, INT, LIFECYCLE_ALWAYS, NETWORK_LAN

_KIND_HELP = "Show these in the Last 24 hours panel and on the timeline (signed-in admins only)."

spec = PluginSpec(
    id="cameras",
    app="cameras",
    urls="cameras.urls",
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
    settings=(
        Setting("show_doorbell", "Doorbell rings", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_person", "People", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_vehicle", "Vehicles", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_package", "Packages", type=BOOL, default=True, help=_KIND_HELP),
        Setting("show_animal", "Animals", type=BOOL, default=False, help=_KIND_HELP),
        Setting("show_motion", "Plain motion", type=BOOL, default=False,
                help="Motion with no person, vehicle, package or animal detected."),
        Setting("keep_open", "Leave cameras open when idle", type=BOOL, default=True,
                help="Keep the cameras page on screen instead of switching to the screen saver."),
        Setting("popup_seconds", "Pop-up duration", type=INT, default=60,
                minimum=10, maximum=600, unit="s"),
        Setting("video_transport", "Advanced: live video transport", type=CHOICE, default="tcp",
                choices=(("tcp", "TCP"), ("udp", "UDP (TCP if UDP fails)")),
                help="TCP recovers lost packets, so video doesn't freeze on a lossy network. "
                     "UDP has lower latency but go2rtc doesn't resend lost packets."),
    ),
    settings_pages=(
        SettingsPage("NVRs", "cameras_nvrs",
                     subtitle="The UniFi Protect consoles the cameras come from",
                     icon="fas fa-server"),
        SettingsPage("Pop-ups", "cameras_popups",
                     subtitle="Which detections and Home Assistant events pop up each camera",
                     icon="fas fa-bell"),
    ),
    api_version=2,
    # Pop-ups: the trigger picker lists HA entities, any of them can trigger
    # a camera, and a detection or trigger pushes a hub alert.
    permissions=("alerts", "ha.read:*", "events.subscribe:ha/state/#"),
    # The UniFi Protect consoles and the go2rtc companion are on the LAN.
    network=NETWORK_LAN,
    # The Protect event workers keep a websocket per site open.
    lifecycle=LIFECYCLE_ALWAYS,
    memory_mb=128,
    companions=("go2rtc",),
)