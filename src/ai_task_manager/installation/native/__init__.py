"""Cold intent resolution never manufactures genuine startup or admission."""

from .authority import MissingNativeAuthority, NativeAuthoritySource, NativePermission
from .manifest import NativeManifest
from .resolver import resolve

__all__ = [
    "MissingNativeAuthority",
    "NativeAuthoritySource",
    "NativePermission",
    "NativeManifest",
    "resolve",
]
