# Author: Eric Benner <ebenner@vultr.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import json
from functools import lru_cache

from cloudinit import dmi
from cloudinit import log as log
from cloudinit import net, netinfo, subp, url_helper, util
from cloudinit.net.dhcp import EphemeralDHCPv4, NoDHCPLeaseError

# Get LOG
LOG = log.getLogger(__name__)


@lru_cache()
def get_metadata(url, timeout, retries, sec_between, agent):
    # Bring up interface (and try untill one works)
    exception = RuntimeError("Failed to DHCP")

    # Seek iface with DHCP
    for iface in net.get_interfaces():
        # Skip dummy, lo interfaces
        if "dummy" in iface[0]:
            continue
        if "lo" == iface[0]:
            continue
        try:
            with EphemeralDHCPv4(
                iface=iface[0], connectivity_url_data={"url": url}
            ):
                # Set metadata route
                set_route(iface[0])

                # Fetch the metadata
                v1 = read_metadata(url, timeout, retries, sec_between, agent)

                return json.loads(v1)
        except (NoDHCPLeaseError, subp.ProcessExecutionError) as exc:
            LOG.error("DHCP Exception: %s", exc)
            exception = exc
    raise exception


# Set route for metadata
def set_route(iface):
    # Get routes, confirm entry does not exist
    routes = netinfo.route_info()

    # If no tools exist and empty dict is returned
    if "ipv4" not in routes:
        return

    # We only care about IPv4
    routes = routes["ipv4"]

    # Searchable list
    dests = []

    # Parse each route into a more searchable format
    for route in routes:
        dests.append(route["destination"])

    gw_present = "100.64.0.0" in dests or "100.64.0.0/10" in dests
    dest_present = "169.254.169.254" in dests

    # If not IPv6 only (No link local)
    # or the route is already present
    if not gw_present or dest_present:
        return

    # Set metadata route
    if subp.which("ip"):
        subp.subp(
            [
                "ip",
                "route",
                "add",
                "169.254.169.254/32",
                "dev",
                iface,
            ]
        )
    elif subp.which("route"):
        subp.subp(["route", "add", "-net", "169.254.169.254/32", "100.64.0.1"])


# Read the system information from SMBIOS
def get_sysinfo():
    return {
        "manufacturer": dmi.read_dmi_data("system-manufacturer"),
        "subid": dmi.read_dmi_data("system-serial-number"),
    }


# Assumes is Vultr is already checked
def is_baremetal():
    if get_sysinfo()["manufacturer"] != "Vultr":
        return True
    return False


# Confirm is Vultr
def is_vultr():
    # VC2, VDC, and HFC use DMI
    sysinfo = get_sysinfo()

    if sysinfo["manufacturer"] == "Vultr":
        return True

    # Baremetal requires a kernel parameter
    if "vultr" in util.get_cmdline().split():
        return True

    return False


# Read Metadata endpoint
def read_metadata(url, timeout, retries, sec_between, agent):
    url = "%s/v1.json" % url

    # Announce os details so we can handle non Vultr origin
    # images and provide correct vendordata generation.
    headers = {"Metadata-Token": "cloudinit", "User-Agent": agent}

    response = url_helper.readurl(
        url,
        timeout=timeout,
        retries=retries,
        headers=headers,
        sec_between=sec_between,
    )

    if not response.ok():
        raise RuntimeError(
            "Failed to connect to %s: Code: %s" % url, response.code
        )

    return response.contents.decode()


# Wrapped for caching
@lru_cache()
def get_interface_map():
    return net.get_interfaces_by_mac()


# Convert macs to nics
def get_interface_name(mac):
    macs_to_nic = get_interface_map()

    if mac not in macs_to_nic:
        return None

    return macs_to_nic.get(mac)


# Generate network configs
def generate_network_config(interfaces):
    network = {
        "version": 1,
        "config": [{"type": "nameserver", "address": ["108.61.10.10"]}],
    }

    # Prepare interface 0, public
    if len(interfaces) > 0:
        public = generate_public_network_interface(interfaces[0])
        network["config"].append(public)

    # Prepare additional interfaces, private
    for i in range(1, len(interfaces)):
        private = generate_private_network_interface(interfaces[i])
        network["config"].append(private)

    return network


# Input Metadata and generate public network config part
def generate_public_network_interface(interface):
    interface_name = get_interface_name(interface["mac"])
    if not interface_name:
        raise RuntimeError(
            "Interface: %s could not be found on the system" % interface["mac"]
        )

    netcfg = {
        "name": interface_name,
        "type": "physical",
        "mac_address": interface["mac"],
        "accept-ra": 1,
        "subnets": [
            {"type": "dhcp", "control": "auto"},
            {"type": "ipv6_slaac", "control": "auto"},
        ],
    }

    # Options that may or may not be used
    if "mtu" in interface:
        netcfg["mtu"] = interface["mtu"]

    if "accept-ra" in interface:
        netcfg["accept-ra"] = interface["accept-ra"]

    if "routes" in interface:
        netcfg["subnets"][0]["routes"] = interface["routes"]

    # Check for additional IP's
    additional_count = len(interface["ipv4"]["additional"])
    if "ipv4" in interface and additional_count > 0:
        for additional in interface["ipv4"]["additional"]:
            add = {
                "type": "static",
                "control": "auto",
                "address": additional["address"],
                "netmask": additional["netmask"],
            }

            if "routes" in additional:
                add["routes"] = additional["routes"]

            netcfg["subnets"].append(add)

    # Check for additional IPv6's
    additional_count = len(interface["ipv6"]["additional"])
    if "ipv6" in interface and additional_count > 0:
        for additional in interface["ipv6"]["additional"]:
            add = {
                "type": "static6",
                "control": "auto",
                "address": additional["address"],
                "netmask": additional["netmask"],
            }

            if "routes" in additional:
                add["routes"] = additional["routes"]

            netcfg["subnets"].append(add)

    # Add config to template
    return netcfg


# Input Metadata and generate private network config part
def generate_private_network_interface(interface):
    interface_name = get_interface_name(interface["mac"])
    if not interface_name:
        raise RuntimeError(
            "Interface: %s could not be found on the system" % interface["mac"]
        )

    netcfg = {
        "name": interface_name,
        "type": "physical",
        "mac_address": interface["mac"],
        "subnets": [
            {
                "type": "static",
                "control": "auto",
                "address": interface["ipv4"]["address"],
                "netmask": interface["ipv4"]["netmask"],
            }
        ],
    }

    # Options that may or may not be used
    if "mtu" in interface:
        netcfg["mtu"] = interface["mtu"]

    if "accept-ra" in interface:
        netcfg["accept-ra"] = interface["accept-ra"]

    if "routes" in interface:
        netcfg["subnets"][0]["routes"] = interface["routes"]

    return netcfg


# Make required adjustments to the network configs provided
def add_interface_names(interfaces):
    for interface in interfaces:
        interface_name = get_interface_name(interface["mac"])
        if not interface_name:
            raise RuntimeError(
                "Interface: %s could not be found on the system"
                % interface["mac"]
            )
        interface["name"] = interface_name

    return interfaces


# vi: ts=4 expandtab
