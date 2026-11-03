"""Tests for planning the colon-name to subfolder migration (#260)."""

from __future__ import annotations

from plaud_tools.core.folders import plan_colon_nesting, split_colon_name
from plaud_tools.core.models import Folder


def folder(id: str, name: str, parent_id: str = "root") -> Folder:
    return Folder(id=id, name=name, parent_id=parent_id)


def test_colon_folders_become_subfolders_of_a_created_parent():
    plan = plan_colon_nesting(
        [
            folder("a", "Client Meetings:Prospects"),
            folder("b", "Client Meetings:Active:2026"),
            folder("c", "Personal"),
        ]
    ).as_dict()
    assert plan["folders_to_convert"] == 2
    assert plan["create"] == ["Client Meetings", "Client Meetings > Active"]
    assert [move["to"] for move in plan["convert"]] == [
        "Client Meetings > Prospects",
        "Client Meetings > Active > 2026",
    ]


def test_an_existing_folder_is_reused_as_the_parent():
    plan = plan_colon_nesting([folder("p", "Personal"), folder("a", "Personal:Health")])
    assert plan.creates == []
    assert plan.owners[("root", "Personal")] == "p"
    assert [move.path for move in plan.moves] == [("root", "Personal", "Health")]


def test_a_taken_place_skips_the_folder_and_the_colon_folders_inside_it():
    plan = plan_colon_nesting(
        [
            folder("x", "Clients:Active"),
            folder("y", "Clients"),
            folder("z", "Active", parent_id="y"),
            folder("inner", "Q1:Notes", parent_id="x"),
        ]
    )
    assert plan.moves == []
    assert [(f.id, reason.split(",")[0]) for f, reason in plan.skipped] == [
        ("x", "another folder would also be 'Clients > Active'"),
        ("inner", "it is inside 'Clients:Active'"),
    ]


def test_names_with_an_empty_part_are_left_alone():
    assert split_colon_name("Client Meetings : Prospects") == ["Client Meetings", "Prospects"]
    for name in (":Prospects", "Clients:", "A::B", "Plain"):
        assert split_colon_name(name) == [name]
    assert plan_colon_nesting([folder("a", "Clients:")]).moves == []
