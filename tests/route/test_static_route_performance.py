import logging
import re
import time
import pytest
from ipaddress import ip_address, ip_network
from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.topology("t0")]
TEST_ROUTE_NETWORK = ip_network("198.18.0.0/15")
ROUTE_COUNT = 100
ROUTE_TIMEOUT = 60
POLL_INTERVAL = 1
ROUTE_PREFIX_PATTERN = re.compile(
    r"^\s*[A-Z][A-Z0-9 >*]*\s+(\d+(?:\.\d+){3}/\d+)\b",
    re.MULTILINE,
)


def extract_prefixes(output):
    """Extract IPv4 prefixes from route-table output."""
    return set(ROUTE_PREFIX_PATTERN.findall(output))


def get_route_prefixes(duthost, route_type=None):
    """Return prefixes from the complete or filtered IPv4 route table."""
    command = "show ip route"
    if route_type:
        command += " {}".format(route_type)

    result = duthost.shell(command, module_ignore_errors=True)
    return extract_prefixes(result["stdout"])


def _are_routes_in_expected_state(duthost, expected_prefixes, should_be_present):
    """Check whether all expected static routes have the requested state."""
    expected_prefixes = set(expected_prefixes)
    current_prefixes = get_route_prefixes(duthost, route_type="static")
    matching_prefixes = expected_prefixes & current_prefixes

    if should_be_present:
        matching_count = len(matching_prefixes)
        state = "present"
    else:
        matching_count = len(expected_prefixes) - len(matching_prefixes)
        state = "absent"

    logger.info(
        "%d of %d expected routes are %s",
        matching_count,
        len(expected_prefixes),
        state,
    )
    return matching_count == len(expected_prefixes)


def are_routes_present(duthost, expected_prefixes):
    """Return True when every expected static route is present."""
    return _are_routes_in_expected_state(duthost, expected_prefixes, should_be_present=True)


def are_routes_absent(duthost, expected_prefixes):
    """Return True when every expected static route is absent."""
    return _are_routes_in_expected_state(duthost, expected_prefixes, should_be_present=False)


def get_ipv4_nexthop(duthost, tbinfo):
    """Find one IPv4 BGP neighbor address from minigraph"""
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)
    for neighbor in mg_facts.get("minigraph_bgp", []):
        address = neighbor.get("addr")

        if not address:
            continue

        try:
            if ip_address(address).version == 4:
                return address
        except ValueError:
            continue

    pytest.skip("No reachable IPv4 nexthop found")


def add_static_routes(duthost, prefixes, nexthop):
    """Add all test routes through the SONiC configuration CLI."""
    commands = ["config route add prefix {} nexthop {}".format(prefix, nexthop) for prefix in sorted(prefixes)]
    duthost.shell_cmds(cmds=commands, continue_on_fail=True, timeout=120)
    logger.info("Added %d static routes through next hop %s", len(prefixes), nexthop)


def remove_static_routes(duthost, prefixes, nexthop):
    """Remove all test routes through the SONiC configuration CLI."""
    commands = ["config route del prefix {} nexthop {}".format(prefix, nexthop) for prefix in sorted(prefixes)]
    duthost.shell_cmds(cmds=commands, continue_on_fail=True, timeout=120)
    logger.info("Removed %d static routes through next hop %s", len(prefixes), nexthop)


def generate_test_prefixes(duthost):
    """Select unused test prefixes from the benchmarking address range."""
    existing_prefixes = get_route_prefixes(duthost)
    selected_prefixes = set()

    for address in TEST_ROUTE_NETWORK.hosts():
        prefix = "{}/32".format(address)

        # Collision check
        if prefix not in existing_prefixes:
            selected_prefixes.add(prefix)

        if len(selected_prefixes) == ROUTE_COUNT:
            break

    if len(selected_prefixes) < ROUTE_COUNT:
        pytest.skip("Unable to find {} unused test prefixes in {}".format(ROUTE_COUNT, TEST_ROUTE_NETWORK))

    return selected_prefixes


def log_cpu_and_memory(duthost):
    """Log the DUT's current CPU and memory information."""
    top_output = duthost.shell("top -b -n 1")["stdout"]
    top_summary = "\n".join(top_output.splitlines()[:15])
    logger.info("CPU/Memory from top:\n%s", top_summary)

    memory_output = duthost.shell("show system-memory")["stdout"]
    logger.info("Memory status:\n%s", memory_output)

    cpu_output = duthost.shell("show processes cpu")["stdout"]
    cpu_summary = "\n".join(cpu_output.splitlines()[:15])
    logger.info("CPU processes:\n%s", cpu_summary)


def test_static_route_performance(duthosts, enum_rand_one_per_hwsku_frontend_hostname, tbinfo):
    """Measure how long 100 static routes take to appear and disappear."""
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]
    prefixes = generate_test_prefixes(duthost)
    nexthop = get_ipv4_nexthop(duthost, tbinfo)

    try:
        start_time = time.monotonic()
        add_static_routes(duthost, prefixes, nexthop)
        routes_appeared = wait_until(ROUTE_TIMEOUT, POLL_INTERVAL, 0, are_routes_present, duthost, prefixes)
        elapsed_time = time.monotonic() - start_time

        pytest_assert(routes_appeared, "Routes did not appear within {} seconds".format(ROUTE_TIMEOUT))
        logger.info("All %d routes appeared in %.2f seconds", ROUTE_COUNT, elapsed_time)

        log_cpu_and_memory(duthost)
    finally:
        start_time = time.monotonic()
        remove_static_routes(duthost, prefixes, nexthop)
        routes_disappeared = wait_until(ROUTE_TIMEOUT, POLL_INTERVAL, 0, are_routes_absent, duthost, prefixes)
        elapsed_time = time.monotonic() - start_time

        pytest_assert(routes_disappeared, "Routes did not disappear within {} seconds".format(ROUTE_TIMEOUT))
        logger.info("All %d routes disappeared in %.2f seconds", ROUTE_COUNT, elapsed_time)
