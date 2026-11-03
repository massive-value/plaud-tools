"""Planning the one-time move from colon-named folders to real subfolders.

Before Plaud 4.0 had nested folders, libraries faked them with names such as
"Client Meetings:Prospects".  ``plan_colon_nesting`` works out, from a
``list_folders`` listing and without touching Plaud, which folders would turn
into which subfolders and which parent folders must be created.
``PlaudClient.nest_colon_folders`` carries a plan out, and the CLI
``folder nest-colon-names`` command shows it first.

A folder's *path* is a tuple: the id of the node it ultimately sits in
("root", or a system folder) followed by the folder names from there down,
with each colon name split into its parts.  "Client Meetings:Prospects" at the
top level has the path ("root", "Client Meetings", "Prospects"), which is
exactly where it ends up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Folder

Path = tuple[str, ...]


def split_colon_name(name: str) -> list[str]:
    """A colon name's parts ("A : B" gives ["A", "B"]), or ``[name]`` when it is not one.

    A name with an empty part (":B", "A::B", "A:") is left alone.
    """
    parts = [part.strip() for part in name.split(":")]
    return parts if len(parts) > 1 and all(parts) else [name]


def display_path(path: Path) -> str:
    """A path for people: its folder names joined with " > "."""
    return " > ".join(path[1:])


def parent_id_of(path: Path, ids: dict[Path, str]) -> str:
    """The id of the folder *path* sits in: the starting node, or the folder one level up."""
    return path[0] if len(path) == 2 else ids[path[:-1]]


@dataclass(frozen=True, slots=True)
class FolderNesting:
    """One colon-named folder and the path it moves to (its new name is the last part)."""

    folder: Folder
    path: Path


@dataclass(slots=True)
class ColonNestingPlan:
    """What ``nest_colon_folders`` would do.

    ``owners`` maps the path of every folder that will exist after the run
    (unchanged folders and converted ones) to its id; ``creates`` lists the
    parent paths to create, shallowest first; ``skipped`` holds colon-named
    folders left alone, each with the reason.
    """

    owners: dict[Path, str] = field(default_factory=dict)
    creates: list[Path] = field(default_factory=list)
    moves: list[FolderNesting] = field(default_factory=list)
    skipped: list[tuple[Folder, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        """The plan as JSON-able data, for the CLI's dry run and its result."""
        return {
            "folders_to_convert": len(self.moves),
            "create": [display_path(path) for path in self.creates],
            "convert": [
                {"folder_id": move.folder.id, "from": move.folder.name, "to": display_path(move.path)}
                for move in self.moves
            ],
            "skipped": [
                {"folder_id": folder.id, "name": folder.name, "reason": reason}
                for folder, reason in self.skipped
            ],
        }


def plan_colon_nesting(folders: list[Folder]) -> ColonNestingPlan:
    """Plan real subfolders for every colon-named folder in a ``list_folders`` listing.

    Each colon folder is planned relative to the folder it sits in now.  A
    folder whose new place is already taken by another folder (Plaud refuses
    two folders with one name side by side) is skipped, and so is any colon
    folder inside a skipped one.  Everything else in the library is unchanged.
    """
    by_id = {folder.id: folder for folder in folders}
    paths: dict[str, Path] = {}

    def path_of(folder: Folder) -> Path:
        if folder.id not in paths:
            parent = by_id.get(folder.parent_id)
            base = path_of(parent) if parent else (folder.parent_id,)
            paths[folder.id] = base + tuple(split_colon_name(folder.name))
        return paths[folder.id]

    claims: dict[Path, list[Folder]] = {}
    for folder in folders:
        claims.setdefault(path_of(folder), []).append(folder)

    def is_colon(folder: Folder) -> bool:
        return len(split_colon_name(folder.name)) > 1

    plan = ColonNestingPlan()
    skipped: set[str] = set()
    # Shallow first, so a skipped folder is known before the folders inside it.
    for folder in sorted(folders, key=lambda f: len(path_of(f))):
        ancestor = by_id.get(folder.parent_id)
        while ancestor and ancestor.id not in skipped:
            ancestor = by_id.get(ancestor.parent_id)
        if ancestor is not None:
            # Its planned path assumed the skipped folder moves, so it is not real.
            skipped.add(folder.id)
            if is_colon(folder):
                plan.skipped.append((folder, f"it is inside {ancestor.name!r}, which is skipped"))
            continue
        if is_colon(folder) and len(claims[path_of(folder)]) > 1:
            skipped.add(folder.id)
            plan.skipped.append((folder, f"another folder would also be {display_path(path_of(folder))!r}"))
            continue
        plan.owners[path_of(folder)] = folder.id
        if is_colon(folder):
            plan.moves.append(FolderNesting(folder, path_of(folder)))

    for move in plan.moves:
        for depth in range(2, len(move.path)):
            prefix = move.path[:depth]
            if prefix not in plan.owners and prefix not in plan.creates:
                plan.creates.append(prefix)
    plan.creates.sort(key=len)
    return plan
