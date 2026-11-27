"""Frozen workflow definitions captured from bdbcd08 before graph composition changed."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from forgepilot.agents.analysis import AnalysisService
from forgepilot.agents.delivery import DeliveryWorkflow
from forgepilot.agents.implementation import ImplementationService
from forgepilot.artifacts.canonical import content_sha256
from forgepilot.errors import ForgePilotError
from forgepilot.mission_events import append_mission_event
from forgepilot.mission_host import MissionHost
from forgepilot.mission_tools import MissionToolDependencies
from forgepilot.models.gateway import ModelGateway
from forgepilot.orchestration.graph import validate_graph
from forgepilot.sqlite_store import SqliteEventStore
from forgepilot.task_host import TaskHost
from forgepilot.workspace_manager import WorkspaceManager
from test_analysis_workflow import request, spec_data
from test_mission_factory import configuration


IMPLEMENTATION_ANALYSIS_V1 = "ba7d3e4455d04043bfa755ad94d3274806df02fee99df58b5735b64827505921"
DELIVERY_ANALYSIS_V2 = "5d2c814436dd98764ffc093e97ed8be5d8779d6377b906e0a67a69ee5be69665"


@pytest.fixture
def graph_workflow_factory(tmp_path):
    events = SqliteEventStore(artifact_root=tmp_path / "artifacts")
    manager = WorkspaceManager(tmp_path / "workspace")
    gateway = ModelGateway(configuration(), secret_resolver=lambda _: "offline")

    def make(workflow_type, *, single_agent=False, frozen_selection=None):
        kwargs = {"validator": SimpleNamespace(workspace=manager)} if workflow_type is DeliveryWorkflow else {}
        service = workflow_type(events, manager, gateway, single_agent=single_agent, **kwargs)
        if frozen_selection is not None:
            selection = SimpleNamespace(to_dict=lambda: frozen_selection)
            service.configure_mission_tools(MissionToolDependencies("frozen", selection, {}))
        service._analysis_services["frozen"] = AnalysisService(events, None, gateway, single_agent=single_agent)
        return service

    try:
        yield make
    finally:
        events.close()


@pytest.mark.parametrize("paused,action", [
    ("implementer", "resume"), ("repair_1", "resume"), ("delivery_bundle", "retry"),
])
def test_paused_single_agent_projects_actions_from_frozen_roles(graph_workflow_factory, paused, action):
    workflow = graph_workflow_factory(DeliveryWorkflow, single_agent=True)
    graph = workflow.graph()
    workflow.runner.store.create_run("frozen", validate_graph(graph))
    events = workflow.events
    with events._transaction():
        append_mission_event(events, "frozen", "task.created", {"orchestration_mode": "single_agent"})
        events._db.execute("UPDATE task_graph_runs SET status='retryable' WHERE task_id='frozen'")
        events._db.execute("UPDATE graph_node_states SET status='retryable' WHERE task_id='frozen' AND node_id=?", (paused,))

    host = MissionHost(TaskHost(lambda _: None, store=events))
    projected = host.graph("frozen")
    assert {n["node_id"]: n["retry_actions"] for n in projected["nodes"] if n["retry_actions"]} == {paused: [action]}
    assert projected["definition"] == graph.to_dict()
    assert projected["definition_hash"] == graph.definition_hash
    if action == "resume":
        with pytest.raises(ForgePilotError, match="recovery_confirmation_required"):
            workflow.runner.admit_retry("frozen", graph, paused, expected_revision=projected["current_revision"])
    else:
        admitted = workflow.runner.admit_retry("frozen", graph, paused, expected_revision=projected["current_revision"])
        assert admitted.revision == projected["current_revision"] + 1


# A locked delivery graph keeps its historical .v2 ID even at graph versions 3 and 4.
@pytest.mark.parametrize("version,single_agent,mode,frozen_hash,json_sha256", [
    (1, False, "plain",
     "309bad066dae7582864abc4c5add6d5a5ae66fbb4932a026a0d5fcbb281e42d2",
     "666212b22d0566c6d0e74ca33003a6dab8a649be6d460743393b7cde7d991bb5"),
    (1, False, "analysis",
     IMPLEMENTATION_ANALYSIS_V1,
     "4c89278cff7f4054f700b6179f29e801088bdd81230844fdda34eb12a6dd735d"),
    (1, False, "locked",
     "60d4da1c1e0a9ce766f8e592f8226eb843e4feb3be29d210cbaf6df9b372368c",
     "3728e24386c6938d705baeb07cdcf989faeda3e309e5660497ad2f5bf20cca71"),
    (2, False, "plain",
     "418f6c965a6e07510360a66c1a2636c395e79710b38db1eb7c6dfa45dbc18fd4",
     "7063de44a80c65aa13db39d086b48c2cf81129f8a2f6ad6d24b2e4eaefe759ce"),
    (2, False, "analysis",
     DELIVERY_ANALYSIS_V2,
     "d88b8c5b74858baf3d670892b28c6894da1b7d75389d61e304bd6b9562697e19"),
    (2, False, "locked",
     "768758a757263cd2cba4fe753fcd60f41d6f48cb42eba8478e37f7e79b5c8a10",
     "1e3e629104b20595c411362f2cf62b12a4391e0d8444219853bcf52796b0bd4c"),
    (3, False, "plain",
     "c673436acf45caba00b3ef2e7042b44d0cbc122523d9138ef77f33560cdbce2f",
     "f8aa33e5834cd97bcd9c951ff8a0e933d5a776e48785f2c7e914f3693aef0988"),
    (3, False, "analysis",
     "c689ca16906e58b9313066e59cfd400800c8658e8925b85cac1b0bcc614c769a",
     "c43f4e9961247224ce35ecab9609cac55a296d72ca1adde9a8232a27dea56181"),
    (3, False, "locked",
     "290753ee8f094463a90d6c36bb7138b08668798274f217c42a8de057a87761f3",
     "8309fd64db0391f870a2185cb4177c60cf47cd49c7047db6981fce023cf6f775"),
    (4, False, "plain",
     "904f87ffb2a4c21dc193b32ae0f373334a3717e00307b7fcf4286303d51f1537",
     "9c0793f1770a8fef5b70acdcb945138742b3494a0d86da99f7f004f10edc7490"),
    (4, False, "analysis",
     "456010d1a1f36099b760a43c9bd1be921416da3bb53c41a687627360a3c67015",
     "4dc9695052fd5273e7736a2e3ad05069c86bf021b2027df2691a71f0cdf34aef"),
    (4, False, "locked",
     "f3eb6b57e1b219db6dc4fe563db60035b3432d27a714fcfd484d666623ad1376",
     "8463f0dfa3b3ef9d9982e9d4724f98c7e6af4aff5e5dbdf7c63027943c33a465"),
    (1, True, "plain",
     "43d2ca86265654746b07673723d208e63b07dd291e43ee6091d34c2b43eef284",
     "66b0c2a91b3a86e02719763e702f5e91d2b854476915d662f8ebebeaf094b370"),
    (1, True, "locked",
     "acc756115d0cd4a59604e045e214815aeed71037962b0ad72224504445a734f6",
     "bfbc77d43ae56b226669ea2f5a01bd8ee34fe0298ecd5aec58c610e8ce40df7f"),
    (2, True, "plain",
     "3cbaf22a215ff6ae06d73d2de16cdda218a4ebaf4f14efcfa363948d3191f702",
     "48b89b1762f618425368acd34b4928c3076274b9c29d049a6f8fa154ec7acbfc"),
    (2, True, "locked",
     "f92d3ae6ee147622fdb0cca17c58e2bc29e85023830de705ccd9abf4df5d29a1",
     "bc54a06a0831bd2732b9560572fda3a71ccec4008e4d25f5b7fc4771ee0386a7"),
    (3, True, "plain",
     "2760ddf8a669f5c71201443c1c51b88c3b69fad3c6b0b53fbd822ac5e304d443",
     "937b71808382859d14e1ce43412843f4a22cbcabef6a90a1f10d0f0a2681cb91"),
    (3, True, "locked",
     "b1674a2068094875a6a03aaecee59b6afca8f747700aa95f61e09b4f35887124",
     "249a60557f21d62bfe31e5b0211e4d8caf03c890ce48e39ae374ab30533ff57e"),
    (4, True, "plain",
     "8d73f511fbb77c40b3ae02ebd6088544cee580bc69541e32e6479eb5eb8a83d6",
     "582ace28843748e875f18a6434beb6ef43098306f7e49dd7cf7581b828d2b557"),
    (4, True, "locked",
     "017f4a55648866401bc3954075f0f04355125a736bbb6274856b487345712aa5",
     "2fdc0605b22b94836f8071880d2dc83a5f6928bdb66d1a33593f81a03f542226"),
])
def test_workflow_rebuilds_frozen_graph(
        graph_workflow_factory, version, single_agent, mode, frozen_hash, json_sha256):
    workflow_type = ImplementationService if version == 1 else DeliveryWorkflow
    service = graph_workflow_factory(workflow_type, single_agent=single_agent,
        frozen_selection={"servers": []} if version >= 4 else None)
    if version >= 3:
        service.mission_source_baseline = {"commit": "a" * 40}
        service.mission_build_selection = {"configuration": "Release"}
    if mode == "plain":
        graph = service.graph()
    else:
        locked = spec_data() if mode == "locked" else None
        graph = service._analysis_graph("frozen", request(), locked_task_spec=locked)
    service.runner.store.create_run("frozen", validate_graph(graph))
    for definition in (graph, service._stored_graph("frozen")):
        assert definition.definition_hash == frozen_hash
        assert content_sha256(definition.to_dict()) == json_sha256


@pytest.mark.parametrize("service_type,frozen_hash", [
    (ImplementationService, IMPLEMENTATION_ANALYSIS_V1),
    (DeliveryWorkflow, DELIVERY_ANALYSIS_V2),
])
def test_composition_identifies_approval_in_reordered_fragments(
        graph_workflow_factory, monkeypatch, service_type, frozen_hash):
    def move_approval_to_second(graph):
        approval = next(node for node in graph.nodes if node.node_id == "execution_approval")
        others = [node for node in graph.nodes if node.node_id != approval.node_id]
        return replace(graph, nodes=(others[0], approval, *others[1:]))

    workflow = graph_workflow_factory(service_type)
    analysis = workflow._analysis_for("frozen")
    analysis_request = request()
    analysis_graph = move_approval_to_second(analysis.graph(analysis_request))
    execution_graph = move_approval_to_second(workflow.graph())
    monkeypatch.setattr(analysis, "graph", lambda *args, **kwargs: analysis_graph)
    monkeypatch.setattr(workflow, "graph", lambda task_id: execution_graph)

    combined = workflow._analysis_graph("frozen", analysis_request)
    assert combined.definition_hash == frozen_hash
