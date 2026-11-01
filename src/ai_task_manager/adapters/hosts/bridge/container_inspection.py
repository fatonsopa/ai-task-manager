"""Read actual daemon facts and remove only an exactly observed owned container."""

import json
import subprocess


def docker(plan, *arguments):
    value = subprocess.run(
        [plan["docker"], *arguments],
        env=plan["environment"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=4,
    )
    return {
        "returncode": value.returncode,
        "stdout": value.stdout.decode(errors="replace")[:1048576],
        "stderr": value.stderr.decode(errors="replace")[:16384],
    }


def owned(plan):
    observed = docker(plan, "inspect", plan["name"])
    if observed["returncode"] != 0:
        return None, observed
    rows = json.loads(observed["stdout"])
    if len(rows) != 1:
        return None, observed
    container = rows[0]
    if (
        container.get("Name") != "/" + plan["name"]
        or not container.get("Id")
        or container.get("Config", {}).get("Labels", {}).get("aitm.owner") != plan["owner"]
    ):
        return None, observed
    return container, observed


def restrictions(plan):
    container, observed = owned(plan)
    if container is None:
        return False, observed
    expected = plan["security"]
    host = container.get("HostConfig", {})
    config = container.get("Config", {})
    mounts = container.get("Mounts")
    safe = (
        container.get("Image") == expected["image_id"]
        and host.get("ReadonlyRootfs") is True
        and host.get("Privileged") is False
        and host.get("NetworkMode") == "none"
        and host.get("PidMode") == ""
        and host.get("Memory") == expected["memory"]
        and host.get("MemorySwap") == expected["memory"]
        and host.get("NanoCpus") == expected["nano_cpus"]
        and host.get("PidsLimit") == expected["pids"]
        and host.get("CapDrop") == ["ALL"]
        and host.get("SecurityOpt") == ["no-new-privileges"]
        and host.get("Tmpfs") == expected["tmpfs"]
        and config.get("User") == expected["user"]
        and isinstance(mounts, list)
        and len(mounts) == 2
    )
    if safe:
        actual = {
            m.get("Destination"): (m.get("Source"), m.get("Type"), m.get("RW")) for m in mounts
        }
        safe = actual == {
            target: (source, "bind", False) for target, source in expected["mounts"].items()
        }
    return safe, observed


def cleanup(plan):
    container, observed = owned(plan)
    result = {"inspect": observed, "confirmed_stop": False, "removed": False}
    if container is None:
        return result
    identity = container["Id"]
    result["container_id"] = identity
    result["configuration"] = {
        "host": container.get("HostConfig"),
        "mounts": container.get("Mounts"),
        "image": container.get("Image"),
    }
    removed = docker(plan, "rm", "--force", "--volumes", identity)
    result["remove"] = removed
    if removed["returncode"] or removed["stdout"].strip() != identity:
        return result
    absent = docker(plan, "inspect", identity)
    result["after"] = absent
    known = (
        absent["returncode"] == 1
        and identity in absent["stderr"]
        and any(
            marker in absent["stderr"].lower()
            for marker in ["no such object:", "no such container:"]
        )
    )
    result["confirmed_stop"] = result["removed"] = known
    return result
