"""Build the existing confined runner from trusted exact local capabilities."""

from ai_task_manager.adapters.hosts.bridge.containers import DockerCommands

from .registry import CommandDefinition, InstalledCommands

__all__ = ["CommandDefinition", "InstalledCommands", "build"]


def build(
    context,
    *,
    docker_executable,
    docker_endpoint,
    image_id,
    definitions,
    check_sources,
    maximum_jobs=4,
):
    plans = {definition.reference: definition.plan(image_id) for definition in definitions}
    runner = DockerCommands(
        context.host_journal,
        context.host_journal.path.parent / "commands",
        docker_executable,
        docker_endpoint,
        plans,
        deadline=context.deadline,
        maximum_jobs=maximum_jobs,
    )
    return InstalledCommands(context, runner, definitions, check_sources=check_sources)
