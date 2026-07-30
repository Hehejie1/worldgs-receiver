import ipaddress
import socket
import subprocess
from typing import Callable, Optional


def local_lan_addresses(
    run_command: Optional[Callable[[list[str]], str]] = None,
    hostname: Optional[str] = None,
) -> list[str]:
    run = run_command or _run_command
    candidates: list[str] = []

    default_interface = _default_route_interface(run)
    if default_interface:
        candidates.extend(_interface_ipv4_addresses(default_interface, run))

    candidates.extend(_hostname_ipv4_addresses(hostname or socket.gethostname()))

    unique: list[str] = []
    for address in candidates:
        if address not in unique and _is_usable_lan_address(address):
            unique.append(address)
    return unique


def _default_route_interface(run_command: Callable[[list[str]], str]) -> Optional[str]:
    output = run_command(["route", "-n", "get", "default"])
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("interface:"):
            return line.split(":", 1)[1].strip()
    output = run_command(["ip", "route", "show", "default"])
    for line in output.splitlines():
        parts = line.split()
        if "dev" in parts:
            dev_index = parts.index("dev")
            if dev_index + 1 < len(parts):
                return parts[dev_index + 1]
    return None


def _interface_ipv4_addresses(interface: str, run_command: Callable[[list[str]], str]) -> list[str]:
    output = run_command(["ipconfig", "getifaddr", interface])
    addresses = [line.strip() for line in output.splitlines() if line.strip()]
    if addresses:
        return addresses

    output = run_command(["ip", "-o", "-4", "addr", "show", "dev", interface])
    addresses = []
    for line in output.splitlines():
        parts = line.split()
        if "inet" not in parts:
            continue
        inet_index = parts.index("inet")
        if inet_index + 1 < len(parts):
            addresses.append(parts[inet_index + 1].split("/", 1)[0])
    return addresses


def _hostname_ipv4_addresses(hostname: str) -> list[str]:
    addresses: list[str] = []
    try:
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addresses.append(item[4][0])
    except OSError:
        pass
    return addresses


def _is_usable_lan_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
        return False
    return parsed.version == 4 and parsed.is_private and not str(parsed).startswith("10.")


def _run_command(command: list[str]) -> str:
    try:
        return subprocess.check_output(command, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""
