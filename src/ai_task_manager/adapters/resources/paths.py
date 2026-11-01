"""Confine resource paths before effects and verify physical identity before removal."""

from pathlib import Path

from ai_task_manager.supervisor.models import RuntimeRefused


def confined(root: Path, relative: str) -> Path:
    root = Path(root).absolute()
    part = Path(relative)
    if root.is_symlink() or root.resolve() != root or part.is_absolute() or ".." in part.parts:
        raise RuntimeRefused("Resource paths must stay inside the owned root")
    path = root / part
    if path == root:
        raise RuntimeRefused("The resource root itself cannot be a task resource")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink():
            raise RuntimeRefused("Symlink resource paths are refused")
    if not path.is_relative_to(root):
        raise RuntimeRefused("Resource path escaped its root")
    return path


def identity(path: Path):
    info = path.lstat()
    if path.is_symlink():
        raise RuntimeRefused("Resource was replaced with a symlink")
    return {"device": info.st_dev, "inode": info.st_ino}


def unchanged(path: Path, expected):
    if not path.exists() or identity(path) != expected:
        raise RuntimeRefused("Resource physical identity changed")
