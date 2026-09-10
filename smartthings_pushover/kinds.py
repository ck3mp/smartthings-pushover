"""Registry of supported appliance kinds.

Everything that differs between a washer and a dryer (and, one day, a
dishwasher) lives in one `KindSpec` entry: which resource carries the
selected course, what the "done" headline says, how phases are worded,
and the defaults for the env-var block. Nothing else in the package
special-cases a kind by name, so adding an appliance is adding a row.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class KindSpec:
    kind: str
    default_name: str
    default_local_port: int
    course_path: tuple[str, ...]  # resource holding the selected course
    course_key: str  # key whose value looks like Table_NN_Course_HH
    finished_headline: str
    phase_verbs: Mapping[str, str]

    @property
    def prefix(self) -> str:
        """Env-var prefix: `WASHER_IP`, `DRYER_COURSE_NAMES`, ..."""
        return self.kind.upper()

    @property
    def course_href(self) -> str:
        return "/" + "/".join(self.course_path)


WASHER = KindSpec(
    kind="washer",
    default_name="Washing machine",
    default_local_port=49700,
    course_path=("st", "washercourse", "vs", "0"),
    course_key="x.com.samsung.da.st.washerMode",
    finished_headline="Laundry is done",
    phase_verbs=MappingProxyType(
        {
            "Weightsensing": "weighing the load",
            "Wash": "washing",
            "Rinse": "rinsing",
            "Spin": "spinning",
        }
    ),
)

DRYER = KindSpec(
    kind="dryer",
    default_name="Tumble dryer",
    default_local_port=49701,
    course_path=("st", "dryercourse", "vs", "0"),
    course_key="x.com.samsung.da.st.dryerMode",
    finished_headline="Laundry is dry",
    phase_verbs=MappingProxyType(
        {
            "Drying": "drying",
            "Cooling": "cooling down",
        }
    ),
)

KIND_SPECS: Mapping[str, KindSpec] = MappingProxyType({s.kind: s for s in (WASHER, DRYER)})
KINDS: tuple[str, ...] = tuple(KIND_SPECS)


def spec(kind: str) -> KindSpec:
    try:
        return KIND_SPECS[kind]
    except KeyError:
        raise ValueError(f"unknown appliance kind {kind!r}; known: {', '.join(KINDS)}") from None
