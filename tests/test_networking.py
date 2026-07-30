from worldgs_receiver.networking import local_lan_addresses


def test_local_lan_addresses_prefers_default_route_interface_over_proxy_hostname() -> None:
    def run(command: list[str]) -> str:
        if command == ["route", "-n", "get", "default"]:
            return "route to: default\ninterface: en0\n"
        if command == ["ipconfig", "getifaddr", "en0"]:
            return "192.168.1.8\n"
        return ""

    addresses = local_lan_addresses(run_command=run, hostname="unused-hostname")

    assert addresses[0] == "192.168.1.8"


def test_local_lan_addresses_filters_10_network_proxy_addresses() -> None:
    def run(command: list[str]) -> str:
        if command == ["route", "-n", "get", "default"]:
            return "interface: utun4\n"
        if command == ["ipconfig", "getifaddr", "utun4"]:
            return "10.255.72.52\n"
        return ""

    addresses = local_lan_addresses(run_command=run, hostname="unused-hostname")

    assert "10.255.72.52" not in addresses


def test_local_lan_addresses_supports_linux_default_route() -> None:
    def run(command: list[str]) -> str:
        if command == ["route", "-n", "get", "default"]:
            return ""
        if command == ["ip", "route", "show", "default"]:
            return "default via 192.168.1.1 dev wlan0 proto dhcp metric 600\n"
        if command == ["ipconfig", "getifaddr", "wlan0"]:
            return ""
        if command == ["ip", "-o", "-4", "addr", "show", "dev", "wlan0"]:
            return "2: wlan0    inet 192.168.1.12/24 brd 192.168.1.255 scope global wlan0\n"
        return ""

    addresses = local_lan_addresses(run_command=run, hostname="unused-hostname")

    assert addresses[0] == "192.168.1.12"
