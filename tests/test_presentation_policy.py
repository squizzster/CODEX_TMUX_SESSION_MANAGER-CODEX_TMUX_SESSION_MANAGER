"""Typed presentation policies preserve event meaning independently of rendering."""

from rodex.interaction_pipeline import InteractionOperation, InteractionRequest, SessionInteractionPipeline
from rodex.presentation_policy import (
    PRESENTATION_POLICY_TARGET,
    PresentationEventKind,
    PresentationEventSelector,
    PresentationPolicyConfig,
    PresentationPolicyInteractionAdapter,
    PresentationSurface,
    SessionPresentationPipeline,
)

ROOT = "root-thread"
CHILD = "child-thread"


def item_event(method, item, *, thread_id=ROOT, turn_id="turn-1"):
    return {
        "method": method,
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": item,
        },
    }


def message_started(item_id="message-1", *, phase="commentary", thread_id=ROOT):
    return item_event(
        "item/started",
        {"id": item_id, "type": "agentMessage", "phase": phase, "text": ""},
        thread_id=thread_id,
    )


def message_delta(text, item_id="message-1", *, thread_id=ROOT):
    return {
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": thread_id,
            "turnId": "turn-1",
            "itemId": item_id,
            "delta": text,
        },
    }


def message_completed(text, item_id="message-1", *, phase="commentary", thread_id=ROOT):
    return item_event(
        "item/completed",
        {"id": item_id, "type": "agentMessage", "phase": phase, "text": text},
        thread_id=thread_id,
    )


def thread_read_response(request_id, *items):
    return {
        "id": request_id,
        "result": {
            "thread": {
                "id": ROOT,
                "turns": [{"id": "turn-1", "items": list(items)}],
            }
        },
    }


def test_light_projects_only_exact_root_commentary_while_dark_remains_native():
    presentation = SessionPresentationPipeline()
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(
        item_event(
            "item/started",
            {"id": "command-1", "type": "commandExecution", "status": "inProgress"},
        )
    )
    presentation.observe_protocol_output(message_started())
    presentation.observe_protocol_output(message_delta("Checking "))
    presentation.observe_protocol_output(message_delta("the boundary."))
    presentation.observe_protocol_output(message_completed("Checking the boundary."))
    presentation.observe_protocol_output(message_completed("Secret final", "final-1", phase="final_answer"))
    presentation.observe_protocol_output(message_completed("Child update", "child-1", thread_id=CHILD))

    dark = presentation.snapshot()
    assert dark.policy_name == "dark" and dark.surface == PresentationSurface.NATIVE
    assert dark.events == () and dark.items == ()

    presentation.select_policy("light")
    light = presentation.snapshot()
    assert light.surface == PresentationSurface.SEMANTIC
    assert [item.text for item in light.items] == ["Checking the boundary."]
    assert {event.item_type for event in light.events} == {"agentMessage"}
    assert {event.phase for event in light.events} == {"commentary"}
    assert all(event.thread_id == ROOT for event in light.events)


def test_revision_is_a_cheap_change_token_and_subscribers_receive_only_real_changes():
    presentation = SessionPresentationPipeline()
    wakes = []
    unsubscribe = presentation.subscribe(lambda: wakes.append(presentation.revision))

    assert presentation.revision == 0
    presentation.bind_root_thread(ROOT)
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(None)
    assert presentation.revision == 1
    assert wakes == [1]

    unsubscribe()
    presentation.select_policy("light")
    assert presentation.revision == 2
    assert wakes == [1]


def test_streamed_commentary_is_visible_before_completion_and_completed_text_is_not_duplicated():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(message_started())
    presentation.observe_protocol_output(message_delta("Live "))
    presentation.observe_protocol_output(message_delta("update"))
    assert [item.text for item in presentation.snapshot().items] == ["Live update"]
    assert presentation.snapshot().items[0].complete is False

    presentation.observe_protocol_output(message_completed("Live update"))
    assert [item.text for item in presentation.snapshot().items] == ["Live update"]
    assert presentation.snapshot().items[0].complete is True


def test_unknown_or_missing_phase_is_not_guessed_from_message_words():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(message_completed("commentary", phase="final_answer"))
    presentation.observe_protocol_output(message_completed("I sound like an update", "unknown", phase=None))
    assert presentation.snapshot().items == ()


def test_semantic_policy_waits_for_the_exact_root_thread_binding():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.observe_protocol_output(message_completed("Not yet bound"))
    assert presentation.snapshot().items == ()

    presentation.bind_root_thread(ROOT)
    assert [item.text for item in presentation.snapshot().items] == ["Not yet bound"]


def test_noisy_unselected_events_cannot_evict_selected_item_text():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(message_completed("Keep this commentary"))
    for index in range(600):
        presentation.observe_protocol_output(
            item_event(
                "item/completed",
                {
                    "id": f"command-{index}",
                    "type": "commandExecution",
                    "status": "completed",
                    "text": f"hidden output {index}",
                },
            )
        )

    snapshot = presentation.snapshot()
    assert snapshot.events == ()
    assert [item.text for item in snapshot.items] == ["Keep this commentary"]


def test_new_policy_selectors_can_admit_another_structural_event_without_transport_changes():
    policies = (
        PresentationPolicyConfig("dark", PresentationSurface.NATIVE),
        PresentationPolicyConfig(
            "commands",
            PresentationSurface.SEMANTIC,
            (PresentationEventSelector(item_types=frozenset({"commandExecution"})),),
            "Command activity",
        ),
    )
    presentation = SessionPresentationPipeline(policies)
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(
        item_event(
            "item/completed",
            {"id": "command-1", "type": "commandExecution", "status": "completed"},
        )
    )
    presentation.select_policy("commands")
    assert [(event.item_type, event.status) for event in presentation.snapshot().events] == [
        ("commandExecution", "completed")
    ]


def test_item_delta_inherits_its_structural_identity_for_future_configured_policies():
    policies = (
        PresentationPolicyConfig(
            "command-output",
            PresentationSurface.SEMANTIC,
            (
                PresentationEventSelector(
                    kinds=frozenset({PresentationEventKind.ITEM_DELTA}),
                    item_types=frozenset({"commandExecution"}),
                ),
            ),
            "Command output",
        ),
    )
    presentation = SessionPresentationPipeline(policies, initial_policy="command-output")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(
        item_event(
            "item/started",
            {"id": "command-1", "type": "commandExecution", "status": "inProgress"},
        )
    )
    presentation.observe_protocol_output(
        {
            "method": "item/commandExecution/outputDelta",
            "params": {
                "threadId": ROOT,
                "turnId": "turn-1",
                "itemId": "command-1",
                "delta": "typed command output",
            },
        }
    )

    assert [(event.item_type, event.text) for event in presentation.snapshot().events] == [
        ("commandExecution", "typed command output")
    ]
    assert [(item.item_type, item.text) for item in presentation.snapshot().items] == [
        ("commandExecution", "typed command output")
    ]


def test_item_retains_configured_selector_evidence_while_later_events_supply_its_text():
    policies = (
        PresentationPolicyConfig(
            "started-commands",
            PresentationSurface.SEMANTIC,
            (
                PresentationEventSelector(
                    methods=frozenset({"item/started"}),
                    item_types=frozenset({"commandExecution"}),
                ),
            ),
            "Started command output",
        ),
    )
    presentation = SessionPresentationPipeline(policies, initial_policy="started-commands")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(
        item_event(
            "item/started",
            {"id": "command-1", "type": "commandExecution", "status": "inProgress"},
        )
    )
    for index in range(20):
        presentation.observe_protocol_output(
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "threadId": ROOT,
                    "turnId": "turn-1",
                    "itemId": "command-1",
                    "delta": str(index % 10),
                },
            }
        )

    assert [item.text for item in presentation.snapshot().items] == ["01234567890123456789"]


def test_thread_read_response_hydrates_typed_history_once_using_exact_request_identity():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    response = thread_read_response(
        7,
        {"id": "commentary-1", "type": "agentMessage", "phase": "commentary", "text": "From history"},
        {"id": "answer-1", "type": "agentMessage", "phase": "final_answer", "text": "Hidden answer"},
        {"id": "command-1", "type": "commandExecution", "status": "completed"},
    )
    presentation.observe_protocol_input({"id": 7, "method": "thread/read"})
    presentation.observe_protocol_output({**response, "id": "7"})
    assert presentation.snapshot().items == ()

    presentation.observe_protocol_output(response)
    first_snapshot = presentation.snapshot()
    assert [item.text for item in first_snapshot.items] == ["From history"]

    presentation.observe_protocol_input({"id": 8, "method": "thread/read"})
    presentation.observe_protocol_output({**response, "id": 8})
    second_snapshot = presentation.snapshot()
    assert second_snapshot.items == first_snapshot.items
    assert second_snapshot.events == first_snapshot.events


def test_disconnect_forgets_pending_response_correlations_even_without_partial_text():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_input({"id": 7, "method": "thread/read"})
    presentation.reset_after_disconnect()
    presentation.observe_protocol_output(
        thread_read_response(
            7,
            {"id": "commentary-1", "type": "agentMessage", "phase": "commentary", "text": "Stale response"},
        )
    )
    assert presentation.snapshot().items == ()


def test_policy_selection_uses_the_shared_interaction_target_and_rejects_unknown_policies():
    interactions = SessionInteractionPipeline()
    presentation = SessionPresentationPipeline()
    adapter = PresentationPolicyInteractionAdapter(interactions, presentation, "runtime-1")
    try:
        result = interactions.execute(
            InteractionRequest(
                PRESENTATION_POLICY_TARGET,
                InteractionOperation.SELECT_PRESENTATION_POLICY,
                "configured-input",
                payload="light",
            )
        )
        assert result.accepted and presentation.snapshot().policy_name == "light"
        rejected = interactions.execute(
            InteractionRequest(
                PRESENTATION_POLICY_TARGET,
                InteractionOperation.SELECT_PRESENTATION_POLICY,
                "configured-input",
                payload="unknown",
            )
        )
        assert not rejected.accepted and presentation.snapshot().policy_name == "light"
    finally:
        adapter.close()
    assert PRESENTATION_POLICY_TARGET not in interactions._targets


def test_disconnect_discards_only_partial_item_text_state():
    presentation = SessionPresentationPipeline(initial_policy="light")
    presentation.bind_root_thread(ROOT)
    presentation.observe_protocol_output(message_started("partial"))
    presentation.observe_protocol_output(message_delta("unfinished", "partial"))
    presentation.observe_protocol_output(message_completed("retained", "complete"))
    presentation.reset_after_disconnect()
    assert [item.text for item in presentation.snapshot().items] == ["retained"]
