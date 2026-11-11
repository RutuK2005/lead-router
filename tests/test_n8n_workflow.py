import json
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / "n8n" / "lead-router-workflow.json"


def test_workflow_is_valid_and_connections_point_to_real_nodes():
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    names = {node["name"] for node in workflow["nodes"]}
    assert len(names) == len(workflow["nodes"]), "node names must be unique"

    for source, outputs in workflow["connections"].items():
        assert source in names
        for branch in outputs["main"]:
            for target in branch:
                assert target["node"] in names


def test_switch_routes_hot_warm_cold():
    workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
    switch = next(n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base.switch")
    keys = [rule["outputKey"] for rule in switch["parameters"]["rules"]["values"]]
    assert keys == ["hot", "warm", "cold"]
