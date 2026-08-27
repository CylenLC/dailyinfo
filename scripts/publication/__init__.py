"""Canonical Publication v1 layer for DailyInfo.

The package deliberately has no delivery-sink dependencies.  It turns a
structured pipeline result into validated, serializable publication objects
and persists them through :class:`PublicationStore`.
"""

from .adapters import (
    PublicationBriefingInput,
    PublicationItemInput,
    StructuredPublicationAdapter,
)
from .finalizer import PublicationFinalizer
from .identity import source_namespace
from .models import (
    CANONICAL_CATEGORIES,
    SCHEMA_VERSION,
    Briefing,
    IdentityConflictError,
    Item,
    PublicationBundle,
    PublicationValidationError,
    SourceMetadata,
)
from .serialization import (
    briefing_content_hash,
    bundle_content_hash,
    deserialize_bundle,
    item_content_hash,
    serialize_bundle,
)
from .store import (
    CorruptPublicationError,
    PublicationStore,
    PublicationStoreError,
    StoreResult,
)
from .validation import (
    validate_briefing,
    validate_bundle,
    validate_category,
    validate_item,
    validate_public_source_url,
)

__all__ = [
    "Briefing",
    "briefing_content_hash",
    "bundle_content_hash",
    "CANONICAL_CATEGORIES",
    "IdentityConflictError",
    "Item",
    "item_content_hash",
    "PublicationBriefingInput",
    "PublicationBundle",
    "PublicationFinalizer",
    "PublicationItemInput",
    "PublicationStore",
    "PublicationStoreError",
    "PublicationValidationError",
    "SCHEMA_VERSION",
    "SourceMetadata",
    "StoreResult",
    "StructuredPublicationAdapter",
    "CorruptPublicationError",
    "deserialize_bundle",
    "serialize_bundle",
    "source_namespace",
    "validate_briefing",
    "validate_bundle",
    "validate_category",
    "validate_item",
    "validate_public_source_url",
]
