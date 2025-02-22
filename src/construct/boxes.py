import re
import os
import json
from glob import glob
from loguru import logger
from functools import cache
from dataclasses import dataclass, field

from common import get_mp4ra_boxes

BOXES = {}
EXTENSIONS = {}
TYPE_HIERARCHY = {}


@dataclass
class Box:
    fourcc: str
    spec: str = None
    codec: str = None
    description: str = None
    type: str = None
    registered_type: str = None
    versions: list = None
    flags: list = None
    containers: list = field(default_factory=list)
    syntax: str = None
    incomplete: bool = False

    def __hash__(self):
        return hash(f"{self.fourcc}:{self.type}")

    def serialize(self):
        return {
            "fourcc": self.fourcc,
            "spec": self.spec,
            "codec": self.codec,
            "description": self.description,
            "type": self.type,
            "registered_type": self.registered_type,
            "versions": self.versions,
            "flags": self.flags,
            "containers": self.containers,
            "syntax": self.syntax,
        }


@cache
def get_all_inheritors(cls):
    """
    Get all inheritors of a class. Skip subclasses that are not in TYPE_HIERARCHY.
    If requested class is not in TYPE_HIERARCHY, return a set with only the requested class.
    """
    if cls not in TYPE_HIERARCHY:
        return set([cls])
    subclasses = set(sc for sc in TYPE_HIERARCHY[cls] if sc in TYPE_HIERARCHY)
    return (
        set([cls])
        | subclasses
        | set(s for c in subclasses for s in get_all_inheritors(c))
    )


def get_all_boxes(json_file):
    with open(json_file, "r") as f:
        data = json.load(f)

        entries = data["entries"]
        _boxes = set()
        _spec = os.path.basename(os.path.dirname(json_file))

        for entry in entries:
            _boxes.add(
                Box(
                    fourcc=entry["fourcc"],
                    description=entry["description"],
                    type=entry["type"],
                    registered_type=data["name"],
                    versions=entry["versions"] if "versions" in entry else [],
                    flags=entry["flags"] if "flags" in entry else [],
                    containers=entry["containers"],
                    syntax=entry["syntax"],
                )
            )

        if _spec in BOXES:
            BOXES[_spec] = BOXES[_spec].union(_boxes)
        else:
            BOXES[_spec] = _boxes


def search_box(fourcc, type=None):
    for value in BOXES.values():
        for _box in value:
            if type is not None:
                if _box.fourcc == fourcc and _box.type == type:
                    return _box
            else:
                if _box.fourcc == fourcc:
                    return _box

    return Box(
        fourcc=fourcc,
        incomplete=True,
    )


def update_container(_spec, _box):
    container_boxes = []
    for container in _box.containers:
        if isinstance(container, str):
            if len(container) != 4:
                container_boxes.append({"type": container})
            else:
                container_boxes.append(search_box(container))
        elif isinstance(container, dict):
            for key, value in container.items():
                if not isinstance(value, list):
                    raise Exception("Unknown container type, found no list as value")
                for fourcc in value:
                    container_box = search_box(fourcc, key)
                    if container_box.incomplete:
                        container_box.type = key
                    container_boxes.append(container_box)
        else:
            raise Exception("Unknown container type, did not find str or dict")

    _box.containers = []
    for container_box in container_boxes:
        if isinstance(container_box, Box):
            _box.containers.append(
                {"fourcc": container_box.fourcc, "type": container_box.type}
            )
        else:
            for inheritor in get_all_inheritors(container_box["type"]):
                _box.containers.append({"fourcc": "*", "type": inheritor})

    for container_box in [_box for _box in container_boxes if isinstance(_box, Box)]:
        if container_box.incomplete:
            if _spec not in EXTENSIONS:
                EXTENSIONS[_spec] = set()
            EXTENSIONS[_spec].add(container_box)


def main():
    logger.add("/tmp/construct.log", level="ERROR")
    files = glob("../data/standard_features/**/*.json")

    # Ignore unrelated files
    files = [
        file for file in files if "user_defined" not in file and "spec_info" not in file
    ]

    for file in files:
        get_all_boxes(file)

    # Get all available syntaxes
    extract_syntax = re.compile(
        r"(?:[Cc]lass\s*([a-zA-Z0-9]+).*extends\s*([a-zA-Z0-9]+)).*{",
        flags=re.MULTILINE | re.DOTALL,
    )
    syntaxes = set()
    for value in BOXES.values():
        for _box in value:
            matches = extract_syntax.findall(_box.syntax)
            for match in matches:
                syntaxes.add(match)

    # Extract type hierarchy
    for cls, ext in syntaxes:
        if ext not in TYPE_HIERARCHY:
            TYPE_HIERARCHY[ext] = set()
        TYPE_HIERARCHY[ext].add(cls)

    # Update containers
    for spec, boxes in BOXES.items():
        for _box in boxes:
            update_container(spec, _box)

    # Merge extensions
    for spec, extensions in EXTENSIONS.items():
        if spec not in BOXES:
            BOXES[spec] = set()
        BOXES[spec] = BOXES[spec].union(extensions)

    # List incomplete boxes
    buffer = []
    for boxes in BOXES.values():
        for _box in boxes:
            if _box.incomplete and _box.fourcc != "file":
                buffer.append(f"{_box.fourcc} ({_box.type})")

    if len(buffer) > 0:
        logger.error(f"Incomplete boxes ({len(set(buffer))}): {set(sorted(buffer))}")

    # Add spec to box and merge all
    for spec, boxes in BOXES.items():
        for _box in boxes:
            _box.spec = spec

    all_boxes = set()
    for boxes in BOXES.values():
        all_boxes = all_boxes.union(boxes)

    # List unknown types
    # if a container has {"type": something} that isnt found in any boxes's type then its unknown
    buffer = []
    for _box in all_boxes:
        if _box.incomplete:
            continue
        for container in _box.containers:
            if isinstance(container, dict):
                if "type" in container:
                    if container["type"] not in [_box.type for _box in all_boxes]:
                        # Ignore '*'
                        if container["type"] == "*":
                            continue
                        buffer.append(f"{container['type']}")

    if len(buffer) > 0:
        logger.error(f"Unknown types ({len(set(buffer))}): {set(sorted(buffer))}")

    # List unknown boxes
    buffer = []
    for _box in all_boxes:
        if _box.incomplete:
            continue
        for container in _box.containers:
            if isinstance(container, dict):
                if "type" in container and "fourcc" in container:
                    if container["type"] == "*" or container["fourcc"] == "*":
                        continue
                    found = False
                    for __box in all_boxes:
                        if (
                            __box.type == container["type"]
                            and __box.fourcc == container["fourcc"]
                        ):
                            found = True
                            break

                    if not found:
                        buffer.append(f"{container['fourcc']} ({container['type']})")

                else:
                    logger.warning(
                        "Skipped",
                        container["fourcc"],
                        "from unknown boxes search",
                    )

    if len(buffer) > 0:
        logger.error(f"Unknown boxes ({len(set(buffer))}): {set(sorted(buffer))}")

    # Show boxes with empty type
    buffer = []
    for _box in all_boxes:
        if _box.incomplete:
            continue
        if _box.type == "":
            buffer.append(f"{_box.fourcc} ({_box.type})")

    # FIXME: This should be an error
    if len(buffer) > 0:
        logger.warning(
            f"Boxes with empty type ({len(set(buffer))}): {set(sorted(buffer))}"
        )

    # Show duplicates (same fourcc)
    buffer = []
    for _box in all_boxes:
        if (
            len(
                [
                    box
                    for box in all_boxes
                    if box.fourcc == _box.fourcc and box.type == _box.type
                ]
            )
            > 1
        ):
            buffer.append(f"{_box.fourcc} ({_box.type})")

    if len(buffer) > 0:
        logger.error(f"Duplicate boxes ({len(set(buffer))}): {set(sorted(buffer))}")

    # Check missing boxes in MP4RA
    buffer = []
    for _box in all_boxes:
        if _box.fourcc not in get_mp4ra_boxes():
            buffer.append(_box.fourcc)

    if len(buffer) > 0:
        logger.warning(
            f"Missing boxes in MP4RA ({len(set(buffer))}): {set(sorted(buffer))}"
        )

    # Check missing boxes in standard features
    all_fourccs = set(_box.fourcc for _box in all_boxes)
    buffer = []
    for _box in get_mp4ra_boxes():
        if _box not in all_fourccs:
            buffer.append(_box)

    # FIXME: This should be an error
    if len(buffer) > 0:
        logger.warning(
            f"Missing boxes in standard features ({len(set(buffer))}): {set(sorted(buffer))}"
        )

    # Sort all boxes by fourcc
    all_boxes = sorted(all_boxes, key=lambda _box: _box.fourcc)

    # Create output directory
    if not os.path.exists("output"):
        os.makedirs("output")

    with open("output/boxes.json", "w") as f:
        json.dump(
            [_box.serialize() for _box in all_boxes],
            f,
            indent=2,
        )


if __name__ == "__main__":
    main()
